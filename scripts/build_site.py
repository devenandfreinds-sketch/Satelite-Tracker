"""Export publishable data for the static dashboard in site/.

  python scripts/build_site.py

Writes site/data/index.json (satellites, events, campaigns, neighbours,
ISS validation) and site/data/sat/<norad>.json (orbit history per watchlist
object). Only publishable (CelesTrak-origin) data is read, and the script
refuses to run otherwise, so nothing from Space-Track can reach the site.
"""
import json
import math
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tracker import classify, config, db, detect, neighbors, validation, watchlist  # noqa: E402
from tracker.elements import satrec_from_raw  # noqa: E402

SITE = config.REPO_ROOT / "site"
OUT = SITE / "data"
SCOPE = "publishable"
MAX_POINTS = 1500  # per satellite; enough for a 17-month chart, small enough to load fast


def _clean(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return None if not math.isfinite(v) else round(float(v), 6)
    return v


def _records(df):
    return [{k: _clean(v) for k, v in r.items()} for r in df.to_dict("records")]


def series(conn, nid, keep_epochs):
    h = db.load_history(conn, nid, publishable_only=True)
    if h.empty:
        return None
    assert (h.publishable == 1).all()
    stride = max(1, math.ceil(len(h) / MAX_POINTS))
    idx = set(range(0, len(h), stride)) | {len(h) - 1}
    idx |= set(h.index[h.epoch_utc.isin(keep_epochs)])  # never drop the sets either side of a burn
    h = h.loc[sorted(idx)]
    geo = classify.regime(h.mean_motion_rev_day.median(), h.eccentricity.median()) == "geo"
    out = {
        "t": [int(x.timestamp()) for x in h.epoch],
        "sma_km": [round(x, 3) for x in h.sma_km],
        "perigee_km": [round(x, 2) for x in h.perigee_alt_km],
        "apogee_km": [round(x, 2) for x in h.apogee_alt_km],
        "inc_deg": [round(x, 4) for x in h.inclination_deg],
        "regime": "geo" if geo else "other",
    }
    if geo:
        out["lon_deg"] = [round(classify.geo_longitude(f, r), 3) for f, r in zip(h.fmt, h.raw)]
    return out


GAP_BRIDGE_DAYS = 5  # days without a set that the chart line may bridge


def _bridge(vals, max_gap):
    """Linearly fill runs of up to max_gap missing days between two known
    values, so sparse tracking doesn't render as a dashed line. Longer gaps,
    and the blanks left at the 180 deg seam, stay empty."""
    out = list(vals)
    known = [i for i, v in enumerate(vals) if v is not None]
    for a, b in zip(known, known[1:]):
        if 1 < b - a <= max_gap + 1 and abs(vals[b] - vals[a]) < 180:
            for i in range(a + 1, b):
                out[i] = round(vals[a] + (vals[b] - vals[a]) * (i - a) / (b - a), 3)
    return out


def geo_belt(conn, wl, ev):
    """Daily mean longitude for every GEO watchlist object, on one shared day axis.

    One value per UTC day (the day's last set), so 40 objects share one x-axis.
    Jumps across the 180 deg seam become gaps instead of lines across the chart.
    """
    per_sat = {}
    for w in wl:
        h = db.load_history(conn, w["norad_id"], publishable_only=True)
        if h.empty or classify.regime(h.mean_motion_rev_day.median(), h.eccentricity.median()) != "geo":
            continue
        h = h[h.mean_motion_rev_day.between(0.9, 1.1)]  # skip any transfer-orbit sets
        h["day"] = h.epoch.dt.floor("D")
        last = h.groupby("day").tail(1)
        per_sat[w["norad_id"]] = {d: classify.geo_longitude(f, r) for d, f, r in zip(last.day, last.fmt, last.raw)}
    if not per_sat:
        return None
    days = pd.date_range(min(min(v) for v in per_sat.values()), max(max(v) for v in per_sat.values()), freq="D")
    series = []
    for w in wl:
        if w["norad_id"] not in per_sat:
            continue
        vals, prev = [], None
        for d in days:
            v = per_sat[w["norad_id"]].get(d)
            # blank the point that crosses the seam so the line breaks there
            wrapped = v is not None and prev is not None and abs(v - prev) > 180
            vals.append(None if v is None or wrapped else round(v, 3))
            prev = v if v is not None else prev
        series.append({"norad_id": w["norad_id"], "name": w["name"], "operator": w["operator"],
                       "lon": _bridge(vals, GAP_BRIDGE_DAYS)})
    geo_ids = set(per_sat)
    burns = [{"norad_id": int(e.norad_id), "t": int(pd.Timestamp(e.window_end_utc).timestamp()),
              "lon": _clean(e.lon_after_deg), "label": e.label, "key": e.key, "dv": _clean(e.dv_total_ms)}
             for e in ev.itertuples() if e.norad_id in geo_ids and not e.routine and e.lon_after_deg is not None]
    return {"t": [int(d.timestamp()) for d in days], "series": series, "burns": burns}


# ---------- 3D globe ----------
RE_UNIT_KM = 6378.137      # positions are exported in Earth radii
LIVE_HOURS, LIVE_STEP_S = 24, 120
# GEO moves slowly, so long windows at coarse steps; LEO needs fine steps, so short windows.
REPLAY_WINDOW = {"geo": (6, 24, 300), "leo": (3, 12, 60), "other": (6, 24, 300)}  # hours before, after, step s
REPLAY_MAX_EVENTS, REPLAY_PER_OBJECT, REPLAY_NEIGHBOURS, REPLAY_MAX_KM = 30, 3, 5, 300.0
REPLAY_SKIP = {"validation"}  # the ISS's only "neighbour" is its own Nauka module


def _jd_arrays(times):
    jd = np.array([t.timestamp() / 86400.0 + 2440587.5 for t in times])
    whole = np.floor(jd)
    return whole, jd - whole


def _positions(sat, times):
    """TEME positions in Earth radii, rounded to ~1 km; None where SGP4 fails."""
    jd, fr = _jd_arrays(times)
    err, r, _ = sat.sgp4_array(jd, fr)
    out = []
    for e, p in zip(err, r):
        out.append(None if e else [round(float(c) / RE_UNIT_KM, 4) for c in p])
    return out


def _orbit_path(sat, n=240):
    """One full revolution from the set's epoch, for drawing the orbit line."""
    period_min = 2 * math.pi / sat.no_kozai
    start = datetime(2000, 1, 1, tzinfo=timezone.utc) + pd.Timedelta(days=(sat.jdsatepoch + sat.jdsatepochF) - 2451544.5)
    times = [start + pd.Timedelta(minutes=period_min * k / n) for k in range(n + 1)]
    return [p for p in _positions(sat, times) if p is not None]


def globe_live(conn, wl):
    """Every watchlist object over the next 24 h from its newest publishable set."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    t0 = now - pd.Timedelta(hours=1)
    times = [t0 + pd.Timedelta(seconds=LIVE_STEP_S * k) for k in range(LIVE_HOURS * 3600 // LIVE_STEP_S + 1)]
    objs = []
    for w in wl:
        got = conn.execute("SELECT fmt, raw, epoch_utc FROM elements WHERE norad_id=? AND publishable=1 "
                           "ORDER BY epoch_jd DESC LIMIT 1", (w["norad_id"],)).fetchone()
        if not got:
            continue
        sat = satrec_from_raw(got[0], got[1])
        objs.append({"norad_id": w["norad_id"], "name": w["name"], "category": w["category"],
                     "operator": w["operator"], "epoch_utc": got[2],
                     "pos": _positions(sat, times), "orbit": _orbit_path(sat)})
    return {"t0": int(t0.timestamp()), "step_s": LIVE_STEP_S, "n": len(times), "objects": objs}


def globe_replays(conn, ev, nb, categories):
    """Target plus closest neighbours around each event with a close approach."""
    # Rank by approaches, not attachments: a docked pair sitting together for a
    # day has nothing to replay, while the approach before docking does.
    close = nb[(nb.min_range_km <= REPLAY_MAX_KM) & ~(nb.max_range_km <= 1.0)]
    best = close.groupby("key").min_range_km.min().sort_values()
    out, per_object = [], {}
    for key, closest in best.items():
        if len(out) >= REPLAY_MAX_EVENTS:
            break
        e = ev[ev.key == key].iloc[0]
        nid = int(e.norad_id)
        if categories.get(nid) in REPLAY_SKIP or per_object.get(nid, 0) >= REPLAY_PER_OBJECT:
            continue
        per_object[nid] = per_object.get(nid, 0) + 1
        before_h, after_h, step = REPLAY_WINDOW[e.regime]
        end = pd.Timestamp(e.window_end_utc).tz_convert("UTC")
        # start shortly before the burn window, but never more than a day before its end
        start = max(pd.Timestamp(e.window_start_utc).tz_convert("UTC") - pd.Timedelta(hours=before_h),
                    end - pd.Timedelta(hours=24))
        n = int(((end + pd.Timedelta(hours=after_h)) - start).total_seconds() // step) + 1
        times = [(start + pd.Timedelta(seconds=step * k)).to_pydatetime() for k in range(n)]
        sets = {r[0]: r for r in conn.execute(
            "SELECT epoch_utc, fmt, raw FROM elements WHERE norad_id=? AND publishable=1 AND epoch_utc IN (?, ?)",
            (int(e.norad_id), e.window_start_utc, e.window_end_utc))}
        if len(sets) < 2:
            continue
        before = satrec_from_raw(*sets[e.window_start_utc][1:])
        after = satrec_from_raw(*sets[e.window_end_utc][1:])
        # Two full tracks; the page switches from one to the other exactly at the
        # burn, so interpolation never mixes the two element sets.
        objs = [{"norad_id": int(e.norad_id), "name": None, "role": "target",
                 "pos": _positions(before, times), "pos_after": _positions(after, times),
                 "orbit_before": _orbit_path(before), "orbit_after": _orbit_path(after)}]
        when = end.to_pydatetime()
        rows = nb[nb.key == key].sort_values("min_range_km").drop_duplicates("neighbor_norad").head(REPLAY_NEIGHBOURS)
        for r in rows.itertuples():
            got = neighbors._nearest_stored(conn, int(r.neighbor_norad), when, True)
            if not got:
                continue
            sat = satrec_from_raw(got[0], got[1])
            objs.append({"norad_id": int(r.neighbor_norad), "name": r.neighbor_name, "role": "neighbor",
                         "min_range_km": _clean(r.min_range_km), "pos": _positions(sat, times), "orbit": _orbit_path(sat)})
        rid = f"{int(e.norad_id)}-{end:%Y%m%dT%H%M}"
        (OUT / "globe" / f"{rid}.json").write_text(json.dumps(
            {"id": rid, "key": key, "t0": int(start.timestamp()), "step_s": step, "n": n,
             "burn_t": int(end.timestamp()), "objects": objs}, separators=(",", ":")))
        best_row = close[close.key == key].sort_values("min_range_km").iloc[0]
        out.append({"id": rid, "key": key, "norad_id": int(e.norad_id), "label": e.label,
                    "dv_total_ms": _clean(e.dv_total_ms), "closest_km": _clean(closest),
                    "closest_name": best_row.neighbor_name, "tca_utc": best_row.tca_utc,
                    "window_end_utc": e.window_end_utc})
    return out


def main():
    (OUT / "sat").mkdir(parents=True, exist_ok=True)
    wl = watchlist.load()
    with db.connect() as conn:
        conn.executescript(detect.EVENTS_SCHEMA)
        ev = pd.read_sql_query("SELECT * FROM maneuver_events WHERE data_scope=? ORDER BY window_end_utc",
                               conn, params=(SCOPE,))
        camp = pd.read_sql_query("SELECT * FROM maneuver_campaigns WHERE data_scope=?", conn, params=(SCOPE,))
        try:
            nb = pd.read_sql_query("SELECT * FROM event_neighbors WHERE data_scope=?", conn, params=(SCOPE,))
        except Exception:
            nb = pd.DataFrame(columns=["norad_id", "window_end_utc"])
        if (ev.data_scope != SCOPE).any() or (nb.get("data_scope", pd.Series(dtype=str)) != SCOPE).any():
            raise SystemExit("refusing to export non-publishable rows")

        ev["key"] = ev.norad_id.astype(str) + "|" + ev.window_end_utc
        nb["key"] = nb.norad_id.astype(str) + "|" + nb.window_end_utc
        orphans = ~nb.key.isin(ev.key)
        if orphans.any():
            print(f"warn: ignoring {int(orphans.sum())} neighbour rows whose event no longer exists "
                  "(rerun tracker.neighbors)", file=sys.stderr)
        nb = nb[~orphans]
        keep_cols = ["key", "norad_id", "status", "window_start_utc", "window_end_utc", "confirmed_at_utc",
                     "n_pairs", "regime", "label", "routine", "campaign_id", "driver", "max_z", "z_pos",
                     "d_sma_km", "d_inc_deg", "d_ecc", "plane_change", "plane_change_deg",
                     "dv_inplane_ms", "dv_plane_ms", "dv_total_ms", "dv_sigma_ms",
                     "before_perigee_km", "before_apogee_km", "before_inc_deg",
                     "after_perigee_km", "after_apogee_km", "after_inc_deg",
                     "lon_before_deg", "lon_after_deg", "drift_before_deg_day", "drift_after_deg_day"]
        events = _records(ev[keep_cols])
        nb_cols = ["key", "method", "neighbor_norad", "neighbor_name", "min_range_km", "max_range_km",
                   "tca_utc", "range_at_end_km", "lon_sep_deg", "elements_age_h"]
        neighbours = {}
        for r in _records(nb[[c for c in nb_cols if c in nb.columns]].sort_values("min_range_km")):
            neighbours.setdefault(r.pop("key"), []).append(r)

        sats = []
        for w in wl:
            nid = w["norad_id"]
            mine = ev[ev.norad_id == nid]
            keep = set(mine.window_start_utc) | set(mine.window_end_utc)
            s = series(conn, nid, keep)
            if s is not None:
                (OUT / "sat" / f"{nid}.json").write_text(json.dumps(s, separators=(",", ":")))
            sats.append({**{k: w[k] for k in ("norad_id", "name", "cospar_id", "category", "operator", "notes")},
                         "n_sets": len(s["t"]) if s else 0, "regime": s["regime"] if s else None,
                         "has_series": s is not None,
                         "n_events": int(len(mine)), "n_notable": int((mine.routine == 0).sum()),
                         "last_epoch": datetime.fromtimestamp(s["t"][-1], timezone.utc).isoformat() if s else None})

        # ISS validation, same rule as scripts/validate_iss.py
        iss = ev[ev.norad_id == 25544].copy()
        iss["start"] = pd.to_datetime(iss.window_start_utc, utc=True)
        iss["end"] = pd.to_datetime(iss.window_end_utc, utc=True)
        hist = db.load_history(conn, 25544, publishable_only=True)
        truth, unmatched = validation.match(iss, validation.load_truth(hist.epoch.min(), hist.epoch.max()))
        vrows = []
        for _, t in truth.iterrows():
            e = iss.loc[t.event] if pd.notna(t.event) else None
            nasa_km = ((t.d_apogee_mi + t.d_perigee_mi) / 2 * validation.MI_KM
                       if pd.notna(t.d_apogee_mi) and pd.notna(t.d_perigee_mi) else None)
            vrows.append({"burn_date": t.burn_date, "vehicle": t.vehicle, "duration": t.duration,
                          "source_url": t.source_url, "nasa_mean_alt_change_km": _clean(nasa_km),
                          "event_key": e.key if e is not None else None,
                          "d_sma_km": _clean(e.d_sma_km) if e is not None else None,
                          "dv_total_ms": _clean(e.dv_total_ms) if e is not None else None,
                          "dv_sigma_ms": _clean(e.dv_sigma_ms) if e is not None else None,
                          "lag_h": _clean((e.end - t.burn).total_seconds() / 3600) if e is not None else None})

        belt = geo_belt(conn, wl, ev)
        if belt:
            (OUT / "geo_belt.json").write_text(json.dumps(belt, separators=(",", ":")))

        (OUT / "globe").mkdir(exist_ok=True)
        for old in (OUT / "globe").glob("*.json"):
            old.unlink()
        live = globe_live(conn, wl)
        (OUT / "globe" / "live.json").write_text(json.dumps(live, separators=(",", ":")))
        replays = globe_replays(conn, ev, nb, {w["norad_id"]: w["category"] for w in wl})
        (OUT / "globe" / "replays.json").write_text(json.dumps(replays, separators=(",", ":")))
        print(f"globe: {len(live['objects'])} live objects, {len(replays)} event replays")

    index = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_scope": SCOPE,
        "satellites": sats,
        "events": events,
        "campaigns": _records(camp.drop(columns=["data_scope"])),
        "neighbors": neighbours,
        "validation": {"burns": vrows, "detected": int(truth.event.notna().sum()), "total": int(len(truth)),
                       "unmatched_event_keys": list(unmatched.key)},
    }
    (OUT / "index.json").write_text(json.dumps(index, separators=(",", ":")))
    # publishable figures only; all_sources figures contain Space-Track-derived data
    (SITE / "img").mkdir(exist_ok=True)
    for fig in config.REPORTS_DIR.glob("*_publishable.png"):
        shutil.copyfile(fig, SITE / "img" / fig.name)
    size = sum(p.stat().st_size for p in OUT.rglob("*.json")) / 1e6
    print(f"site data: {len(sats)} satellites, {len(events)} events, {len(index['campaigns'])} campaigns, "
          f"{sum(len(v) for v in neighbours.values())} neighbour rows, {size:.1f} MB -> {OUT}")


if __name__ == "__main__":
    main()
