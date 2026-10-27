# Satelite Tracker

A public tracker that detects satellite maneuvers (thruster burns) from free public orbital data, then publishes analysis of the notable ones.

Everything here is **analysis of public data**. Public element sets (TLE/GP) are accurate to roughly a kilometre or worse, are published hours after the fact, and come from fits that can blur a burn. Nothing this project produces counts as precise tracking.

## Status

| Milestone | State |
|---|---|
| 1. Daily pull + storage for 50 to 100 objects | Done. 73 objects: 43 named targets plus a 30-satellite Starlink sample (seed 20261026, spread across the 43, 53, 70 and 97 degree shells) |
| 2. Detector validated on ISS reboosts | Done (in-sample). Per-object noise model and dual-signal detector find 17/17 NASA-reported burns (Jun 2025 to Oct 2026), with 5 events NASA doesn't list; burn size agrees with NASA to 0.09 km RMS. Events are labelled (station-keeping, drift start/stop, relocation, plane change...), grouped into campaigns, and screened for nearby objects |
| 3. Public dashboard + event write-up template | Dashboard built (`site/`, static, runs locally). Not deployed yet; write-up template not started |

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python -m tracker.ingest daily          # newest CelesTrak set for every watchlist object
.venv/bin/python -m tracker.watchlist verify      # confirm IDs/COSPAR IDs still match CelesTrak
.venv/bin/python -m tracker.ingest backfill --norad 25544 --start 2025-06-01 --end 2026-10-27
.venv/bin/python -m tracker.detect                # maneuver events, labels, campaigns
.venv/bin/python -m tracker.neighbors             # closest objects after each non-routine event
.venv/bin/python scripts/validate_iss.py          # ISS validation report and figures -> reports/
.venv/bin/python scripts/build_site.py            # export publishable data for the dashboard -> site/data/
.venv/bin/python -m pytest tests                  # tests on synthetic orbits and burns
```

## Dashboard

`site/` is a static page: no server code, no build step. `scripts/build_site.py` exports publishable data only (it refuses to export anything else) into `site/data/`, and the page reads those JSON files. To view it locally:

```bash
python3 -m http.server 8000 --directory site
```

Then open http://localhost:8000. It has a filterable, sortable event list (date range, satellite, group, burn type, minimum delta-v, routine on/off), a page per event (delta-v with uncertainty, before/after orbit, closest objects, orbit charts with the burn windows shaded), a page per satellite, campaigns, and the ISS validation. Links are shareable (`#/event/...`, `#/sat/49330`).

The committed `site/data/` is a snapshot from 2026-10-27. When we deploy (GitHub Pages), a scheduled workflow should rebuild and publish the data as a Pages artifact instead of committing 3 MB a day.

To backfill 17 months of history for the whole watchlist (about 10 minutes):

```bash
for id in $(tail -n +2 config/watchlist.csv | cut -d, -f1); do
  .venv/bin/python -m tracker.ingest backfill --norad $id --start 2025-06-01 --end 2026-10-28
done
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
  residuals.py              consecutive-set residuals (RIC frame), mean-element and orbit-normal steps
  detect.py                 CLI: per-object noise model, maneuver events, delta-v estimates
  classify.py               event labels (GEO station-keeping, drift start/stop, ...) and campaigns
  catalog.py                daily screening catalog: CelesTrak active payloads + GEO protected zone
  neighbors.py              CLI: closest objects after each event, with time-matched element sets
  validation.py             matching events to NASA-reported ISS burns
  ingest.py                 CLI: daily pull, historical backfill
  watchlist.py              CLI: verify IDs, add a reproducible Starlink sample
  sources/                  celestrak.py, satchecker.py, spacetrack.py
validation/iss_maneuvers.csv  NASA-reported ISS burns (ground truth), each with a nasa.gov URL
scripts/validate_iss.py     ISS validation: hit rate, unmatched events, latency, size vs NASA
scripts/build_site.py       publishable-only export for the dashboard
site/                       static dashboard (index.html, app.js, style.css, data/)
tests/                      pytest: synthetic burns, bad fits, delta-v, watchlist safeguards
reports/                    generated figures
docs/METHOD.md              detection method, literature, and accuracy limits
data/                       SQLite database (git-ignored)
```

## Storage

`data/tracker.sqlite`, six tables (plus daily catalog snapshots in `data/catalog/`):

- `satellites`: the watchlist as last loaded.
- `elements`: one row per (object, epoch, source). Keeps the exact element set as received (`raw`, OMM JSON or TLE text) plus parsed mean elements. OMM is the primary format because TLEs cannot hold catalog numbers above 99999, and CelesTrak already issues some (TJS-27A is 100174).
- `fetch_log`: every request, status, and record count, so gaps in coverage are visible.
- `maneuver_events`: detector output per object and data scope (`publishable` or `all_sources`): burn window, element changes, z-scores, delta-v with 1-sigma, before/after orbit, label, routine flag, GEO longitude and drift, campaign. Rebuilt on each `tracker.detect` run.
- `maneuver_campaigns`: runs of non-routine burns on one object.
- `event_neighbors`: closest objects in the 24 h after each screened event, with range, time, and how old the neighbour's element set was.

## Watchlist notes

- IDs come from CelesTrak name searches on 2026-10-26, not from memory. `python -m tracker.watchlist verify` re-checks them.
- **USA 314 and USA 338 have no public element sets.** They are NRO satellites, which CelesTrak does not publish. So the Cosmos 2576 and Cosmos 2588 "shadowing" cases cannot be measured directly from public data. We track the Cosmos side and must rely on published reporting for the US side. That limit goes in every write-up on these cases.
- Cosmos 2558, which shadowed USA 326 in 2022, returns no public data.
- GSSAP comparison set: USA 253, 254, 270, 271, 324, 325. Public history is thin: SatChecker's CelesTrak-origin archive has 1 set each for USA 253 and 254, so this set only becomes usable publicly as our daily pulls accumulate.
- Notes marked "verify before citing" are leads, not findings.
