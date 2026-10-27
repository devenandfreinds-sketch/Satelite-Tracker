# Detection method

## Signals (`tracker/residuals.py`)

For every pair of consecutive element sets for one object:

1. **Propagation residual.** Propagate the older set with SGP4 to the newer set's epoch. Difference the two positions in the newer set's radial / in-track / cross-track (RIC) frame. SGP4 is the only propagator consistent with how TLEs are fitted, so we use it rather than a high-fidelity propagator.
2. **Mean-element steps.** Change in mean semi-major axis and eccentricity, and change in the **orbit-normal vector** after removing the J2 nodal drift. The mean elements come from SGP4's own initialisation, so they are Brouwer mean elements, not osculating ones.

We use the orbit normal instead of inclination and RAAN for plane changes, because RAAN is undefined at zero inclination. A GEO satellite at i ≈ 0.2° can show RAAN flipping by ~180° between fits. Scoring RAAN directly turned that flip into fake 45 m/s burns; the normal vector has no such problem.

## Detector (`tracker/detect.py`)

1. **Drop isolated bad fits.** A set that jumps away from both neighbours, while the neighbours agree with each other, is a bad fit, not a burn.
2. **Per-object noise model.** For each pair, the expected step and the noise scale come from that object's own trailing 40 pairs. The expected step is the median drift rate (for example drag decay) times the gap. The noise grows with the gap between sets, so σ = s·(Δt + 0.1 d), where s is a robust (MAD) scale. On the ISS the scatter of the SMA step follows this closely: about 0.002 km at 1 h, 0.010 km at 15 h, 0.02 km at 30 h. Only past data is used, so every flag is one we could have raised live.
3. **Two signals must agree, and they must agree physically.** An SMA or eccentricity step at z ≥ 10 must come with an in-track or radial miss at z ≥ 10. A plane-change step must come with a cross-track miss at z ≥ 10. Element steps alone are too noisy: TLE fits trade eccentricity against other elements and produce z = 10 to 65 "steps" with almost no position change. A real burn moves the satellite.
4. **Persistence.** Comparing the set before the event directly with the set after next must still show both the element step and the position miss. A bad fit reverts; a burn does not. An event whose next set has not arrived yet is `provisional`.
5. **Merging.** Adjacent flagged pairs (a burn smeared across two or more fits, or a sequence of burns) become one event.
6. **Δv.** We report the minimum impulsive Δv for a near-circular orbit. A tangential burn gives Δa/a = 2Δv/v and Δe = 2Δv/v, so the in-plane part is the larger of the two implied values. A rotation of the orbit plane by θ costs 2v·sin(θ/2). Before computing θ we remove the change in J2 nodal drift caused by the new SMA. Raising the ISS by 4 km speeds its nodal drift by ~0.011°/day, which otherwise shows up as a fake ~1 m/s plane change. Burn timing inside the window is unknown, so this correction assumes mid-window and carries half the window as uncertainty. A plane change enters the total only when it passes the detection bar itself; otherwise it is reported but treated as fit noise leaking across the burn.
7. **Uncertainty.** The 1σ combines element noise with a 5% systematic floor for model error (impulsive burn, near-circular orbit, mean vs osculating elements). The floor comes from the NASA comparison below, which scatters by ~4%.

The noise window keeps every pair, burns included. An earlier version excluded flagged pairs from the window, which seems safer but has a failure mode: when an object's noise genuinely rises, everything gets flagged, the noise estimate freezes, and the detector never recovers. On the ISS with all sources, that produced 97 false events from April 2026 onward. The median and MAD already ignore the occasional burn.

## Labels and campaigns (`tracker/classify.py`)

Each event gets a label describing what the burn did to the orbit, not why. The regime comes from the pre-burn orbit (GEO: 0.9 to 1.1 rev/day and e < 0.05).

- **GEO.** Longitude is the *mean* longitude (node + argument of perigee + mean anomaly − GMST). That removes the daily east-west wobble an inclined or eccentric orbit traces; the instantaneous sub-satellite longitude moves 1 to 3° depending on when each set was fitted. Drift rate is (n − 1.00273791 rev/day) × 360. A satellite is "holding" if |drift| < 0.05°/day.
  - `geo_ew_stationkeeping`: holding before and after, longitude within 0.5°, in-plane, Δv ≤ 1 m/s. **Routine.**
  - `geo_ns_stationkeeping`: holding, within 0.5°, a plane change that moves the inclination vector toward or through zero (cosine ≥ 0.7 with the pull toward i = 0), Δv ≤ 60 m/s. **Routine.**
  - `geo_drift_start`, `geo_drift_stop`, `geo_drift_change`, `geo_relocation`, `geo_plane_change`, `geo_maneuver`: everything else. These are what inspection, rendezvous and repositioning look like.
- **LEO and other.** `altitude_maintenance` (a raise under 10 km in LEO, e.g. drag makeup; **routine**), `orbit_raise`, `orbit_lowering`, `plane_change` (the plane change carries most of the Δv). In eccentric orbits SMA alone misleads, so perigee and apogee decide: `circularization` (perigee up, apogee down, e.g. Cosmos 2589's transfer to GEO) and `eccentricity_raise`.

"Routine" only decides what gets screened and shown first. It is never evidence of benign intent: small altitude tweaks are also how a satellite holds station on another one, so every event for `leo_shadowing` objects is screened regardless.

A **campaign** is a run of non-routine events on one object with gaps of at most 3 days, or a single event the detector merged across three or more fits spanning more than 3 days. Routine events break a run.

## Closest-object search (`tracker/neighbors.py`)

For each non-routine event (and every event on `leo_shadowing` objects), find what the satellite ended up near in the 24 h after the burn.

1. **Screening catalog.** A daily snapshot (`tracker/catalog.py`) of CelesTrak's active payloads (~17k) and the GEO protected zone list (`SPECIAL=gpz-plus`, ~1.7k objects including dead satellites, rocket bodies and debris), plus the watchlist.
2. **Candidates by orbit similarity.** GEO: mean longitude within 2° at the event time, extrapolated from the snapshot with each object's drift rate ("colocated"). Other regimes: overlapping altitude band and orbit planes within 3°, with RAAN carried back to the event time using J2 ("coplanar"). This is the geometry of inspection and shadowing.
3. **Time-matched elements.** The 12 best candidates are screened with element sets from the event's own time: the stored set nearest the event (−3 d to +12 h), else a one-time SatChecker backfill of that object's history. Requests are spaced 1 s apart, retried with backoff, and stop for the run after 5 consecutive failures. A first version without this got cut off by SatChecker after ~340 requests.
4. **Screening.** Both objects are propagated with SGP4 over the 24 h after the event (5 min steps in GEO, 20 s in LEO). We record the minimum range, its time, the range at the first post-burn epoch, the GEO longitude gap, and the neighbour set's age. GEO candidates that were actually more than 2° away at the event time are dropped. An object that stays within 1 km all day is reported as **attached** (docked, or catalogued as part of the same body, like ISS (NAUKA)).
5. **Crossings.** When a snapshot is within a few days of the event (1.5 d LEO, 7 d GEO), every snapshot object in the altitude band is also brute-force screened for close crossings. For historical events only today's snapshot exists, so this covers recent events only.

Limits: CelesTrak has no public list of LEO debris and rocket bodies, so LEO neighbours are active payloads only. Objects launched after the event or decayed since are missed by the candidate step. Ranges inherit public-element errors (about a km for a fresh LEO set, tens of km in GEO) and grow with the neighbour set's age, which is stored with every row.

**First result.** The search independently recovers the SJ-21 / SJ-25 rendezvous: SJ-25 is SJ-21's closest neighbour after every one of SJ-21's burns from June 2025. It made close passes of 0.3 km (2025-06-11) and 1.6 km (2025-06-29) while staying within ~140 to 210 km on most days, then from 2025-07-02 stayed within about 1 km all day, and the two share element sets from August. Other close approaches worth checking: Luch-5X (Olymp-K 2) to within 6.5 km of Intelsat 39 (2026-02-27) and 16.5 km (2026-06-10); Cosmos 2589 within 13 to 63 km of Chinese and Japanese comsats in 2026 (ZhongXing-2E, ZhongXing-11, JCSAT-3A).

## Approach and why

The published TLE maneuver-detection work mostly uses one of two signals:

- **Element time series.** Look for discontinuities in mean SMA, inclination, and so on against a fitted trend. Examples are Kelecy, Hall, Hamada and Stocker, "Satellite Maneuver Detection Using Two-line Element (TLE) Data" (AMOS 2007), and Patera, "Space Event Detection Method" (J. Spacecraft and Rockets, 2008).
- **Propagation residuals.** Propagate each set forward to the next epoch and test the residual against per-object statistics. Lemmens and Krag, "Two-Line-Elements-Based Maneuver Detection Methods for Satellites in Low Earth Orbit" (J. Guidance, Control, and Dynamics, 2014), compares both families and argues for thresholds derived from each object's own TLE noise rather than a fixed global value.

**We require both, with per-object robust thresholds.** This follows the reasoning in Lemmens and Krag. The in-track residual is the most sensitive single signal, because a small SMA error integrates into a large along-track error, but it cannot tell you what kind of burn happened. The mean-element steps give the burn's direction and Δv. A single global threshold cannot work across our watchlist: ISS drag noise, Starlink's frequent small burns, and GEO station-keeping are different regimes.

*These citations are from memory. Pull each paper and confirm the details before citing it publicly.*

## ISS validation (2026-10-27, `scripts/validate_iss.py`)

Data: 1,153 CelesTrak-origin ISS element sets, 2025-06-01 to 2026-10-27, median spacing 9.3 h.

Ground truth: `validation/iss_maneuvers.csv`, 17 burns compiled by reading every page of NASA's Space Station blog archive for the window, independently of the detector. Gap: the blog was dark during the October to November 2025 government shutdown, so a non-Dragon burn in that stretch could be missing.

| Measure | Public data only | All sources (internal) |
|---|---|---|
| NASA burns detected | **17 / 17** | 17 / 17 |
| Events not in NASA list | 5 | 8 |
| Burn date to first set showing it (median / max) | 31 h / 76 h | 30 h / 76 h |
| Burn date to confirmation (median / max) | 44 h / 93 h | 44 h / 93 h |
| SMA change vs NASA-stated change (6 burns) | +0.02 km bias, 0.09 km RMS | +0.04 km bias, 0.10 km RMS |

- **Burn size agrees with NASA.** Where NASA states both apogee and perigee change, the detector's SMA change matches their mean to 0.09 km RMS, with a ratio of 1.01 (`reports/iss_detector_vs_nasa_publishable.png`). Example: on 2026-01-23 NASA reports +3.1 mi apogee and +2.3 mi perigee, a mean of 4.35 km; the detector gives 4.28 km and 2.41 ± 0.12 m/s. The aborted 2025-09-25 Dragon burn (about 3 min 45 s) is detected at 0.49 m/s.
- **The 5 events NASA doesn't list.** 2025-06-19 (SMA +0.52 km, 0.33 m/s) looks like a real orbit change: the step persists, but no public record of a burn was found. The other four are small (0.10 to 0.77 m/s, SMA change ≤ 0.02 km), driven by eccentricity or the orbit normal, and sit near the threshold. The 0.77 m/s one is a claimed plane change, which the ISS does not make, so treat these as likely false alarms. That is about one false alarm every four months on this object.
- **Denser data does not help much.** Adding Space-Track-origin sets cuts latency by only about an hour and adds three near-threshold events. Most of the latency is the time it takes for new tracking to be fitted and published, not the sampling rate.
- **This is an in-sample result.** The ISS window was used while designing the detector (the dual-signal rule, the orbit-normal features, the window fix). The out-of-sample test is ISS burns after 2026-10-27, scored as they happen, plus a second object with published ground truth.
- **The ISS is the easy case.** Its burns are 0.25 to 2.4 m/s against a noise floor near 0.01 m/s.

## Watchlist run (2026-10-27)

The detector ran on all 73 objects over 17 months of public history: 22 events on the ISS, 459 on the GEO set, 11 on the two Cosmos LEO objects, and 207 on the 30 Starlinks. There is no ground truth for these yet, so they are leads, not findings. Checks so far:

- **GEO station-keeping looks right.** TJS-22 shows 22 events of 0.001 to 0.07 m/s, the size of east-west station-keeping. Several TJS satellites show plane changes of about 0.85° (~45 m/s), where the inclination vector swings across zero, the signature of north-south station-keeping. TJS-6 on 2025-06-19 went from 0.50° inclination at node 90° to 0.33° at node 265°, confirmed by a 545 km cross-track miss.
- **SJ-21 and SJ-25 moved as one stack in August 2025.** From 2025-08-04 the two carry identical element sets at the same epochs, meaning they were docked or catalogued as one object. Between 2025-08-05 and 2025-08-12 the stack's inclination stepped from 10.58° to 4.41° in several burns. The largest, between Aug 11 and 12, is a 5.87° plane change of about 315 ± 16 m/s; the earlier steps add roughly another 42 m/s. **This needs checking against published reporting before anyone writes about it.** Shared element sets could also be a catalog artifact.
- **Long orbit-raising campaigns merge into one event.** Cosmos 2589 raised its perigee from 20,400 to 23,100 km over 28 days in Nov–Dec 2025; adjacent flags merged into one 97 m/s event. These should be labelled as campaigns.
- **Thin public data for GSSAP.** SatChecker's CelesTrak-origin archive holds only 1 set each for USA 253 and USA 254 (Space-Track has about 148). The US comparison set will only become publishable as our own daily CelesTrak pulls build up history.

## Accuracy limits that go in every write-up

- Public element sets are accurate to roughly 1 km or worse, depending on regime, object, and fit. Use each object's own noise floor as the error bar, not a nominal TLE accuracy.
- **Latency.** For the ISS, the first set showing a burn arrives a median 31 h after the burn date, and confirmation a median 44 h after. GEO and less-observed objects will be slower.
- **Fit contamination.** A set fitted across a burn blends pre- and post-burn tracking. The burn time is uncertain to roughly the gap between sets, and fits can take a day to settle afterwards.
- **Δv is a minimum.** Real burns are not impulsive, may not be tangential, and may waste propellant; we estimate the smallest Δv consistent with the element change.
- **Missing objects.** Classified US payloads (for example USA 314 and USA 338) have no public elements, so proximity to them cannot be measured from public data.

## Next

- Out-of-sample validation: score new ISS reboosts as they happen, and find a second object with published maneuver records.
- Event write-up template and the public dashboard (milestone 3).
