"""Labels, campaigns and neighbour geometry on hand-built orbits."""
import json
import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from tracker import classify, neighbors
from tracker.elements import element_row, satrec_from_raw

EPOCH = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def omm(norad=90000, mm=classify.SIDEREAL_REV_DAY, inc=0.05, raan=90.0, ma=0.0, ecc=0.0002, argp=0.0, epoch=EPOCH):
    return {"CLASSIFICATION_TYPE": "U", "OBJECT_ID": "2099-001A", "EPHEMERIS_TYPE": 0, "ELEMENT_SET_NO": 999,
            "REV_AT_EPOCH": 1, "NORAD_CAT_ID": norad, "BSTAR": 0.0, "MEAN_MOTION_DOT": 0.0, "MEAN_MOTION_DDOT": 0.0,
            "MEAN_MOTION": mm, "ECCENTRICITY": ecc, "INCLINATION": inc, "RA_OF_ASC_NODE": raan,
            "ARG_OF_PERICENTER": argp, "MEAN_ANOMALY": ma, "EPOCH": epoch.strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "OBJECT_NAME": f"OBJ {norad}"}


def row(o):
    return pd.Series(element_row(o["NORAD_CAT_ID"], "omm", json.dumps(o), "celestrak", o["OBJECT_NAME"]))


def event(before, after, dv=0.05, plane=0, dv_plane=0.0):
    return {"plane_change": plane, "dv_total_ms": dv, "dv_plane_ms": dv_plane, "dv_inplane_ms": dv - dv_plane,
            "d_sma_km": after.sma_km - before.sma_km,
            "before_perigee_km": before.perigee_alt_km, "after_perigee_km": after.perigee_alt_km,
            "before_apogee_km": before.apogee_alt_km, "after_apogee_km": after.apogee_alt_km}


def test_geo_east_west_stationkeeping():
    b, a = row(omm()), row(omm(mm=classify.SIDEREAL_REV_DAY + 1e-5))
    reg, lab, routine, extras = classify.label(event(b, a), b, a)
    assert (reg, lab, routine) == ("geo", "geo_ew_stationkeeping", True)
    assert abs(extras["drift_after_deg_day"]) < classify.STATIONARY_DRIFT_DEG_DAY


def test_geo_north_south_through_zero():
    # the TJS-6 pattern: 0.50 deg at node 90 swung to 0.33 deg at node 265
    b, a = row(omm(inc=0.50, raan=90.0)), row(omm(inc=0.33, raan=265.0, ma=-175.0))
    _, lab, routine, _ = classify.label(event(b, a, dv=45, plane=1, dv_plane=45), b, a)
    assert (lab, routine) == ("geo_ns_stationkeeping", True)


def test_geo_inclination_increase_is_not_stationkeeping():
    b, a = row(omm(inc=0.05)), row(omm(inc=1.0))
    _, lab, routine, _ = classify.label(event(b, a, dv=50, plane=1, dv_plane=50), b, a)
    assert (lab, routine) == ("geo_plane_change", False)


def test_geo_drift_start_and_stop():
    held, drifting = row(omm()), row(omm(mm=1.0100))   # ~2.6 deg/day eastward
    assert classify.label(event(held, drifting, dv=3), held, drifting)[1] == "geo_drift_start"
    assert classify.label(event(drifting, held, dv=3), drifting, held)[1] == "geo_drift_stop"


def test_transfer_orbit_circularization():
    b = row(omm(mm=1.25, ecc=0.15, inc=1.0))
    a = row(omm(mm=1.20, ecc=0.08, inc=1.0))
    assert b.perigee_alt_km < a.perigee_alt_km and a.apogee_alt_km < b.apogee_alt_km
    assert classify.label(event(b, a, dv=90), b, a)[1] == "circularization"


def test_campaigns_group_close_nonroutine_events():
    t = [EPOCH + timedelta(days=d) for d in (0, 1, 2, 3, 20)]
    ev = pd.DataFrame({
        "window_start_utc": [x.isoformat() for x in t],
        "window_end_utc": [(x + timedelta(hours=12)).isoformat() for x in t],
        "routine": [0, 0, 1, 0, 0], "n_pairs": [1, 1, 1, 1, 1],
        "label": ["geo_drift_start", "geo_plane_change", "geo_ew_stationkeeping", "geo_drift_stop", "geo_drift_start"],
        "dv_total_ms": [3.0, 40.0, 0.1, 3.0, 2.0], "dv_sigma_ms": [0.2, 2.0, 0.01, 0.2, 0.1],
        "d_sma_km": [10.0, 0.0, 0.0, -10.0, 5.0],
    })
    rows = classify.campaigns(ev, 90000)
    # the routine event splits the run, so 0-1 form one campaign; 3 and 4 are isolated
    assert len(rows) == 1
    assert rows[0]["n_events"] == 2 and rows[0]["total_dv_ms"] == pytest.approx(43.0)
    assert list(ev.campaign_id.notna()) == [True, True, False, False, False]


def test_smeared_multi_fit_event_is_a_campaign():
    ev = pd.DataFrame({"window_start_utc": [EPOCH.isoformat()], "window_end_utc": [(EPOCH + timedelta(days=28)).isoformat()],
                       "routine": [0], "n_pairs": [22], "label": ["circularization"], "dv_total_ms": [97.0],
                       "dv_sigma_ms": [5.0], "d_sma_km": [-35.0]})
    assert len(classify.campaigns(ev, 64467)) == 1


def test_geo_colocated_candidates():
    target = row(omm(norad=1))
    lon = classify.geo_longitude(target.fmt, target.raw)
    # same orbit, mean anomaly offsets of 0.5 and 5 deg -> longitude offsets of the same size
    snap = {2: omm(norad=2, ma=0.5), 3: omm(norad=3, ma=5.0)}
    got = neighbors.candidates(target, "geo", EPOCH, snap, exclude={1})
    assert [c[0] for c in got] == [2]
    assert got[0][2] == pytest.approx(0.5, abs=0.05)
    assert lon == lon  # finite


def test_coplanar_leo_neighbour_range():
    # two satellites on one LEO orbit, 0.2 deg apart in mean anomaly
    a_omm, b_omm = omm(norad=1, mm=15.5, inc=51.6, raan=100), omm(norad=2, mm=15.5, inc=51.6, raan=100, ma=0.2)
    target = row(a_omm)
    got = neighbors.candidates(target, "leo", EPOCH, {2: b_omm, 3: omm(norad=3, mm=15.5, inc=51.6, raan=130)}, {1})
    assert [c[0] for c in got] == [2]
    other = satrec_from_raw("omm", json.dumps(b_omm))
    ev = {"norad_id": 1, "data_scope": "publishable", "window_end_utc": EPOCH.isoformat()}
    out = neighbors._summarise(ev, target, satrec_from_raw(target.fmt, target.raw),
                               [(2, "B", other, "test", EPOCH)], EPOCH, "leo", "coplanar")
    chord = 2 * target.sma_km * math.sin(math.radians(0.2) / 2)
    assert out[0]["min_range_km"] == pytest.approx(chord, rel=0.05)
