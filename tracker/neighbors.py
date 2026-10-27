"""Closest-object search after each maneuver (method step 4).

  python -m tracker.neighbors [--norad ID ...] [--all-events] [--all-sources]

For each non-routine event, find what the satellite ended up near in the 24 h
after the burn, using element sets from the event's own time:

1. Candidates by orbit similarity, from the catalog snapshot nearest the event
   (tracker.catalog: active payloads + GEO protected zone, plus the watchlist).
     colocated  (GEO)  sub-satellite longitude within COLOCATED_DEG at the
                       event time, extrapolated with each object's drift rate
     coplanar   (other) same altitude band and orbit plane within
                       COPLANAR_DEG at the event time, RAAN carried back with J2
   This is the geometry of inspection and shadowing, which is what matters
   here. Random crossings are covered only by step 3.
2. Time-matched elements for the best MAX_SCREEN candidates: the stored set
   nearest the event, else one SatChecker backfill of that object's history
   (cached in the elements table, so each object is fetched at most once).
3. If a snapshot is within SNAPSHOT_MAX_AGE_D of the event, also brute-force
   screen every snapshot object in the same altitude band ("crossing").

Then both objects are propagated over [event end, +24 h] and the minimum range
and its time recorded. All ranges inherit public-element errors: roughly 1 km
for a fresh LEO set, tens of km for GEO, more as sets age. elements_age_h is
stored with every row so stale comparisons are visible.

Limits: CelesTrak has no public list of LEO debris and rocket bodies, so LEO
neighbours are active payloads only. Objects launched after the event, or
decayed since, are missed by the candidate step.
"""
import argparse
import json
import math
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sgp4.api import SatrecArray
from sgp4.propagation import gstime

from . import catalog, classify, db, watchlist
from .elements import MU_EARTH, satrec_from_raw, valid_rows
from .residuals import _j2_raan_rate_deg_day, orbit_normal
from .sources import celestrak, satchecker

RE_KM = 6378.135
WINDOW_H = 24
COLOCATED_DEG = 2.0
COPLANAR_DEG = 3.0
SHELL_MARGIN_KM = {"geo": 300.0, "leo": 30.0, "other": 100.0}
STEP_S = {"geo": 300, "leo": 20, "other": 60}
SNAPSHOT_MAX_AGE_D = {"geo": 7.0, "leo": 1.5, "other": 3.0}
MAX_SCREEN = 12           # time-matched candidates screened per event
# Categories where even routine-looking burns get screened: for a satellite
# shadowing another, small altitude tweaks are how it holds station on it.
ALWAYS_SCREEN = {"leo_shadowing"}
# The Starlink sample exists for noise testing; its coplanar neighbours are
# hundreds of other Starlinks, so it is skipped unless asked for.
SKIP_BY_DEFAULT = {"starlink_sample"}
KEEP = 5                  # neighbours kept per event and method
HISTORY_START = "2025-06-01"

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_neighbors (
    norad_id          INTEGER NOT NULL,   -- the maneuvering object
    data_scope        TEXT NOT NULL,
    window_end_utc    TEXT NOT NULL,      -- identifies the event in maneuver_events
    method            TEXT NOT NULL,      -- colocated | coplanar | crossing
    neighbor_norad    INTEGER NOT NULL,
    neighbor_name     TEXT,
    min_range_km      REAL NOT NULL,      -- minimum over the 24 h after the event
    max_range_km      REAL,               -- maximum over the same window; < 1 km = attached/docked
    tca_utc           TEXT NOT NULL,      -- time of that minimum
    range_at_end_km   REAL,               -- at the first post-burn epoch
    lon_sep_deg       REAL,               -- GEO only, at the first post-burn epoch
    elements_source   TEXT,               -- where the neighbour's set came from
    elements_age_h    REAL,               -- |neighbour set epoch - event end|
    run_utc           TEXT NOT NULL,
    PRIMARY KEY (norad_id, data_scope, window_end_utc, method, neighbor_norad)
);
"""


def _jd(dt):
    jd = dt.timestamp() / 86400.0 + 2440587.5
    return math.floor(jd), jd - math.floor(jd)


def _shell(mean_motion_rev_day, ecc):
    n = mean_motion_rev_day * 2 * math.pi / 86400.0
    a = (MU_EARTH / n ** 2) ** (1 / 3)
    return a * (1 - ecc) - RE_KM, a * (1 + ecc) - RE_KM, a


def _omm_epoch(o):
    return datetime.fromisoformat(o["EPOCH"]).replace(tzinfo=timezone.utc)


def _lon_at(o, when):
    """GEO longitude of a snapshot object at `when`, extrapolated by drift rate."""
    lon = classify.geo_longitude("omm", celestrak.to_raw(o))
    days = (when - _omm_epoch(o)).total_seconds() / 86400
    return (lon + classify.drift_rate(o["MEAN_MOTION"]) * days + 180) % 360 - 180


def candidates(target, reg, when, snapshot, exclude):
    """Snapshot objects whose orbits resemble the target's at `when`, best first.

    target: history row (post-burn set). Returns [(norad, name, score, omm)].
    """
    per_t, apo_t, _ = _shell(target.mean_motion_rev_day, target.eccentricity)
    m = SHELL_MARGIN_KM[reg]
    out = []
    if reg == "geo":
        lon_t = classify.geo_longitude(target.fmt, target.raw)
    else:
        n_t = orbit_normal(target.inclination_deg, target.raan_deg)
    for nid, o in snapshot.items():
        if nid in exclude:
            continue
        per, apo, a = _shell(o["MEAN_MOTION"], o["ECCENTRICITY"])
        if per > apo_t + m or apo < per_t - m:
            continue
        if reg == "geo":
            if classify.regime(o["MEAN_MOTION"], o["ECCENTRICITY"]) != "geo":
                continue
            score = abs((_lon_at(o, when) - lon_t + 180) % 360 - 180)
            limit = COLOCATED_DEG
        else:
            days = (when - _omm_epoch(o)).total_seconds() / 86400
            raan = o["RA_OF_ASC_NODE"] + _j2_raan_rate_deg_day(a, o["ECCENTRICITY"], o["INCLINATION"]) * days
            cosang = float(np.clip(orbit_normal(o["INCLINATION"], raan) @ n_t, -1, 1))
            score = math.degrees(math.acos(cosang))
            limit = COPLANAR_DEG
        if score <= limit:
            out.append((nid, o.get("OBJECT_NAME"), score, o))
    return sorted(out, key=lambda c: c[2])


def shell_members(target, reg, snapshot, exclude):
    per_t, apo_t, _ = _shell(target.mean_motion_rev_day, target.eccentricity)
    m = SHELL_MARGIN_KM[reg]
    out = []
    for nid, o in snapshot.items():
        if nid in exclude:
            continue
        per, apo, _ = _shell(o["MEAN_MOTION"], o["ECCENTRICITY"])
        if per <= apo_t + m and apo >= per_t - m:
            out.append((nid, o.get("OBJECT_NAME"), o))
    return out


def _nearest_stored(conn, nid, when, publishable_only):
    """Stored set nearest `when` within [-3 d, +0.5 d] (prefer before the event)."""
    lo = (when - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    hi = (when + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%S")
    q = ("SELECT fmt, raw, source, epoch_utc, object_name FROM elements WHERE norad_id=? "
         "AND epoch_utc BETWEEN ? AND ?")
    if publishable_only:
        q += " AND publishable=1"
    rows = conn.execute(q, (nid, lo, hi)).fetchall()
    if not rows:
        return None
    t = when.timestamp()
    return min(rows, key=lambda r: abs(datetime.fromisoformat(r[3].rstrip("Z")).replace(tzinfo=timezone.utc).timestamp() - t))


# SatChecker is a free community service: space requests out, back off on
# errors, and stop asking for the rest of the run if it stops answering.
FETCH_DELAY_S = 1.0
MAX_CONSECUTIVE_FAILURES = 5
_fetch_state = {"failed": set(), "consecutive": 0, "disabled": False}


def _ensure_history(conn, nid, end_day):
    """One SatChecker backfill per object for the whole study period.

    Returns True if a fetch was attempted and succeeded.
    """
    st = _fetch_state
    if st["disabled"] or nid in st["failed"]:
        return False
    if conn.execute("SELECT 1 FROM fetch_log WHERE source='satchecker' AND query LIKE ? AND error IS NULL",
                    (f"NORAD={nid} {HISTORY_START}..%",)).fetchone():
        return False
    query = f"NORAD={nid} {HISTORY_START}..{end_day}"
    for attempt in range(2):
        try:
            status, recs = satchecker.fetch_history(nid, HISTORY_START, end_day)
            rows, _ = valid_rows((nid, "tle", tle, label, name) for label, tle, name in recs)
            new = db.insert_elements(conn, rows)
            db.log_fetch(conn, "satchecker", query, status, len(rows), new)
            conn.commit()
            st["consecutive"] = 0
            time.sleep(FETCH_DELAY_S)
            return True
        except Exception as e:  # a missing neighbour must not stop the run
            err = str(e)[:300]
            time.sleep(FETCH_DELAY_S * 5 * (attempt + 1))
    db.log_fetch(conn, "satchecker", query, error=err)
    conn.commit()
    st["failed"].add(nid)
    st["consecutive"] += 1
    if st["consecutive"] >= MAX_CONSECUTIVE_FAILURES:
        st["disabled"] = True
        print(f"warn: SatChecker failed {MAX_CONSECUTIVE_FAILURES} times in a row; "
              "no more history fetches this run (rerun later to fill gaps)")
    return False


def _ranges(target_sat, others, start, reg):
    """Range (km) from target to each other satrec over the window, plus times."""
    step = STEP_S[reg]
    times = [start + timedelta(seconds=s) for s in range(0, WINDOW_H * 3600 + 1, step)]
    jd = np.array([_jd(t)[0] for t in times], dtype=float)
    fr = np.array([_jd(t)[1] for t in times], dtype=float)
    e0, r0, _ = SatrecArray([target_sat]).sgp4(jd, fr)
    e1, r1, _ = SatrecArray(others).sgp4(jd, fr)
    d = np.linalg.norm(r1 - r0, axis=2)
    d[(e1 != 0) | (e0 != 0)] = np.nan
    return d, times


def screen_event(conn, ev, target, snapshot, snap_day, publishable_only, names):
    """Neighbour rows for one event."""
    reg = ev["regime"]
    when = datetime.fromisoformat(ev["window_end_utc"].rstrip("Z")).replace(tzinfo=timezone.utc)
    target_sat = satrec_from_raw(target.fmt, target.raw)
    exclude = {int(ev["norad_id"])}
    rows = []
    snap_age_d = abs((datetime.strptime(snap_day, "%Y-%m-%d").replace(tzinfo=timezone.utc) - when).total_seconds()) / 86400
    end_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    cands = candidates(target, reg, when, snapshot, exclude)
    method = "colocated" if reg == "geo" else "coplanar"
    matched = []
    for nid, name, score, o in cands:
        if len(matched) >= MAX_SCREEN:
            break
        if snap_age_d <= SNAPSHOT_MAX_AGE_D[reg]:
            sat, src, ep = satrec_from_raw("omm", celestrak.to_raw(o)), "catalog_snapshot", _omm_epoch(o)
        else:
            got = _nearest_stored(conn, nid, when, publishable_only)
            if got is None and _ensure_history(conn, nid, end_day):
                got = _nearest_stored(conn, nid, when, publishable_only)
            if got is None:
                continue
            sat, src = satrec_from_raw(got[0], got[1]), got[2]
            ep = datetime.fromisoformat(got[3].rstrip("Z")).replace(tzinfo=timezone.utc)
        matched.append((nid, name or names.get(nid), sat, src, ep))
    rows += _summarise(ev, target, target_sat, matched, when, reg, method)

    if snap_age_d <= SNAPSHOT_MAX_AGE_D[reg]:
        members = shell_members(target, reg, snapshot, exclude)
        sats = [(nid, name, satrec_from_raw("omm", celestrak.to_raw(o)), "catalog_snapshot", _omm_epoch(o))
                for nid, name, o in members]
        rows += _summarise(ev, target, target_sat, sats, when, reg, "crossing")
    return rows


def _summarise(ev, target, target_sat, matched, when, reg, method):
    if not matched:
        return []
    d, times = _ranges(target_sat, [m[2] for m in matched], when, reg)
    out = []
    lon_t = classify.geo_longitude(target.fmt, target.raw) if reg == "geo" else None
    for idx, (nid, name, sat, src, ep) in enumerate(matched):
        row = d[idx]
        if np.all(np.isnan(row)):
            continue
        k = int(np.nanargmin(row))
        if row[k] < 1e-3:
            continue  # identical elements: a docked partner or a duplicate entry, not a neighbour
        lon_sep = None
        if reg == "geo":
            err, r, _ = sat.sgp4(*_jd(when))
            if err:
                continue
            lon = math.degrees(math.atan2(r[1], r[0]) - gstime(sum(_jd(when))))
            lon_sep = abs((lon - lon_t + 180) % 360 - 180)
            # Candidates were picked from a later catalog extrapolated back in
            # time; keep only those that really were co-located at the event.
            if method == "colocated" and lon_sep > COLOCATED_DEG:
                continue
        out.append({
            "norad_id": int(ev["norad_id"]), "data_scope": ev["data_scope"],
            "window_end_utc": ev["window_end_utc"], "method": method,
            "neighbor_norad": int(nid), "neighbor_name": name,
            "min_range_km": float(row[k]), "max_range_km": float(np.nanmax(row)), "tca_utc": times[k].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "range_at_end_km": float(row[0]) if np.isfinite(row[0]) else None,
            "lon_sep_deg": lon_sep, "elements_source": src,
            "elements_age_h": abs((ep - when).total_seconds()) / 3600,
        })
    out.sort(key=lambda x: x["min_range_km"])
    return out[:KEEP]


def run(norad_ids=None, all_events=False, publishable_only=True, include_starlink=False):
    scope = "publishable" if publishable_only else "all_sources"
    wl = watchlist.load()
    names = {w["norad_id"]: w["name"] for w in wl}
    always = [w["norad_id"] for w in wl if w["category"] in ALWAYS_SCREEN]
    skip = set() if include_starlink or norad_ids else {w["norad_id"] for w in wl if w["category"] in SKIP_BY_DEFAULT}
    cache = {}
    with db.connect() as conn:
        conn.executescript(SCHEMA)
        q = "SELECT * FROM maneuver_events WHERE data_scope=?"
        args = [scope]
        if not all_events:
            q += f" AND (routine=0 OR norad_id IN ({','.join(str(i) for i in always) or 'NULL'}))"
        if norad_ids:
            q += f" AND norad_id IN ({','.join('?' * len(norad_ids))})"
            args += list(norad_ids)
        events = pd.read_sql_query(q + " ORDER BY norad_id, window_end_utc", conn, params=args)
        events = events[~events.norad_id.isin(skip)]
        run_at = db.utcnow_iso()
        n_rows = 0
        for _, ev in events.iterrows():
            tq = "SELECT * FROM elements WHERE norad_id=? AND epoch_utc=?"
            target = pd.read_sql_query(tq + (" AND publishable=1" if publishable_only else ""), conn,
                                       params=(int(ev.norad_id), ev.window_end_utc))
            if target.empty:
                continue
            target = target.iloc[0]
            when = datetime.fromisoformat(ev.window_end_utc.rstrip("Z")).replace(tzinfo=timezone.utc)
            day = catalog.nearest_day(when)
            if day is None:
                raise SystemExit("No catalog snapshot yet: run `python -m tracker.catalog pull` first.")
            if day not in cache:
                cache[day] = catalog.load(day)
                # watchlist objects absent from the lists still count as candidates
                for w in watchlist.load():
                    if w["norad_id"] not in cache[day]:
                        got = conn.execute("SELECT raw FROM elements WHERE norad_id=? AND fmt='omm' "
                                           "ORDER BY epoch_jd DESC LIMIT 1", (w["norad_id"],)).fetchone()
                        if got:
                            cache[day][w["norad_id"]] = json.loads(got[0])
            rows = screen_event(conn, ev.to_dict(), target, cache[day], day, publishable_only, names)
            conn.execute("DELETE FROM event_neighbors WHERE norad_id=? AND data_scope=? AND window_end_utc=?",
                         (int(ev.norad_id), scope, ev.window_end_utc))
            for r in rows:
                r["run_utc"] = run_at
                cols = list(r)
                conn.execute(f"INSERT INTO event_neighbors ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                             [r[c] for c in cols])
            conn.commit()
            n_rows += len(rows)
    return len(events), n_rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--norad", type=int, nargs="*")
    ap.add_argument("--all-events", action="store_true", help="include routine station-keeping events")
    ap.add_argument("--all-sources", action="store_true")
    ap.add_argument("--include-starlink", action="store_true", help="also screen the Starlink noise-test sample")
    args = ap.parse_args()
    n_ev, n_rows = run(args.norad, args.all_events, not args.all_sources, args.include_starlink)
    st = _fetch_state
    print(f"neighbors: {n_ev} events screened, {n_rows} neighbour rows written; "
          f"history fetch failures: {len(st['failed'])}{' (fetching stopped early)' if st['disabled'] else ''}")


if __name__ == "__main__":
    main()
