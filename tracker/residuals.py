"""Consecutive-element-set residuals: the raw signal the maneuver detector reads.

For each pair (previous set, new set) we

  1. propagate the previous set with SGP4 to the new set's epoch and take the
     position difference in the new set's radial / in-track / cross-track
     (RIC) frame, and
  2. difference the mean elements (semi-major axis, eccentricity,
     inclination, RAAN), removing the RAAN drift that J2 alone predicts.

A burn shows up as an in-track residual far above the usual few km, with a
step in mean semi-major axis. Natural drag only gives a slow, smooth decay.
"""
import math

import numpy as np
import pandas as pd

from .elements import satrec_from_raw

J2 = 1.08262668e-3  # WGS-72
RE_KM = 6378.135
MU = 398600.8


def _ric(r_ref, v_ref, d):
    r_hat = r_ref / np.linalg.norm(r_ref)
    c_hat = np.cross(r_ref, v_ref)
    c_hat /= np.linalg.norm(c_hat)
    i_hat = np.cross(c_hat, r_hat)
    return float(d @ r_hat), float(d @ i_hat), float(d @ c_hat)


def _j2_raan_rate_deg_day(a_km, e, inc_deg):
    n = math.sqrt(MU / a_km**3)  # rad/s
    p = a_km * (1 - e * e)
    rate = -1.5 * n * J2 * (RE_KM / p) ** 2 * math.cos(math.radians(inc_deg))
    return math.degrees(rate) * 86400.0


def _wrap180(x):
    return (x + 180.0) % 360.0 - 180.0


def compute(history):
    """history: DataFrame from db.load_history (one row per epoch, oldest first).

    Returns one row per consecutive pair, indexed by the newer set's epoch.
    """
    sats = [satrec_from_raw(f, r) for f, r in zip(history["fmt"], history["raw"])]
    out = []
    for k in range(1, len(sats)):
        prev, cur = sats[k - 1], sats[k]
        p, c = history.iloc[k - 1], history.iloc[k]
        dt_days = (cur.jdsatepoch - prev.jdsatepoch) + (cur.jdsatepochF - prev.jdsatepochF)
        if dt_days <= 0:
            continue
        e1, r1, v1 = prev.sgp4(cur.jdsatepoch, cur.jdsatepochF)
        e2, r2, v2 = cur.sgp4(cur.jdsatepoch, cur.jdsatepochF)
        if e1 or e2:
            continue
        r1, r2, v2 = np.array(r1), np.array(r2), np.array(v2)
        dr, di, dc = _ric(r2, v2, r1 - r2)
        raan_pred = _j2_raan_rate_deg_day(p.sma_km, p.eccentricity, p.inclination_deg) * dt_days
        out.append({
            "epoch": c.epoch,
            "prev_epoch": p.epoch,
            "dt_hours": dt_days * 24.0,
            "source": c.source,
            "res_radial_km": dr,
            "res_intrack_km": di,
            "res_cross_km": dc,
            "res_total_km": float(np.linalg.norm(r1 - r2)),
            "sma_km": c.sma_km,
            "d_sma_km": c.sma_km - p.sma_km,
            "d_ecc": c.eccentricity - p.eccentricity,
            "d_inc_deg": c.inclination_deg - p.inclination_deg,
            "d_raan_resid_deg": _wrap180(c.raan_deg - p.raan_deg - raan_pred),
            "perigee_alt_km": c.perigee_alt_km,
            "apogee_alt_km": c.apogee_alt_km,
        })
    return pd.DataFrame(out)
