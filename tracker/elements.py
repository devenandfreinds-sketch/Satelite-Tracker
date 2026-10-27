"""Turn raw element sets (CelesTrak OMM JSON or two-line TLE text) into
sgp4 Satrec objects and a flat row of mean elements for storage."""
import json
import math
from datetime import datetime, timedelta, timezone

from sgp4 import omm
from sgp4.api import Satrec, WGS72

MU_EARTH = 398600.8  # km^3/s^2, WGS-72, the constant SGP4 itself uses


class BadElementSet(ValueError):
    """An element set that is malformed or physically impossible."""


def _tle_checksum(line):
    return sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line[:68]) % 10


def check_tle(raw):
    """Reject TLEs whose columns or checksum are wrong.

    Fixed-column parsing does not notice a field that is one character too
    long: everything after it shifts and is read as the wrong element. Some
    archived sets have an 8-character eccentricity, which turns the mean
    anomaly into a mean motion of hundreds of revolutions per day.
    """
    lines = raw.splitlines()[:2]
    if len(lines) != 2:
        raise BadElementSet("TLE needs two lines")
    for n, line in enumerate(lines, start=1):
        if len(line.rstrip()) != 69 or not line.startswith(str(n)):
            raise BadElementSet(f"line {n} is {len(line.rstrip())} characters, expected 69")
        if not line[68].isdigit() or int(line[68]) != _tle_checksum(line):
            raise BadElementSet(f"line {n} checksum mismatch")


def satrec_from_raw(fmt, raw):
    if fmt == "omm":
        sat = Satrec()
        omm.initialize(sat, json.loads(raw))
        return sat
    if fmt == "tle":
        line1, line2 = raw.splitlines()[:2]
        return Satrec.twoline2rv(line1, line2, WGS72)
    raise ValueError(f"unknown element format {fmt!r}")


def epoch_of(sat):
    """Epoch as an aware UTC datetime."""
    year = sat.epochyr + (2000 if sat.epochyr < 57 else 1900)
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=sat.epochdays - 1)


def element_row(norad_id, fmt, raw, source, object_name=None):
    """Flatten one element set into the columns of the `elements` table.

    Raises BadElementSet for malformed or physically impossible sets.
    """
    if fmt == "tle":
        check_tle(raw)
    sat = satrec_from_raw(fmt, raw)
    rev_per_day = sat.no_kozai * 1440.0 / (2 * math.pi)
    # Semi-major axis from SGP4's un-Kozai'd (Brouwer) mean motion, in km.
    sma_km = sat.a * sat.radiusearthkm
    ep = epoch_of(sat)
    perigee = sma_km * (1 - sat.ecco) - sat.radiusearthkm
    if not (0.05 <= rev_per_day <= 17.5) or not (0 <= sat.ecco < 1) or perigee < 80:
        raise BadElementSet(f"impossible orbit: {rev_per_day:.3f} rev/day, e={sat.ecco:.5f}, perigee {perigee:.0f} km")
    return {
        "norad_id": int(norad_id),
        "epoch_utc": ep.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "epoch_jd": sat.jdsatepoch + sat.jdsatepochF,
        "source": source,
        "object_name": object_name,
        "fmt": fmt,
        "raw": raw,
        "mean_motion_rev_day": rev_per_day,
        "eccentricity": sat.ecco,
        "inclination_deg": math.degrees(sat.inclo),
        "raan_deg": math.degrees(sat.nodeo),
        "argp_deg": math.degrees(sat.argpo),
        "mean_anomaly_deg": math.degrees(sat.mo),
        "bstar": sat.bstar,
        "sma_km": sma_km,
        "perigee_alt_km": sma_km * (1 - sat.ecco) - sat.radiusearthkm,
        "apogee_alt_km": sma_km * (1 + sat.ecco) - sat.radiusearthkm,
    }


def valid_rows(make_rows):
    """Build rows from (norad_id, fmt, raw, source, name) tuples, skipping bad sets.

    Returns (rows, n_rejected).
    """
    rows, bad = [], 0
    for args in make_rows:
        try:
            rows.append(element_row(*args))
        except BadElementSet:
            bad += 1
    return rows, bad
