# Satelite Tracker

A public tracker that detects satellite maneuvers (thruster burns) from free public orbital data, then publishes analysis of the notable ones.

Everything here is **analysis of public data**. Public element sets (TLE/GP) are accurate to roughly a kilometre or worse, are published hours after the fact, and come from fits that can blur a burn. Nothing this project produces counts as precise tracking.

## Status

| Milestone | State |
|---|---|
| 1. Daily pull + storage for 50 to 100 objects | Built. 43 named objects pulling daily; 30-satellite Starlink sample is one command away (`python -m tracker.watchlist starlink-sample`) once CelesTrak's 2 h window reopens |
| 2. Detector validated on ISS reboosts | Residual engine built. Provisional threshold flags 17/17 NASA-reported burns (Jun 2025 to Oct 2026), with 1 unexplained flag. Real detector and per-object noise model still to come |
| 3. Public dashboard + event write-up template | Not started |

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python -m tracker.ingest daily          # newest CelesTrak set for every watchlist object
.venv/bin/python -m tracker.watchlist verify      # confirm IDs/COSPAR IDs still match CelesTrak
.venv/bin/python -m tracker.ingest backfill --norad 25544 --start 2025-06-01 --end 2026-10-27
.venv/bin/python scripts/validate_iss.py          # residual plots -> reports/
```

Run `daily` once a day (cron/launchd for now; GitHub Actions once the dashboard exists). It is safe to run more often: any query CelesTrak has already served inside its ~2 hour refresh window is skipped. CelesTrak blocks clients that ignore that window.

## Data sources and the publishing rule

| Source | Used for | Stored as `source` | Publishable |
|---|---|---|---|
| [CelesTrak GP](https://celestrak.org/NORAD/documentation/gp-data-formats.php) | Daily pulls; everything public | `celestrak` | yes |
| [SatChecker](https://satchecker.readthedocs.io/en/latest/tools_tle.html) (IAU CPS) historical TLEs | Backfill for validation and noise models | `satchecker:celestrak` / `satchecker:spacetrack` | only the CelesTrak-origin rows |
| [Space-Track.org](https://www.space-track.org) | Deeper internal history (optional) | `spacetrack` | **never** |

Every row in `elements` carries a `publishable` flag set from its origin. Space-Track's user agreement follows the data, so Space-Track-origin rows stay internal even when they arrive through a third-party archive. Anything public must be built with `publishable_only=True` (see `db.load_history`). The SQLite file is git-ignored for the same reason.

Space-Track credentials, if used, come from environment variables only:

```bash
export SPACETRACK_USER=...
export SPACETRACK_PASSWORD=...
.venv/bin/python -m tracker.ingest backfill --source spacetrack --norad 25544 --start 2025-06-01 --end 2026-10-27
```

(The Space-Track client is written but untested until we have an account set up.)

## Repo layout

```
config/watchlist.csv        curated watchlist; NORAD IDs verified against CelesTrak 2026-10-26
tracker/
  config.py                 paths, rate limits, which sources are publishable, env-var credentials
  db.py                     SQLite schema; every element set ever seen is kept, never overwritten
  elements.py               OMM/TLE -> sgp4 Satrec -> flat mean-element row (SMA, perigee, ...)
  residuals.py              consecutive-set residuals (RIC frame) and mean-element steps
  ingest.py                 CLI: daily pull, historical backfill
  watchlist.py              CLI: verify IDs, add a reproducible Starlink sample
  sources/                  celestrak.py, satchecker.py, spacetrack.py
validation/iss_maneuvers.csv  NASA-reported ISS burns (ground truth), each with a nasa.gov URL
scripts/validate_iss.py     ISS residual plots and hit/false-flag report
reports/                    generated figures
docs/METHOD.md              detection method, literature, and accuracy limits
data/                       SQLite database (git-ignored)
```

## Storage

`data/tracker.sqlite`, three tables:

- `satellites`: the watchlist as last loaded.
- `elements`: one row per (object, epoch, source). Keeps the exact element set as received (`raw`, OMM JSON or TLE text) plus parsed mean elements. OMM is the primary format because TLEs cannot hold catalog numbers above 99999, and CelesTrak already issues some (TJS-27A is 100174).
- `fetch_log`: every request, status, and record count, so gaps in coverage are visible.

## Watchlist notes

- IDs come from CelesTrak name searches on 2026-10-26, not from memory. `python -m tracker.watchlist verify` re-checks them.
- **USA 314 and USA 338 have no public element sets.** They are NRO satellites, which CelesTrak does not publish. So the Cosmos 2576 and Cosmos 2588 "shadowing" cases cannot be measured directly from public data. We track the Cosmos side and must rely on published reporting for the US side. That limit goes in every write-up on these cases.
- Cosmos 2558, which shadowed USA 326 in 2022, returns no public data.
- GSSAP comparison set: USA 253, 254, 270, 271, 324, 325.
- Notes marked "verify before citing" are leads, not findings.
