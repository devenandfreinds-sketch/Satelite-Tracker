"""Maneuver detector.

  python -m tracker.detect [--norad 25544 ...] [--all-sources] [--z 10]

Reads stored element sets, writes events to the `maneuver_events` table.

Method (see docs/METHOD.md for the reasoning):

1. Drop outlier element sets: a set that jumps away from both neighbours and
   the neighbours agree with each other is a bad fit, not a burn.
2. For each consecutive pair, take the mean-element steps (SMA, eccentricity,
   and the orbit-normal vector with J2 nodal drift removed) minus the object's normal drift
   (trailing median rate, e.g. drag decay), and the SGP4 position residual in
   radial / in-track / cross-track.
3. Score both against the object's own noise. Noise grows with the gap
   between sets, so sigma = s * (gap_days + TAU_D), with s a robust (MAD)
   scale over the trailing pairs. Only past data is used, so a flag is
   one we could have raised live.
4. A pair is a candidate only when an element step AND the position miss it
   physically implies both reach the threshold: SMA/eccentricity steps need an
   in-track or radial miss, plane changes need a cross-track miss. Element
   steps alone are noisy (fits trade eccentricity against other elements); a
   real burn also moves the satellite.
   It is confirmed when the change persists: the set before the event,
   compared directly with the set after the next one, still disagrees.
5. Adjacent flagged pairs (a burn smeared across two fits) merge into one event.
6. Delta-v is the minimum impulsive delta-v consistent with the element change,
   for a near-circular orbit, with a 1-sigma uncertainty from the noise model.
"""
import argparse
import math

import numpy as np
import pandas as pd

from . import classify, db, watchlist
from .elements import MU_EARTH
from .residuals import _j2_raan_rate_deg_day, compute as compute_residuals

TAU_D = 0.1          # noise floor, in days of equivalent gap (fit noise of a single set)
WINDOW = 40          # trailing pairs used for drift and noise
MIN_HISTORY = 15     # pairs needed before an object is scored at all
Z_THRESHOLD = 10.0
PERSIST_FRACTION = 0.5   # step must keep >= this share of the threshold in the k-1 -> k+1 check
DV_SYSTEMATIC_FRACTION = 0.05
FEATURES = {
    # name: (column in residual frame, unit). Plane changes are scored on the
    # orbit-normal components, which stay well defined at zero inclination.
    "sma": ("d_sma_km", "km"),
    "ecc": ("d_ecc", ""),
    "nx": ("d_nx", ""),
    "ny": ("d_ny", ""),
    "nz": ("d_nz", ""),
}
PLANE = ("nx", "ny", "nz")
POSITION = {"radial": "res_radial_km", "intrack": "res_intrack_km", "cross": "res_cross_km"}
POSITION_FLOOR_KM = 1e-3
# Keeps z finite for elements that are effectively noiseless in a regime.
SCALE_FLOOR = {"sma": 1e-4, "ecc": 1e-7, "nx": 1e-8, "ny": 1e-8, "nz": 1e-8}

EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS maneuver_events (
    id                 INTEGER PRIMARY KEY,
    norad_id           INTEGER NOT NULL,
    data_scope         TEXT NOT NULL,      -- 'publishable' or 'all_sources'
    status             TEXT NOT NULL,      -- 'confirmed' or 'provisional'
    window_start_utc   TEXT NOT NULL,      -- epoch of last set before the burn
    window_end_utc     TEXT NOT NULL,      -- epoch of first set after the burn
    confirmed_at_utc   TEXT,               -- epoch of the set that confirmed persistence
    n_pairs            INTEGER NOT NULL,
    max_z              REAL NOT NULL,
    driver             TEXT NOT NULL,      -- which element drove max_z
    d_sma_km           REAL, d_inc_deg REAL, d_raan_deg REAL, d_ecc REAL,
    z_sma REAL, z_ecc REAL, z_plane REAL,
    plane_change_deg   REAL,
    dv_inplane_ms      REAL, dv_plane_ms REAL, dv_plane_sigma_ms REAL,
    plane_change       INTEGER,            -- 1 if the plane change is significant and in the total
    dv_total_ms        REAL, dv_sigma_ms REAL,   -- total omits a plane change below 3 sigma
    before_perigee_km  REAL, before_apogee_km REAL, before_inc_deg REAL,
    after_perigee_km   REAL, after_apogee_km REAL, after_inc_deg REAL,
    intrack_resid_km   REAL,
    z_pos              REAL,
    regime             TEXT,               -- geo | leo | other
    label              TEXT,               -- see tracker/classify.py
    routine            INTEGER,            -- 1 = looks like ordinary upkeep
    lon_before_deg     REAL, lon_after_deg REAL,               -- GEO only
    drift_before_deg_day REAL, drift_after_deg_day REAL,       -- GEO only
    campaign_id        TEXT,
    detected_run_utc   TEXT NOT NULL,
    UNIQUE (norad_id, data_scope, window_end_utc)
);

CREATE TABLE IF NOT EXISTS maneuver_campaigns (
    campaign_id        TEXT NOT NULL,
    norad_id           INTEGER NOT NULL,
    data_scope         TEXT NOT NULL,
    start_utc          TEXT NOT NULL,
    end_utc            TEXT NOT NULL,
    n_events           INTEGER NOT NULL,
    span_days          REAL,
    total_dv_ms        REAL, total_dv_sigma_ms REAL,
    net_d_sma_km       REAL,
    labels             TEXT,
    PRIMARY KEY (campaign_id, data_scope)
);
"""


def _robust_scale(x):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return np.nan
    return 1.4826 * np.median(np.abs(x - np.median(x)))


def drop_outlier_sets(history, z=None, passes=2):
    """Remove isolated bad element sets. Returns (clean_history, dropped_epochs)."""
    z = Z_THRESHOLD if z is None else z
    dropped = []
    h = history
    for _ in range(passes):
        if len(h) < MIN_HISTORY + 2:
            break
        r = compute_residuals(h)
        # Normalise the SMA step by a global robust scale for this object; good
        # enough to spot a there-and-back spike, which is all this step needs.
        s = _robust_scale(r.d_sma_km / (r.dt_hours / 24 + TAU_D))
        if not np.isfinite(s) or s == 0:
            break
        zz = (r.d_sma_km / ((r.dt_hours / 24 + TAU_D) * s)).to_numpy()
        bad = []
        # pair i compares sets i and i+1 (residual rows are 1 shorter than history)
        for i in range(len(zz) - 1):
            a, b = zz[i], zz[i + 1]
            if abs(a) >= z and abs(b) >= z and np.sign(a) != np.sign(b):
                # set i+1 jumped out and back; check neighbours agree without it
                k = i + 1
                if abs(h.sma_km.iloc[k + 1] - h.sma_km.iloc[k - 1]) < 0.5 * abs(h.sma_km.iloc[k] - h.sma_km.iloc[k - 1]):
                    bad.append(k)
        if not bad:
            break
        dropped += list(h.epoch_utc.iloc[bad])
        h = h.drop(index=h.index[bad]).reset_index(drop=True)
    return h, dropped


def _v_circ_ms(a_km):
    return math.sqrt(MU_EARTH / a_km) * 1000.0


def delta_v(a_km, d_sma, d_ecc, plane_angle_rad, sigma_sma=0.0, sigma_ecc=0.0):
    """Minimum impulsive delta-v (m/s) for a near-circular orbit.

    In-plane: a tangential burn dv gives da/a = 2 dv/v and de = 2 dv/v, so the
    larger of the two implied values is a lower bound. Out-of-plane: a pure
    plane rotation of angle theta costs 2 v sin(theta/2).
    Returns (inplane, plane, total, sigma).
    """
    v = _v_circ_ms(a_km)
    dv_a = 0.5 * v * abs(d_sma) / a_km
    dv_e = 0.5 * v * abs(d_ecc)
    dv_in = max(dv_a, dv_e)
    dv_pl = 2 * v * math.sin(plane_angle_rad / 2)
    sigma = 0.5 * v * (sigma_sma / a_km if dv_a >= dv_e else sigma_ecc)
    return dv_in, dv_pl, math.hypot(dv_in, dv_pl), sigma


def _scale_and_sigma(past_res, past_dt, dt, floor):
    s = _robust_scale(past_res / (past_dt + TAU_D))
    s = max(s if np.isfinite(s) else 0.0, floor)
    return s * (dt + TAU_D)


def score(history):
    """Residual frame with drift-corrected element steps, position residuals,
    their sigmas and z-scores, all from trailing pairs only."""
    r = compute_residuals(history)
    if r.empty:
        return r
    dt_d = r.dt_hours / 24.0
    r["dt_d"] = dt_d
    for name, (col, _) in FEATURES.items():
        r[f"rate_{name}"] = r[col] / dt_d.where(dt_d > 1e-3)
        for pre in ("exp", "rate_median", "sig", "z"):
            r[f"{pre}_{name}"] = np.nan
    for name in POSITION:
        r[f"sig_{name}"] = np.nan
        r[f"z_{name}"] = np.nan
    r["z_elem"] = np.nan
    r["z_pos"] = np.nan
    r["driver"] = None
    # Drift and noise come from the trailing WINDOW pairs, burns included. The
    # median and MAD shrug off the occasional burn, and unlike excluding
    # flagged pairs, they still adapt when an object's noise genuinely rises
    # (excluding flags froze the noise scale and flagged everything after).
    for k in range(len(r)):
        past = r.iloc[max(0, k - WINDOW):k]
        if len(past) < MIN_HISTORY:
            continue
        dt = r.at[k, "dt_d"]
        best, driver = 0.0, None
        for name, (col, _) in FEATURES.items():
            rate = np.nanmedian(past[f"rate_{name}"])
            rate = rate if np.isfinite(rate) else 0.0
            sig = _scale_and_sigma(past[col] - rate * past["dt_d"], past["dt_d"], dt, SCALE_FLOOR[name])
            z = (r.at[k, col] - rate * dt) / sig
            r.at[k, f"rate_median_{name}"] = rate
            r.at[k, f"exp_{name}"] = rate * dt
            r.at[k, f"sig_{name}"] = sig
            r.at[k, f"z_{name}"] = z
            if abs(z) > abs(best):
                best, driver = z, name
        zpos = 0.0
        for name, col in POSITION.items():
            bias = np.nanmedian(past[col] / (past["dt_d"] + TAU_D))
            sig = _scale_and_sigma(past[col], past["dt_d"], dt, POSITION_FLOOR_KM)
            z = (r.at[k, col] - bias * (dt + TAU_D)) / sig
            r.at[k, f"sig_{name}"] = sig
            r.at[k, f"z_{name}"] = z
            zpos = max(zpos, abs(z))
        r.at[k, "z_elem"] = abs(best)
        r.at[k, "z_pos"] = zpos
        r.at[k, "driver"] = driver
    # Each kind of element change must be matched by the position miss it
    # physically causes: an in-plane burn (SMA, eccentricity) shows up in-track
    # and radially, a plane change shows up cross-track.
    z_inplane = r[["z_sma", "z_ecc"]].abs().max(axis=1)
    z_plane = r[[f"z_{c}" for c in PLANE]].abs().max(axis=1)
    z_ir = r[["z_intrack", "z_radial"]].abs().max(axis=1)
    r["candidate"] = ((z_inplane >= Z_THRESHOLD) & (z_ir >= Z_THRESHOLD)) | \
                     ((z_plane >= Z_THRESHOLD) & (r.z_cross.abs() >= Z_THRESHOLD))
    return r


def _persists(history, i_before, i_check, r, k_ref, name):
    """Does the change survive into a later set?

    Compares set i_before directly with set i_check (skipping the sets in
    between). A bad single fit reverts, so the old set agrees with the later
    one again. A real burn leaves both the element step and the position miss
    in place. Returns None if set i_check does not exist yet.
    """
    if i_check >= len(history):
        return None
    span = compute_residuals(history.iloc[[i_before, i_check]].reset_index(drop=True)).iloc[0]
    dt = span["dt_hours"] / 24.0
    col = FEATURES[name][0]
    step = span[col] - r.at[k_ref, f"rate_median_{name}"] * dt
    sig_e = r.at[k_ref, f"sig_{name}"] / (r.at[k_ref, "dt_d"] + TAU_D) * (dt + TAU_D)
    elem_ok = abs(step) >= PERSIST_FRACTION * Z_THRESHOLD * sig_e
    pos_ok = False
    for pname, pcol in POSITION.items():
        sig_p = r.at[k_ref, f"sig_{pname}"] / (r.at[k_ref, "dt_d"] + TAU_D) * (dt + TAU_D)
        pos_ok |= abs(span[pcol]) >= PERSIST_FRACTION * Z_THRESHOLD * sig_p
    return bool(elem_ok and pos_ok)


def detect(history):
    """Return (events DataFrame, scored residual frame, dropped outlier epochs)."""
    clean, dropped = drop_outlier_sets(history)
    r = score(clean)
    if r.empty:
        return pd.DataFrame(), r, dropped
    # r row k compares clean sets k and k+1. Adjacent candidate pairs (a burn
    # smeared over two or more fits) become one event.
    groups = []
    for k in r.index[r.candidate]:
        if groups and k == groups[-1][-1] + 1:
            groups[-1].append(k)
        else:
            groups.append([k])
    events = []
    for g in groups:
        k0, k1 = g[0], g[-1]
        before, after = clean.iloc[k0], clean.iloc[k1 + 1]
        k_peak = r.loc[g, "z_elem"].idxmax()
        name = r.at[k_peak, "driver"]
        persist = _persists(clean, k0, k1 + 2, r, k_peak, name)
        if persist is False:
            continue  # change reverted: a bad fit, not a burn
        # Event deltas: J2-corrected, drift-corrected steps summed over the event's pairs.
        d = {f: float((r.loc[g, col] - r.loc[g, f"exp_{f}"]).sum()) for f, (col, _) in FEATURES.items()}
        d_inc = float(r.loc[g, "d_inc_deg"].sum())
        d_raan = float(r.loc[g, "d_raan_resid_deg"].sum())
        # Changing SMA changes the J2 nodal drift rate, which the per-pair J2
        # correction (computed from the pre-burn orbit) does not know about.
        # Remove it, assuming the burn sits mid-window; half the window is the
        # timing uncertainty, carried into the plane-change sigma.
        window_d = float(r.loc[g, "dt_d"].sum())
        d_rate = (_j2_raan_rate_deg_day(before.sma_km + d["sma"], before.eccentricity, before.inclination_deg)
                  - _j2_raan_rate_deg_day(before.sma_km, before.eccentricity, before.inclination_deg))
        extra_node = math.radians(d_rate * window_d / 2)
        o = math.radians(after.raan_deg)
        sin_i = math.sin(math.radians(after.inclination_deg))
        node_tangent = np.array([sin_i * math.cos(o), sin_i * math.sin(o), 0.0])  # d(normal)/d(RAAN)
        dn = np.array([d[c] for c in PLANE]) - node_tangent * extra_node
        d_raan -= math.degrees(extra_node)
        chord = float(np.linalg.norm(dn))
        theta = 2 * math.asin(min(1.0, chord / 2))
        dv_in, dv_pl, dv_tot, dv_sig = delta_v(
            before.sma_km, d["sma"], d["ecc"], theta,
            sigma_sma=float(np.sqrt((r.loc[g, "sig_sma"] ** 2).sum())),
            sigma_ecc=float(np.sqrt((r.loc[g, "sig_ecc"] ** 2).sum())),
        )
        sig_chord = math.hypot(
            float(np.sqrt(sum((r.loc[g, f"sig_{c}"] ** 2).sum() for c in PLANE))),
            sin_i * abs(extra_node))
        dv_pl_sig = _v_circ_ms(before.sma_km) * sig_chord
        # A plane change counts toward the total only if it clears the same
        # detection bar as everything else (normal-vector step z, and 3 sigma
        # including J2 timing error). Otherwise it is reported but treated as
        # fit noise leaking across the burn.
        plane_z = max(float(r.loc[g, f"z_{c}"].abs().max()) for c in PLANE)
        plane_real = plane_z >= Z_THRESHOLD and dv_pl >= 3 * dv_pl_sig
        if plane_real:
            dv_tot = math.hypot(dv_in, dv_pl)
            dv_sig = math.hypot(dv_in * dv_sig, dv_pl * dv_pl_sig) / dv_tot if dv_tot > 0 else dv_sig
        else:
            dv_tot = dv_in
        # Model error (impulsive burn, near-circular, mean vs osculating
        # elements) dominates element noise. Against NASA-stated ISS orbit
        # changes the SMA estimate scatters ~4%, so carry a 5% floor.
        dv_sig = math.hypot(dv_sig, DV_SYSTEMATIC_FRACTION * dv_tot)
        ev = {
            "status": "confirmed" if persist else "provisional",
            "plane_change": int(plane_real),
            "window_start_utc": before.epoch_utc,
            "window_end_utc": after.epoch_utc,
            "confirmed_at_utc": clean.epoch_utc.iloc[k1 + 2] if persist else None,
            "n_pairs": len(g),
            "max_z": float(r.loc[g, "z_elem"].max()),
            "driver": "plane" if name in PLANE else name,
            "d_sma_km": d["sma"], "d_inc_deg": d_inc, "d_raan_deg": d_raan, "d_ecc": d["ecc"],
            "z_sma": float(r.loc[g, "z_sma"].abs().max()), "z_ecc": float(r.loc[g, "z_ecc"].abs().max()),
            "z_plane": plane_z, "plane_change_deg": math.degrees(theta),
            "dv_inplane_ms": dv_in, "dv_plane_ms": dv_pl, "dv_plane_sigma_ms": dv_pl_sig, "dv_total_ms": dv_tot, "dv_sigma_ms": dv_sig,
            "before_perigee_km": before.perigee_alt_km, "before_apogee_km": before.apogee_alt_km,
            "before_inc_deg": before.inclination_deg,
            "after_perigee_km": after.perigee_alt_km, "after_apogee_km": after.apogee_alt_km,
            "after_inc_deg": after.inclination_deg,
            "intrack_resid_km": float(r.loc[g, "res_intrack_km"].abs().max()),
            "z_pos": float(r.loc[g, "z_pos"].max()),
        }
        reg, lab, routine, extras = classify.label(ev, before, after)
        ev.update(regime=reg, label=lab, routine=int(routine), **extras)
        events.append(ev)
    events = pd.DataFrame(events)
    nid = int(history.norad_id.iloc[0])
    campaign_rows = classify.campaigns(events, nid) if len(events) else []
    events.attrs["campaigns"] = campaign_rows
    return events, r, dropped


def _insert(conn, table, row):
    row = {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in row.items()}
    cols = list(row)
    conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 [row[c] for c in cols])


def run(norad_ids, publishable_only=True):
    scope = "publishable" if publishable_only else "all_sources"
    summary = []
    with db.connect() as conn:
        conn.executescript(EVENTS_SCHEMA)
        run_at = db.utcnow_iso()
        for nid in norad_ids:
            h = db.load_history(conn, nid, publishable_only=publishable_only)
            if len(h) < MIN_HISTORY + 2:
                summary.append((nid, len(h), 0, 0, "insufficient history"))
                continue
            ev, r, dropped = detect(h)
            conn.execute("DELETE FROM maneuver_events WHERE norad_id=? AND data_scope=?", (nid, scope))
            conn.execute("DELETE FROM maneuver_campaigns WHERE norad_id=? AND data_scope=?", (nid, scope))
            for e in ev.to_dict("records"):
                e.update(norad_id=nid, data_scope=scope, detected_run_utc=run_at)
                _insert(conn, "maneuver_events", e)
            for c in ev.attrs.get("campaigns", []):
                _insert(conn, "maneuver_campaigns", {**c, "data_scope": scope})
            summary.append((nid, len(h), len(ev), len(dropped), ""))
    return summary


def main():
    global Z_THRESHOLD
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--norad", type=int, nargs="*", help="default: whole watchlist")
    ap.add_argument("--all-sources", action="store_true", help="include internal-only (Space-Track-origin) sets")
    ap.add_argument("--z", type=float, default=Z_THRESHOLD)
    args = ap.parse_args()
    Z_THRESHOLD = args.z
    ids = args.norad or [w["norad_id"] for w in watchlist.load()]
    names = {w["norad_id"]: w["name"] for w in watchlist.load()}
    for nid, n_sets, n_ev, n_drop, note in run(ids, publishable_only=not args.all_sources):
        print(f"{nid:>6}  {names.get(nid, ''):<26} sets {n_sets:>5}  events {n_ev:>3}  outliers dropped {n_drop:>2}  {note}")


if __name__ == "__main__":
    main()
