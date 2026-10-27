"""Daily catalog snapshots used to find what a satellite ended up near.

  python -m tracker.catalog pull      # once a day; also run by `ingest daily`

Two CelesTrak lists, saved as gzipped OMM JSON under data/catalog/:

  active    every active payload (~17k)
  gpz-plus  everything in and near the GEO protected zone, including dead
            satellites, rocket bodies and debris (~1.7k)

Together with the watchlist these are the screening catalog. CelesTrak has no
single public list of LEO debris and rocket bodies, so LEO neighbours are
active payloads only; every write-up must say so.
"""
import argparse
import gzip
import json
from datetime import datetime, timezone

import requests

from . import config, db
from .sources import celestrak

CATALOG_DIR = config.DATA_DIR / "catalog"
LISTS = {"active": {"GROUP": "active"}, "gpz-plus": {"SPECIAL": "gpz-plus"}}


def _path(name, day):
    return CATALOG_DIR / f"{day}_{name}.json.gz"


def pull(force=False):
    """Save today's snapshot of each list, at most once per UTC day."""
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with db.connect() as conn:
        for name, params in LISTS.items():
            out = _path(name, day)
            if out.exists() and not force:
                continue
            resp = requests.get(celestrak.BASE, params={**params, "FORMAT": "JSON"},
                                headers={"User-Agent": config.USER_AGENT}, timeout=180)
            query = "&".join(f"{k}={v}" for k, v in params.items())
            if resp.status_code == 403 and "has not updated" in resp.text:
                db.log_fetch(conn, "celestrak", query, resp.status_code, 0, 0)
                continue
            if resp.status_code != 200 or not resp.text.lstrip().startswith("["):
                db.log_fetch(conn, "celestrak", query, resp.status_code, error=resp.text[:200])
                print(f"catalog {name}: HTTP {resp.status_code}")
                continue
            recs = resp.json()
            tmp = out.with_suffix(".tmp")
            with gzip.open(tmp, "wt") as f:
                json.dump(recs, f)
            tmp.replace(out)
            db.log_fetch(conn, "celestrak", query, resp.status_code, len(recs), len(recs))
            print(f"catalog {name}: {len(recs)} objects -> {out.name}")


def snapshot_days():
    return sorted({p.name.split("_", 1)[0] for p in CATALOG_DIR.glob("*_*.json.gz")})


def load(day):
    """All objects in the snapshot for `day`, keyed by NORAD ID (lists merged)."""
    merged = {}
    for name in LISTS:
        p = _path(name, day)
        if p.exists():
            with gzip.open(p, "rt") as f:
                for o in json.load(f):
                    merged[int(o["NORAD_CAT_ID"])] = o
    return merged


def nearest_day(when):
    """Snapshot day closest to `when` (a datetime), or None if there are none."""
    days = snapshot_days()
    if not days:
        return None
    target = when.date() if hasattr(when, "date") else when
    return min(days, key=lambda d: abs((datetime.strptime(d, "%Y-%m-%d").date() - target).days))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull")
    p.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.cmd == "pull":
        pull(args.force)


if __name__ == "__main__":
    main()
