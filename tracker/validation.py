"""Match detector events to NASA-reported ISS burns (shared by the validation
script and the site export, so both report the same numbers)."""
import pandas as pd

from . import config

TRUTH = config.REPO_ROOT / "validation" / "iss_maneuvers.csv"
MI_KM = 1.609344

# An event spans [last set before, first set after]. It matches a burn when
# that span overlaps the burn's calendar day. NASA dates are US Eastern, so a
# "Wednesday night" burn can be early Thursday UTC: the burn window runs from
# 00:00Z on the date to 12:00Z the next day.
BURN_WINDOW_END_H = 36


def load_truth(lo=None, hi=None):
    truth = pd.read_csv(TRUTH, comment="#")
    truth["burn"] = pd.to_datetime(truth.burn_date).dt.tz_localize("UTC")
    if lo is not None:
        truth = truth[(truth.burn >= lo) & (truth.burn <= hi)]
    return truth.reset_index(drop=True)


def match(events, truth):
    """events needs `start` and `end` (UTC timestamps). Returns (truth with an
    `event` column holding the matched events index, unmatched events)."""
    truth = truth.copy()
    truth["event"] = pd.NA
    used = set()
    for i, t in truth.iterrows():
        burn_end = t.burn + pd.Timedelta(hours=BURN_WINDOW_END_H)
        win = events[(events.end >= t.burn) & (events.start <= burn_end) & ~events.index.isin(used)]
        if len(win):
            truth.at[i, "event"] = win.index[0]
            used.add(win.index[0])
    return truth, events.drop(index=list(used))
