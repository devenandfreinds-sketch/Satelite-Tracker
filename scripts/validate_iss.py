"""ISS reboost validation: residual plots around NASA-reported burns.

  python scripts/validate_iss.py [--all-sources]

By default only publishable (CelesTrak-origin) element sets are used, so the
figure could be shown publicly. --all-sources adds Space-Track-origin sets
for an internal-only check of cadence effects.

The flag rule here is a provisional eyeball threshold, not the detector.
The detector and its noise model are milestone 2.
"""
import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tracker import config, db, residuals  # noqa: E402

ISS = 25544
TRUTH = config.REPO_ROOT / "validation" / "iss_maneuvers.csv"

# Provisional: a reboost raises mean SMA and throws the old set well off in-track.
FLAG_INTRACK_KM = 15.0
FLAG_DSMA_KM = 0.25
# A flag says "something happened between the previous set's epoch and this
# one". It matches a burn when that interval overlaps the burn's calendar day.
# NASA dates are US Eastern, so a "Wednesday night" burn can be early Thursday
# UTC: the burn window runs from 00:00Z on the date to 12:00Z the next day.
BURN_WINDOW_END_H = 36

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


def match(res, truth):
    flags = res[res.flag]
    truth = truth.copy()
    truth["detected_at"] = pd.NaT
    used = set()
    for i, t in truth.iterrows():
        burn_end = t.burn + pd.Timedelta(hours=BURN_WINDOW_END_H)
        win = flags[(flags.epoch >= t.burn) & (flags.prev_epoch <= burn_end) & ~flags.index.isin(used)]
        if len(win):
            truth.at[i, "detected_at"] = win.epoch.iloc[0]
            used.add(win.index[0])
    unmatched = flags.drop(index=list(used))
    return truth, unmatched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-sources", action="store_true")
    args = ap.parse_args()

    with db.connect() as conn:
        hist = db.load_history(conn, ISS, publishable_only=not args.all_sources)
    res = residuals.compute(hist)
    res["flag"] = (res.res_intrack_km.abs() > FLAG_INTRACK_KM) & (res.d_sma_km > FLAG_DSMA_KM)

    truth = pd.read_csv(TRUTH, comment="#")
    truth["burn"] = pd.to_datetime(truth.burn_date).dt.tz_localize("UTC")
    lo, hi = res.epoch.min(), res.epoch.max()
    truth = truth[(truth.burn >= lo) & (truth.burn <= hi)].reset_index(drop=True)
    truth, unmatched = match(res, truth)

    tag = "all_sources" if args.all_sources else "publishable"
    config.REPORTS_DIR.mkdir(exist_ok=True)

    # Overview: one measure per panel, shared time axis, NASA burns as rules.
    fig, axes = plt.subplots(3, 1, figsize=(12, 8.5), sharex=True, facecolor="white")
    panels = [
        ("sma_km", "Mean semi-major axis (km)", False),
        ("res_intrack_km", "In-track residual, previous set\npropagated to new epoch (km, symlog)", True),
        ("d_sma_km", "Change in mean SMA\nbetween consecutive sets (km)", False),
    ]
    for ax, (col, label, symlog) in zip(axes, panels):
        style(ax)
        for b in truth.burn:
            ax.axvline(b, color=EVENT, linewidth=0.8, linestyle=(0, (3, 3)), alpha=0.7)
        ax.plot(res.epoch, res[col], color=SERIES, linewidth=1.2 if col == "sma_km" else 0.9,
                marker=None if col == "sma_km" else "o", markersize=2.2)
        f = res[res.flag]
        if col != "sma_km":
            ax.plot(f.epoch, f[col], linestyle="none", marker="o", markersize=5,
                    markerfacecolor="none", markeredgecolor=FLAG, markeredgewidth=1.4)
        if symlog:
            ax.set_yscale("symlog", linthresh=1.0)
            ax.axhline(FLAG_INTRACK_KM, color=MUTED, linewidth=0.7)
            ax.axhline(-FLAG_INTRACK_KM, color=MUTED, linewidth=0.7)
        ax.set_ylabel(label, fontsize=8.5, color=INK)
    axes[-1].xaxis.set_major_locator(mdates.MonthLocator())
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%b\n%Y"))
    n_hit = truth.detected_at.notna().sum()
    fig.suptitle(
        f"ISS (25544): residuals between consecutive public element sets, {lo:%Y-%m-%d} to {hi:%Y-%m-%d}",
        x=0.06, ha="left", fontsize=12, color=INK)
    fig.text(0.06, 0.935,
             f"{len(hist)} element sets ({tag}). Dashed rules: {len(truth)} NASA-reported burns. "
             f"Red rings: provisional flags (|in-track| > {FLAG_INTRACK_KM:g} km and dSMA > {FLAG_DSMA_KM:g} km). "
             f"{n_hit}/{len(truth)} burns flagged; {len(unmatched)} flags not in the NASA list.",
             fontsize=8.5, color=MUTED)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    out1 = config.REPORTS_DIR / f"iss_residuals_overview_{tag}.png"
    fig.savefig(out1, dpi=150)

    # Small multiples: +/- 6 days around each NASA burn.
    n = len(truth)
    cols = 4
    rows = -(-n // cols)
    fig, axes = plt.subplots(rows, cols, figsize=(3.4 * cols, 2.6 * rows), facecolor="white", squeeze=False)
    for ax, (_, t) in zip(axes.flat, truth.iterrows()):
        style(ax)
        w = res[(res.epoch > t.burn - pd.Timedelta(days=6)) & (res.epoch < t.burn + pd.Timedelta(days=6))]
        ax.axvspan(t.burn, t.burn + pd.Timedelta(hours=BURN_WINDOW_END_H), color=GRID, alpha=0.6, linewidth=0)
        ax.axvline(t.burn, color=EVENT, linewidth=0.8, linestyle=(0, (3, 3)))
        ax.plot(w.epoch, w.res_intrack_km.abs().clip(lower=0.01), color=SERIES, marker="o", markersize=3, linewidth=0.9)
        f = w[w.flag]
        ax.plot(f.epoch, f.res_intrack_km.abs(), linestyle="none", marker="o", markersize=7,
                markerfacecolor="none", markeredgecolor=FLAG, markeredgewidth=1.4)
        ax.set_yscale("log")
        ax.set_ylim(0.01, 2000)
        ax.axhline(FLAG_INTRACK_KM, color=MUTED, linewidth=0.7)
        status = "flagged" if pd.notna(t.detected_at) else "MISSED"
        ax.set_title(f"{t.burn:%Y-%m-%d} {t.vehicle}  ({status})", fontsize=8, color=INK, loc="left")
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=3))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    for ax in list(axes.flat)[n:]:
        ax.set_visible(False)
    fig.suptitle("|In-track residual| (km, log) within 6 days of each NASA-reported ISS burn; "
                 "shaded = burn day (US date, 00Z to +36 h)", x=0.02, ha="left", fontsize=11, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    out2 = config.REPORTS_DIR / f"iss_residuals_windows_{tag}.png"
    fig.savefig(out2, dpi=150)

    quiet = res[~res.flag]
    print(f"element sets: {len(hist)}  pairs: {len(res)}  median spacing: {res.dt_hours.median():.1f} h")
    print(f"quiet-pair noise: in-track median |{quiet.res_intrack_km.abs().median():.2f}| km, "
          f"95th pct {quiet.res_intrack_km.abs().quantile(0.95):.2f} km, 99th {quiet.res_intrack_km.abs().quantile(0.99):.2f} km; "
          f"radial 95th {quiet.res_radial_km.abs().quantile(0.95):.2f} km")
    print(f"NASA burns in window: {len(truth)}, flagged: {n_hit}")
    for _, t in truth.iterrows():
        lag = (t.detected_at - t.burn).total_seconds() / 3600 if pd.notna(t.detected_at) else None
        print(f"  {t.burn:%Y-%m-%d} {t.vehicle:<14} {t.date_basis:<16} "
              + (f"flagged at {t.detected_at:%Y-%m-%d %H:%M}Z (+{lag:.0f} h)" if lag is not None else "MISSED"))
    print(f"flags not matched to a NASA burn: {len(unmatched)}")
    for _, u in unmatched.iterrows():
        print(f"  {u.epoch:%Y-%m-%d %H:%M}Z  in-track {u.res_intrack_km:7.1f} km  dSMA {u.d_sma_km:+.2f} km  gap {u.dt_hours:.0f} h")
    print(f"wrote {out1}\nwrote {out2}")


if __name__ == "__main__":
    main()
