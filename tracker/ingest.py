"""Pull element sets and store them.

  python -m tracker.ingest daily
      Newest CelesTrak set for every watchlist object, plus the day's catalog
      snapshot (tracker.catalog). Safe to run as often as you like: queries
      repeated inside CelesTrak's ~2 h refresh are skipped.

  python -m tracker.ingest backfill --norad 25544 --start 2025-06-01 --end 2026-10-26 [--source satchecker|spacetrack]
      Historical sets for internal analysis (validation, noise models).
"""
import argparse
import sys
from collections import defaultdict

from . import catalog, config, db, watchlist
from .elements import valid_rows
from .sources import celestrak, satchecker, spacetrack


def _store_omm(conn, recs, wanted, source="celestrak"):
    rows, bad = valid_rows((o["NORAD_CAT_ID"], "omm", celestrak.to_raw(o), source, o.get("OBJECT_NAME"))
                           for o in recs if wanted is None or o["NORAD_CAT_ID"] in wanted)
    if bad:
        print(f"warn: rejected {bad} malformed element sets", file=sys.stderr)
    return len(rows), db.insert_elements(conn, rows)


def daily(force=False):
    sats = watchlist.load()
    by_route = defaultdict(list)
    for s in sats:
        by_route[s["fetch_via"]].append(s["norad_id"])

    total_new, failures = 0, 0
    with db.connect() as conn:
        db.upsert_satellites(conn, sats)
        for route, ids in by_route.items():
            # One request per group (e.g. all of Starlink) beats N single requests.
            queries = [(f"GROUP={route.split(':', 1)[1]}", set(ids))] if route.startswith("group:") \
                else [(f"CATNR={i}", {i}) for i in ids]
            for query, wanted in queries:
                age = db.hours_since_last_fetch(conn, "celestrak", query)
                if not force and age is not None and age < config.CELESTRAK_MIN_REFETCH_HOURS:
                    continue
                key, val = query.split("=", 1)
                try:
                    status, recs = celestrak.fetch_group(val) if key == "GROUP" else celestrak.fetch_catnr(val)
                    n, new = _store_omm(conn, recs, wanted)
                    db.log_fetch(conn, "celestrak", query, status, n, new)
                    total_new += new
                    if status == 403:
                        print(f"skip: {query} unchanged since last download", file=sys.stderr)
                    elif n == 0:
                        print(f"warn: {query} returned no data", file=sys.stderr)
                except Exception as e:  # keep going; one bad object must not stop the run
                    failures += 1
                    db.log_fetch(conn, "celestrak", query, error=str(e)[:500])
                    print(f"error: {query}: {e}", file=sys.stderr)
                conn.commit()
        n_stored = conn.execute("SELECT COUNT(*) FROM elements").fetchone()[0]
    print(f"daily: {total_new} new element sets, {failures} failed queries, {n_stored} stored in total")
    try:
        catalog.pull()  # once per UTC day; screening catalog for closest-object search
    except Exception as e:
        failures += 1
        print(f"error: catalog snapshot: {e}", file=sys.stderr)
    return failures


def backfill(norad_id, start, end, source):
    with db.connect() as conn:
        query = f"NORAD={norad_id} {start}..{end}"
        if source == "satchecker":
            status, recs = satchecker.fetch_history(norad_id, start, end)
            rows, bad = valid_rows((norad_id, "tle", tle, label, name) for label, tle, name in recs)
        else:
            status, recs = spacetrack.fetch_history(norad_id, start, end)
            rows, bad = valid_rows((norad_id, "omm", celestrak.to_raw(o), "spacetrack", o.get("OBJECT_NAME")) for o in recs)
        new = db.insert_elements(conn, rows)
        db.log_fetch(conn, source, query, status, len(rows), new)
    by_src = defaultdict(int)
    for r in rows:
        by_src[r["source"]] += 1
    print(f"backfill {norad_id}: {len(rows)} sets received ({dict(by_src)}), {new} new"
          + (f", {bad} malformed sets rejected" if bad else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daily")
    d.add_argument("--force", action="store_true", help="ignore the 2 h refetch guard")
    b = sub.add_parser("backfill")
    b.add_argument("--norad", type=int, required=True)
    b.add_argument("--start", required=True, help="YYYY-MM-DD")
    b.add_argument("--end", required=True, help="YYYY-MM-DD")
    b.add_argument("--source", choices=["satchecker", "spacetrack"], default="satchecker")
    args = ap.parse_args()
    if args.cmd == "daily":
        raise SystemExit(1 if daily(args.force) else 0)
    backfill(args.norad, args.start, args.end, args.source)


if __name__ == "__main__":
    main()
