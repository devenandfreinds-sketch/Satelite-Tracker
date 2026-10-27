"""Label maneuver events and group them into multi-burn campaigns.

Labels are about what the burn did to the orbit, not about intent:

  GEO (0.9-1.1 rev/day, e < 0.05)
    geo_ew_stationkeeping   small in-plane burn, stays in its longitude slot   (routine)
    geo_ns_stationkeeping   plane change that pushes the inclination vector
                            back toward or through zero, stays in its slot     (routine)
    geo_drift_start         was holding a longitude, now drifting
    geo_drift_stop          was drifting, now holding a longitude
    geo_drift_change        drifting before and after, at a different rate
    geo_relocation          holds a longitude before and after, but a new one
    geo_plane_change        plane change that is not station-keeping
    geo_maneuver            anything else in GEO
  Other regimes
    altitude_maintenance    small raise (< 10 km) in LEO, e.g. drag makeup     (routine)
    orbit_raise / orbit_lowering
    circularization         perigee up, apogee down (eccentric orbits, e.g. a GEO transfer)
    eccentricity_raise      perigee down, apogee up
    plane_change            the plane change carries most of the delta-v

"Routine" means the burn looks like ordinary upkeep. A routine label is never
proof of benign intent; it only decides what gets a closer look first.

A campaign is a run of non-routine events on one object with no more than
CAMPAIGN_GAP_D days between them, or one event that itself spans more than
three days and three or more fits (a burn sequence the detector merged).
"""
import math

import numpy as np
import pandas as pd
from sgp4.propagation import gstime

from .elements import satrec_from_raw

SIDEREAL_REV_DAY = 1.00273791
STATIONARY_DRIFT_DEG_DAY = 0.05   # |drift| below this counts as holding a longitude
SLOT_DEG = 0.5                    # station-keeping keeps the longitude within this
EW_MAX_DV = 1.0                   # m/s; east-west station-keeping burns are small
NS_MAX_DV = 60.0                  # m/s; a large north-south correction
NS_ALIGNMENT = 0.7                # cos(angle) between the change and the pull toward i = 0
LEO_MAINTENANCE_KM = 10.0
CAMPAIGN_GAP_D = 3.0
ROUTINE = {"geo_ew_stationkeeping", "geo_ns_stationkeeping", "altitude_maintenance"}


def regime(mean_motion_rev_day, ecc):
    if 0.9 < mean_motion_rev_day < 1.1 and ecc < 0.05:
        return "geo"
    if mean_motion_rev_day > 11.25:
        return "leo"
    return "other"


def geo_longitude(fmt, raw):
    """Mean east longitude (deg, -180..180) at the set's own epoch.

    Uses the mean elements (node + argument of perigee + mean anomaly - GMST),
    which removes the daily east-west wobble an inclined or eccentric GEO orbit
    traces. The instantaneous sub-satellite longitude would jump by up to a
    few degrees depending on the time of day each set was fitted.
    """
    sat = satrec_from_raw(fmt, raw)
    lon = math.degrees(sat.nodeo + sat.argpo + sat.mo - gstime(sat.jdsatepoch + sat.jdsatepochF))
    return (lon + 180) % 360 - 180


def drift_rate(mean_motion_rev_day):
    """Longitude drift, deg/day (positive = eastward)."""
    return (mean_motion_rev_day - SIDEREAL_REV_DAY) * 360.0


def _inc_vector(inc_deg, raan_deg):
    o = math.radians(raan_deg)
    return np.array([inc_deg * math.cos(o), inc_deg * math.sin(o)])


def label(event, before, after):
    """Return (regime, label, routine, extras) for one event.

    event: dict from detect.detect; before/after: history rows either side.
    """
    reg = regime(before.mean_motion_rev_day, before.eccentricity)
    extras = {}
    if reg == "geo":
        lon_b, lon_a = geo_longitude(before.fmt, before.raw), geo_longitude(after.fmt, after.raw)
        dr_b, dr_a = drift_rate(before.mean_motion_rev_day), drift_rate(after.mean_motion_rev_day)
        extras = {"lon_before_deg": lon_b, "lon_after_deg": lon_a,
                  "drift_before_deg_day": dr_b, "drift_after_deg_day": dr_a}
        held_b, held_a = abs(dr_b) < STATIONARY_DRIFT_DEG_DAY, abs(dr_a) < STATIONARY_DRIFT_DEG_DAY
        d_lon = abs((lon_a - lon_b + 180) % 360 - 180)
        if held_b and held_a and d_lon < SLOT_DEG:
            if event["plane_change"]:
                iv_b = _inc_vector(before.inclination_deg, before.raan_deg)
                d_iv = _inc_vector(after.inclination_deg, after.raan_deg) - iv_b
                toward_zero = (np.dot(d_iv, -iv_b) / (np.linalg.norm(d_iv) * np.linalg.norm(iv_b))
                               if np.linalg.norm(d_iv) > 0 and np.linalg.norm(iv_b) > 0 else 0.0)
                if toward_zero >= NS_ALIGNMENT and event["dv_total_ms"] <= NS_MAX_DV:
                    name = "geo_ns_stationkeeping"
                else:
                    name = "geo_plane_change"
            elif event["dv_total_ms"] <= EW_MAX_DV:
                name = "geo_ew_stationkeeping"
            else:
                name = "geo_maneuver"
        elif held_b and not held_a:
            name = "geo_drift_start"
        elif held_a and not held_b:
            name = "geo_drift_stop"
        elif not held_a and not held_b:
            name = "geo_plane_change" if _plane_dominant(event) else "geo_drift_change"
        else:
            name = "geo_relocation"
    else:
        d_per = event["after_perigee_km"] - event["before_perigee_km"]
        d_apo = event["after_apogee_km"] - event["before_apogee_km"]
        if _plane_dominant(event):
            name = "plane_change"
        elif reg == "other" and (d_per > 0) != (d_apo > 0):
            # eccentric orbits: SMA alone hides what happened (Cosmos 2589's
            # transfer to GEO lowers SMA while raising perigee)
            name = "circularization" if d_per > 0 else "eccentricity_raise"
        elif event["d_sma_km"] > 0:
            name = ("altitude_maintenance" if reg == "leo" and event["d_sma_km"] < LEO_MAINTENANCE_KM
                    else "orbit_raise")
        else:
            name = "orbit_lowering"
    return reg, name, name in ROUTINE, extras


def _plane_dominant(event):
    return bool(event["plane_change"]) and event["dv_plane_ms"] > event["dv_inplane_ms"]


def campaigns(events, norad_id):
    """Assign campaign ids in place; return a list of campaign rows.

    events: DataFrame for one object, oldest first, with label/routine columns.
    """
    events["campaign_id"] = None
    if events.empty:
        return []
    start = pd.to_datetime(events.window_start_utc, utc=True)
    end = pd.to_datetime(events.window_end_utc, utc=True)
    groups, cur = [], []
    for i in events.index:
        if events.at[i, "routine"]:
            if cur:
                groups.append(cur)
                cur = []
            continue
        if cur and (start[i] - end[cur[-1]]).total_seconds() / 86400 > CAMPAIGN_GAP_D:
            groups.append(cur)
            cur = []
        cur.append(i)
    if cur:
        groups.append(cur)
    rows = []
    for g in groups:
        span_d = (end[g[-1]] - start[g[0]]).total_seconds() / 86400
        smeared = len(g) == 1 and events.at[g[0], "n_pairs"] >= 3 and span_d > 3
        if len(g) < 2 and not smeared:
            continue
        cid = f"{norad_id}-{start[g[0]]:%Y%m%d}"
        events.loc[g, "campaign_id"] = cid
        rows.append({
            "campaign_id": cid, "norad_id": norad_id,
            "start_utc": events.at[g[0], "window_start_utc"], "end_utc": events.at[g[-1], "window_end_utc"],
            "n_events": len(g), "span_days": span_d,
            "total_dv_ms": float(events.loc[g, "dv_total_ms"].sum()),
            "total_dv_sigma_ms": float(np.sqrt((events.loc[g, "dv_sigma_ms"] ** 2).sum())),
            "net_d_sma_km": float(events.loc[g, "d_sma_km"].sum()),
            "labels": ",".join(sorted(set(events.loc[g, "label"]))),
        })
    return rows
