"""Watchlist tools.

  python -m tracker.watchlist verify              # check every ID against CelesTrak
  python -m tracker.watchlist starlink-sample -n 30 # append a reproducible Starlink sample
"""
import argparse
import csv
import os
import random
from datetime import datetime, timezone
from pathlib import Path

from . import config, db
from .elements import element_row
from .sources import celestrak

FIELDS = ["norad_id", "name", "cospar_id", "category", "operator", "fetch_via", "notes"]


def load(path=None):
    with open(path or config.WATCHLIST_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    for line, r in enumerate(rows, start=2):
        # DictReader files surplus fields under None: an unquoted comma in notes.
        if None in r or None in r.values():
            raise ValueError(f"watchlist.csv line {line}: wrong field count; quote any notes containing commas")
        r["norad_id"] = int(r["norad_id"])
    return rows


def save(rows, path=None):
    # Write a temp file then rename, so a failed write never truncates the watchlist.
    path = Path(path or config.WATCHLIST_PATH)
    tmp = path.with_suffix(".csv.tmp")
    try:
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def verify():
    """Confirm each entry's CelesTrak name/COSPAR ID still match and data is fresh."""
    now = datetime.now(timezone.utc)
    problems = 0
    for r in load():
        if r["fetch_via"] != "catnr":
            continue
        _, recs = celestrak.fetch_catnr(r["norad_id"])
        if not recs:
            print(f"MISSING  {r['norad_id']:>6}  {r['name']}: no public GP data")
            problems += 1
            continue
        o = recs[0]
        age_d = (now - datetime.fromisoformat(o["EPOCH"]).replace(tzinfo=timezone.utc)).total_seconds() / 86400
        flag = "OK"
        if o["OBJECT_ID"] != r["cospar_id"]:
            flag = "MISMATCH"
            problems += 1
        elif age_d > 14:
            flag = "STALE"
        print(f"{flag:<8} {r['norad_id']:>6}  {o['OBJECT_NAME']:<28} {o['OBJECT_ID']}  epoch age {age_d:5.1f} d")
    return problems


def starlink_sample(n, seed):
    """Pick n operational-altitude Starlinks, spread evenly across inclination shells."""
    # Load first: a malformed watchlist should fail before we spend the
    # once-per-2-hours Starlink group download.
    rows = load()
    status, recs = celestrak.fetch_group("starlink")
    if not recs:
        raise SystemExit(f"CelesTrak returned no Starlink data (HTTP {status}); "
                         "a 403 means the group was downloaded <2 h ago, so retry later.")
    # >15 rev/day is roughly the ~550 km operational shells; lower ones are
    # still raising or are deorbiting, which is a different noise regime.
    ops = [o for o in recs if o["MEAN_MOTION"] > 15.0]
    shells = {}
    for o in ops:
        shells.setdefault(round(o["INCLINATION"]), []).append(o)
    rng = random.Random(seed)
    picked = []
    big = sorted((k for k, v in shells.items() if len(v) >= 50), key=lambda k: -len(shells[k]))
    for i in range(n):
        shell = big[i % len(big)]
        picked.append(shells[shell].pop(rng.randrange(len(shells[shell]))))
    have = {r["norad_id"] for r in rows}
    for o in sorted(picked, key=lambda o: o["NORAD_CAT_ID"]):
        if o["NORAD_CAT_ID"] in have:
            continue
        rows.append({
            "norad_id": o["NORAD_CAT_ID"], "name": o["OBJECT_NAME"], "cospar_id": o["OBJECT_ID"],
            "category": "starlink_sample", "operator": "SpaceX", "fetch_via": "group:starlink",
            "notes": f"Random sample seed={seed}, {round(o['INCLINATION'])} deg shell, picked {datetime.now(timezone.utc):%Y-%m-%d}",
        })
    save(rows)
    # Store the picked satellites' current elements now, and log the fetch, so
    # the next `ingest daily` does not need a second group download.
    with db.connect() as conn:
        db.upsert_satellites(conn, rows)
        new = db.insert_elements(conn, [
            element_row(o["NORAD_CAT_ID"], "omm", celestrak.to_raw(o), "celestrak", o["OBJECT_NAME"]) for o in picked
        ])
        db.log_fetch(conn, "celestrak", "GROUP=starlink", status, len(picked), new)
    print(f"added {len(picked)} Starlinks from shells {sorted(big)}; stored {new} element sets")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify")
    sp = sub.add_parser("starlink-sample")
    sp.add_argument("-n", type=int, default=30)
    sp.add_argument("--seed", type=int, default=20261026)
    args = ap.parse_args()
    if args.cmd == "verify":
        raise SystemExit(1 if verify() else 0)
    starlink_sample(args.n, args.seed)


if __name__ == "__main__":
    main()
