"""Detector tests on synthetic element histories with a known burn injected.

  .venv/bin/python -m pytest tests
"""
import json
import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sgp4 import omm
from sgp4.api import Satrec

from tracker import detect, watchlist
from tracker.elements import element_row
from tracker.residuals import orbit_normal

T0 = datetime(2026, 1, 1)
BASE = {  # ISS-like LEO, no drag so the truth is easy to write down
    "CLASSIFICATION_TYPE": "U", "OBJECT_ID": "2099-001A", "EPHEMERIS_TYPE": 0, "ELEMENT_SET_NO": 999,
    "REV_AT_EPOCH": 1, "NORAD_CAT_ID": 99999, "BSTAR": 0.0, "MEAN_MOTION_DOT": 0.0, "MEAN_MOTION_DDOT": 0.0,
    "MEAN_MOTION": 15.5, "ECCENTRICITY": 0.0006, "INCLINATION": 51.6,
    "RA_OF_ASC_NODE": 100.0, "ARG_OF_PERICENTER": 80.0, "MEAN_ANOMALY": 10.0,
}


def _rates(fields):
    """SGP4's own secular rates (deg/day) for M, argp and node."""
    sat = Satrec()
    omm.initialize(sat, {**fields, "EPOCH": T0.strftime("%Y-%m-%dT%H:%M:%S.%f")})
    k = math.degrees(1.0) * 1440.0
    return sat.mdot * k, sat.argpdot * k, sat.nodedot * k


def synthetic_history(n_sets=120, gap_h=9.0, burn_at=None, d_mm=0.0, d_inc=0.0, seed=1):
    """Element sets that follow SGP4's secular motion, with fit-sized noise.

    burn_at: index of the first set after an impulsive change of d_mm
    (rev/day) in mean motion and d_inc (deg) in inclination.
    """
    rng = np.random.default_rng(seed)
    f = dict(BASE)
    m_rate, w_rate, o_rate = _rates(f)
    m, w, o, t_prev = f["MEAN_ANOMALY"], f["ARG_OF_PERICENTER"], f["RA_OF_ASC_NODE"], 0.0
    rows = []
    for k in range(n_sets):
        t = k * gap_h / 24.0
        if burn_at is not None and k == burn_at:
            # burn halfway through the gap: advance at the old rates to the burn,
            # then switch to the new orbit's rates
            mid = (t_prev + t) / 2
            m += m_rate * (mid - t_prev); w += w_rate * (mid - t_prev); o += o_rate * (mid - t_prev)
            t_prev = mid
            f["MEAN_MOTION"] += d_mm
            f["INCLINATION"] += d_inc
            m_rate, w_rate, o_rate = _rates(f)
        dt = t - t_prev
        m += m_rate * dt; w += w_rate * dt; o += o_rate * dt
        t_prev = t
        rec = {**f, "EPOCH": (T0 + timedelta(days=t)).strftime("%Y-%m-%dT%H:%M:%S.%f"),
               # noise of roughly the size seen in real ISS fits
               "MEAN_ANOMALY": (m + rng.normal(0, 0.0015)) % 360,
               "MEAN_MOTION": f["MEAN_MOTION"] + rng.normal(0, 2e-6),
               "INCLINATION": f["INCLINATION"] + rng.normal(0, 5e-5),
               "RA_OF_ASC_NODE": (o + rng.normal(0, 5e-5)) % 360,
               "ARG_OF_PERICENTER": w % 360,
               "ECCENTRICITY": f["ECCENTRICITY"] + rng.normal(0, 2e-7)}
        rows.append(element_row(99999, "omm", json.dumps(rec), "celestrak", "SYNTH"))
    h = pd.DataFrame(rows)
    h["epoch"] = pd.to_datetime(h.epoch_utc, utc=True)
    h["publishable"] = 1
    return h


def test_quiet_history_has_no_events():
    ev, _, dropped = detect.detect(synthetic_history())
    assert len(ev) == 0
    assert dropped == []


def test_reboost_found_with_right_size():
    # -0.0068 rev/day at 15.5 rev/day is about +2 km in SMA, ~1.1 m/s
    h = synthetic_history(burn_at=80, d_mm=-0.0068)
    ev, _, _ = detect.detect(h)
    assert len(ev) == 1
    e = ev.iloc[0]
    assert e.status == "confirmed"
    assert e.window_end_utc == h.epoch_utc.iloc[80]
    assert e.d_sma_km == pytest.approx(2.0, rel=0.05)
    expected_dv = 0.5 * math.sqrt(398600.8 / h.sma_km.iloc[79]) * 1000 * 2.0 / h.sma_km.iloc[79]
    assert e.dv_total_ms == pytest.approx(expected_dv, rel=0.1)
    assert e.plane_change == 0


def test_plane_change_found():
    # 0.02 deg inclination change at ~7.66 km/s is about 2.7 m/s
    ev, _, _ = detect.detect(synthetic_history(burn_at=60, d_inc=0.02))
    assert len(ev) == 1
    e = ev.iloc[0]
    assert e.driver == "plane"
    assert e.plane_change == 1
    assert e.plane_change_deg == pytest.approx(0.02, rel=0.15)
    assert e.dv_plane_ms == pytest.approx(2 * 7660 * math.sin(math.radians(0.01)), rel=0.15)


def test_single_bad_fit_is_dropped_not_reported():
    h = synthetic_history()
    bad = json.loads(h.raw.iloc[70])
    bad["MEAN_MOTION"] -= 0.0068  # one fit 2 km off, neighbours agree
    h.loc[70] = element_row(99999, "omm", json.dumps(bad), "celestrak", "SYNTH") | {
        "epoch": h.epoch.iloc[70], "publishable": 1}
    ev, _, dropped = detect.detect(h)
    assert len(ev) == 0
    assert dropped == [h.epoch_utc.iloc[70]]


def test_burn_at_end_is_provisional():
    h = synthetic_history(n_sets=81, burn_at=80, d_mm=-0.0068)
    ev, _, _ = detect.detect(h)
    assert len(ev) == 1
    assert ev.iloc[0].status == "provisional"


def test_delta_v_tangential_identity():
    a = 6798.0
    v = math.sqrt(398600.8 / a) * 1000
    dv_in, dv_pl, dv_tot, _ = detect.delta_v(a, d_sma=2 * a * 1.0 / v, d_ecc=0.0, plane_angle_rad=0.0)
    assert dv_in == pytest.approx(1.0, rel=1e-9)
    assert dv_pl == 0.0


def test_orbit_normal_defined_at_zero_inclination():
    np.testing.assert_allclose(orbit_normal(0.0, 10.0), orbit_normal(0.0, 250.0), atol=1e-15)
    # inclination vector swinging through zero: 0.5 deg at node 90 to 0.33 deg at node 270
    chord = np.linalg.norm(orbit_normal(0.33, 270.0) - orbit_normal(0.5, 90.0))
    assert math.degrees(2 * math.asin(chord / 2)) == pytest.approx(0.83, abs=1e-3)


def test_watchlist_rejects_unquoted_comma(tmp_path):
    p = tmp_path / "w.csv"
    p.write_text("norad_id,name,cospar_id,category,operator,fetch_via,notes\n"
                 "1,X,2000-001A,geo,Nobody,catnr,has, a comma\n")
    with pytest.raises(ValueError, match="line 2"):
        watchlist.load(p)


def test_watchlist_save_is_atomic(tmp_path, monkeypatch):
    p = tmp_path / "w.csv"
    p.write_text("original\n")
    with pytest.raises(ValueError):
        watchlist.save([{"norad_id": 1, "unexpected": "field"}], p)
    assert p.read_text() == "original\n"
    assert not (tmp_path / "w.csv.tmp").exists()


GOOD_TLE = ("1 47613U 21010A   25217.58714096  .00000050  00000+0  00000+0 0  9995\n"
            "2 47613   0.2235 262.6404 0001349 187.7607 253.8330  1.00269677 16630")
# Real archived set with an 8-character eccentricity: every later column shifts
# and the mean anomaly is read as 328 rev/day.
SHIFTED_TLE = ("1 47613U 21010A   25217.79413917  .00000050  00000+0  00000+0 0  9996\n"
               "2 47613   0.2230 262.6402 00013391 188.1547 328.1612  1.00269724 16644")


def test_good_tle_accepted():
    row = element_row(47613, "tle", GOOD_TLE, "test")
    assert row["mean_motion_rev_day"] == pytest.approx(1.0027, abs=1e-4)


def test_column_shifted_tle_rejected():
    from tracker.elements import BadElementSet, valid_rows
    with pytest.raises(BadElementSet, match="70 characters"):
        element_row(47613, "tle", SHIFTED_TLE, "test")
    rows, bad = valid_rows([(47613, "tle", GOOD_TLE, "test", None), (47613, "tle", SHIFTED_TLE, "test", None)])
    assert len(rows) == 1 and bad == 1


def test_bad_checksum_rejected():
    from tracker.elements import BadElementSet
    with pytest.raises(BadElementSet, match="checksum"):
        element_row(47613, "tle", GOOD_TLE[:-1] + "1", "test")


def test_unpropagatable_set_does_not_shift_later_events():
    # A set SGP4 cannot propagate makes residuals skip two pairs. Events after
    # it must still point at the right sets (a positional assumption used to
    # shift every later event by two sets and hide real burns).
    h = synthetic_history(n_sets=140, burn_at=110, d_mm=-0.0068)
    broken = json.loads(h.raw.iloc[40])
    broken["ECCENTRICITY"] = 0.99  # perigee far inside the Earth: SGP4 error
    h.loc[40, "raw"] = json.dumps(broken)
    ev, _, _ = detect.detect(h)
    assert len(ev) == 1
    assert ev.iloc[0].window_end_utc == h.epoch_utc.iloc[110]
    assert ev.iloc[0].d_sma_km == pytest.approx(2.0, rel=0.05)
