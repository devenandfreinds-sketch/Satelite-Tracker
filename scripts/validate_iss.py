"""ISS validation of the maneuver detector against NASA-reported burns.

  python scripts/validate_iss.py [--all-sources]

Runs tracker.detect on the ISS, matches events to validation/iss_maneuvers.csv,
reports hit rate, unmatched events, detection latency, and how well the
estimated SMA change agrees with the orbit change NASA states.

By default only publishable (CelesTrak-origin) element sets are used, so the
figures could be shown publicly. --all-sources adds Space-Track-origin sets
for an internal-only check of cadence effects.
"""
import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tracker import config, db, detect, validation  # noqa: E402

ISS = 25544
MI_KM = validation.MI_KM

INK, MUTED, GRID, SURFACE = "#0b0b0b", "#898781", "#e1e0d9", "#fcfcfb"
SERIES, FLAG, EVENT = "#2a78d6", "#d03b3b", "#52514e"


def style(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, linewidth=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#c3c2b7")
    ax.tick_params(colors=MUTED, labelsize=8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-sources", action="store_true")
    args = ap.parse_args()
    tag = "all_sources" if args.all_sources else "publishable"

    with db.connect() as conn:
        hist = db.load_history(conn, ISS, publishable_only=not args.all_sources)
    events, r, dropped = detect.detect(hist)
    events["start"] = pd.to_datetime(events.window_start_utc, utc=True)
    events["end"] = pd.to_datetime(events.window_end_utc, utc=True)
    events["confirmed"] = pd.to_datetime(events.confirmed_at_utc, utc=True)

    lo, hi = hist.epoch.min(), hist.epoch.max()
    truth, unmatched = validation.match(events, validation.load_truth(lo, hi))
    hits = truth[truth.event.notna()].copy()
    hits = hits.join(events, on="event", rsuffix="_ev")
    n_hit = len(hits)

    config.REPORTS_DIR.mkdir(exist_ok=True)

    # Overview: one measure per panel, shared time axis.
    fig, axes = plt.subplots(3, 1, figsize=(12, 8.5), sharex=True, facecolor="white")
    style(axes[0])
    axes[0].plot(hist.epoch, hist.sma_km, color=SERIES, linewidth=1.2)
    axes[0].set_ylabel("Mean semi-major axis (km)", fontsize=8.5, color=INK)
    for ax, col, label in [(axes[1], "z_pos", "Position residual z\n(max of R/I/C, log)"),
                           (axes[2], "z_elem", "Element step z\n(max of SMA/ecc/orbit normal, log)")]:
        style(ax)
        ax.plot(r.epoch, r[col].clip(lower=0.1), color=SERIES, linewidth=0.7, marker="o", markersize=1.8)
        ax.axhline(detect.Z_THRESHOLD, color=MUTED, linewidth=0.8)
        ax.set_yscale("log")
        ax.set_ylim(0.1, 3000)
        ax.set_ylabel(label, fontsize=8.5, color=INK)
    for ax in axes:
        for b in truth.burn:
            ax.axvline(b, color=EVENT, linewidth=0.8, linestyle=(0, (3, 3)), alpha=0.7)
    for _, e in events.iterrows():
        rows = r[(r.epoch > e.start) & (r.epoch <= e.end)]
        for ax, col in [(axes[1], "z_pos"), (axes[2], "z_elem")]:
            ax.plot(rows.epoch, rows[col], linestyle="none", marker="o", markersize=5,
                    markerfacecolor="none", markeredgecolor=FLAG, markeredgewidth=1.4)
    axes[-1].xaxis.set_major_locator(mdates.MonthLocator())
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%b\n%Y"))
    fig.suptitle(f"ISS (25544): maneuver detector on public element sets, {lo:%Y-%m-%d} to {hi:%Y-%m-%d}",
                 x=0.06, ha="left", fontsize=12, color=INK)
    fig.text(0.06, 0.925,
             f"{len(hist)} element sets ({tag}). Dashed rules: {len(truth)} NASA-reported burns. Red rings: detector events "
             f"(element z and matching position z both >= {detect.Z_THRESHOLD:g}, persistent).\n"
             f"{n_hit}/{len(truth)} burns detected; {len(unmatched)} events not in the NASA list.",
             fontsize=8.5, color=MUTED)
    fig.tight_layout(rect=(0, 0, 1, 0.91))
    out1 = config.REPORTS_DIR / f"iss_detector_overview_{tag}.png"
    fig.savefig(out1, dpi=150)

    # Estimated SMA change vs NASA's stated mean-altitude change.
    both = hits.dropna(subset=["d_apogee_mi", "d_perigee_mi"]).copy()
    both["nasa_km"] = (both.d_apogee_mi + both.d_perigee_mi) / 2 * MI_KM
    fig, ax = plt.subplots(figsize=(5.2, 4.6), facecolor="white")
    style(ax)
    m = max(both.nasa_km.max(), both.d_sma_km.max()) * 1.1
    ax.plot([0, m], [0, m], color=MUTED, linewidth=0.8)
    ax.errorbar(both.nasa_km, both.d_sma_km, yerr=both.dv_sigma_ms / both.dv_inplane_ms * both.d_sma_km,
                fmt="o", color=SERIES, markersize=6, elinewidth=1, capsize=2)
    for _, b in both.iterrows():
        ax.annotate(f"{b.burn:%Y-%m-%d}", (b.nasa_km, b.d_sma_km), textcoords="offset points",
                    xytext=(6, -3), fontsize=7, color=MUTED)
    ax.set_xlim(0, m)
    ax.set_ylim(0, m)
    ax.set_xlabel("NASA: mean of stated apogee and perigee change (km)", fontsize=8.5, color=INK)
    ax.set_ylabel("Detector: change in mean SMA (km)", fontsize=8.5, color=INK)
    ax.set_title("Burn size: detector vs NASA (line = perfect agreement)", fontsize=10, color=INK, loc="left")
    fig.tight_layout()
    out2 = config.REPORTS_DIR / f"iss_detector_vs_nasa_{tag}.png"
    fig.savefig(out2, dpi=150)

    lat_first = (hits.end - hits.burn).dt.total_seconds() / 3600
    lat_conf = (hits.confirmed - hits.burn).dt.total_seconds() / 3600
    print(f"element sets: {len(hist)} ({tag}), outlier sets dropped: {len(dropped)}, events: {len(events)}")
    print(f"NASA burns in window: {len(truth)}, detected: {n_hit} "
          f"({n_hit / len(truth):.0%}); events not in NASA list: {len(unmatched)}")
    print(f"latency from burn date: first set showing it median {lat_first.median():.0f} h "
          f"(max {lat_first.max():.0f} h); confirmed median {lat_conf.median():.0f} h (max {lat_conf.max():.0f} h)")
    for _, t in truth.iterrows():
        if pd.isna(t.event):
            print(f"  {t.burn:%Y-%m-%d} {t.vehicle:<14} MISSED")
            continue
        e = events.loc[t.event]
        print(f"  {t.burn:%Y-%m-%d} {t.vehicle:<14} dSMA {e.d_sma_km:+5.2f} km  dv {e.dv_total_ms:4.2f}"
              f" +/- {e.dv_sigma_ms:.2f} m/s  z_elem {e.max_z:5.0f}  z_pos {e.z_pos:5.0f}  "
              f"confirmed {e.confirmed:%m-%d %H:%M}Z")
    print("events not matched to a NASA burn:")
    for _, u in unmatched.iterrows():
        print(f"  {u.start:%Y-%m-%d %H:%M} to {u.end:%m-%d %H:%M}Z  driver {u.driver:<4} dSMA {u.d_sma_km:+.2f} km  "
              f"dv {u.dv_total_ms:.2f} m/s  z_elem {u.max_z:.0f}  z_pos {u.z_pos:.0f}")
    err = both.d_sma_km - both.nasa_km
    print(f"SMA change vs NASA ({len(both)} burns with apogee and perigee stated): "
          f"mean error {err.mean():+.2f} km, RMS {np.sqrt((err ** 2).mean()):.2f} km, "
          f"mean ratio {(both.d_sma_km / both.nasa_km).mean():.2f}")
    print(f"wrote {out1}\nwrote {out2}")


if __name__ == "__main__":
    main()
