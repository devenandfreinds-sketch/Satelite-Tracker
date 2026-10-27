# Detection method

## What we compute (built)

For every pair of consecutive element sets for one object (`tracker/residuals.py`):

1. **Propagation residual.** Propagate the older set with SGP4 to the newer set's epoch. Difference the two positions in the newer set's radial / in-track / cross-track (RIC) frame. SGP4 is the only propagator consistent with how TLEs are fitted, so we use it rather than a high-fidelity propagator.
2. **Mean-element steps.** Change in mean semi-major axis, eccentricity, inclination, and RAAN. RAAN has the J2 secular drift removed first. The mean elements come from SGP4's own initialisation, so they are Brouwer mean elements, not osculating ones.

A burn appears as an in-track residual far above the object's normal scatter, with a step in a mean element. A prograde burn steps SMA up. A plane change steps inclination or RAAN. Drag alone gives a slow, smooth SMA decay.

## Approach and why

The published TLE maneuver-detection work mostly uses one of two signals:

- **Element time series.** Look for discontinuities in mean SMA, inclination, and so on against a fitted trend. Examples are Kelecy, Hall, Hamada and Stocker, "Satellite Maneuver Detection Using Two-line Element (TLE) Data" (AMOS 2007), and Patera, "Space Event Detection Method" (J. Spacecraft and Rockets, 2008).
- **Propagation residuals.** Propagate each set forward to the next epoch and test the residual against per-object statistics. Lemmens and Krag, "Two-Line-Elements-Based Maneuver Detection Methods for Satellites in Low Earth Orbit" (J. Guidance, Control, and Dynamics, 2014), compares both families and argues for thresholds derived from each object's own TLE noise rather than a fixed global value.

**We use both, with per-object robust thresholds.** This follows the reasoning in Lemmens and Krag. The in-track residual is the most sensitive single signal, because a small SMA error integrates into a large along-track error. It cannot tell you what kind of burn happened, though. The mean-element steps give the burn's direction and a delta-v estimate. A single global threshold cannot work across our watchlist: ISS drag noise, Starlink's frequent small burns, and GEO station-keeping are different regimes. So the threshold for each object comes from its own recent residuals (rolling median and MAD), not from a constant.

*These citations are from memory. Pull each paper and confirm the details before citing it publicly.*

## ISS validation so far (2026-10-26)

Data: 1,152 CelesTrak-origin ISS element sets, 2025-06-01 to 2026-10-26, median spacing 9.3 h.

Ground truth: `validation/iss_maneuvers.csv`, 17 burns compiled by reading every page of NASA's Space Station blog archive for the window, independently of the detector. Gap: the blog was dark during the Oct to Nov 2025 government shutdown, so a non-Dragon burn in that stretch could be missing.

- Quiet-pair in-track residual: median 0.20 km, 95th percentile 1.7 km, 99th percentile 3.8 km. Radial 95th percentile 0.15 km.
- **Hit rate: 17/17 NASA-reported burns flagged** by the provisional rule (|in-track| > 15 km and SMA step > +0.25 km). That includes the 2025-09-25 Dragon burn that was aborted after about 3 min 45 s.
- **One unexplained flag in 17 months:** 2025-06-19, in-track 33 km, SMA +0.48 km. The SMA step persists afterwards, which points to a real orbit change rather than a bad element set, but neither NASA nor a quick search shows a burn. It stays open. It could be a Russian-segment maneuver NASA did not blog about, or an artifact.
- A flag counts as a hit when the burn's calendar day falls between the two element sets being compared. The time from burn to the first set showing it ran 10 to 45 h, and 74 to 76 h twice, when CelesTrak had a 62 to 67 h gap in element sets.
- Caveat: the ISS is the easy case. Its burns are big (0.4 to 4 km in SMA) against low noise. Starlink and GEO will be much harder, and the rule above is tuned only to prograde LEO reboosts.

**Sanity check on magnitude.** For a near-circular orbit, Δv ≈ (v / 2a)·Δa. A typical +1.9 km ISS step gives about 1.1 m/s. As an independent check, Roscosmos reported an April 2025 Progress MS-30 reboost as 1.22 m/s for +2.1 km mean altitude; the formula gives about 1.18 m/s for 2.1 km.

## Accuracy limits that go in every write-up

- Public element sets are accurate to roughly 1 km or worse, depending on regime, object, and fit. Use the residual noise floor above, not a nominal TLE accuracy, as the error bar for each object.
- **Latency.** The first element set reflecting a burn arrives 19 to 45 h later for the ISS. GEO and less-observed objects will be slower.
- **Fit contamination.** A set fitted across a burn blends pre- and post-burn tracking. The burn epoch is uncertain to roughly the gap between sets, and the first post-burn set can understate the change.
- **Missing objects.** Classified US payloads (for example USA 314 and USA 338) have no public elements, so proximity to them cannot be measured from public data.

## Still to build (milestone 2)

- Per-object noise model: rolling median and MAD of in-track residual, scaled by gap length. Long gaps inflate residuals; see the 62 h and 67 h gaps in the ISS results, which also delayed detection to 74 to 76 h.
- Detector output table: burn epoch window, Δa, Δi, ΔRAAN, Δv estimate, before/after orbits.
- GEO handling: longitude drift rate and east-west/north-south station-keeping signatures, so routine station-keeping is labelled as such and not flagged as notable.
- Closest-object search after each event, against the full public catalog.
