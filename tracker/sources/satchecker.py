"""Historical TLEs from the IAU CPS SatChecker archive (no login).

https://satchecker.readthedocs.io/en/latest/tools_tle.html

SatChecker records which upstream each TLE came from. We keep that label:
rows it collected from CelesTrak become `satchecker:celestrak` (publishable);
rows it collected from Space-Track become `satchecker:spacetrack` (internal
only, because the Space-Track user agreement follows the data).
"""
from datetime import datetime, timezone

import requests

from .. import config

BASE = "https://satchecker.cps.iau.org/tools/get-tle-data/"


def _jd(dt):
    return dt.replace(tzinfo=timezone.utc).timestamp() / 86400.0 + 2440587.5


def fetch_history(norad_id, start, end):
    """Return (http_status, [(source_label, tle_text, name), ...])."""
    if isinstance(start, str):
        start = datetime.fromisoformat(start)
    if isinstance(end, str):
        end = datetime.fromisoformat(end)
    resp = requests.get(
        BASE,
        params={
            "id": int(norad_id),
            "id_type": "catalog",
            "start_date_jd": f"{_jd(start):.5f}",
            "end_date_jd": f"{_jd(end):.5f}",
        },
        headers={"User-Agent": config.USER_AGENT},
        timeout=max(config.HTTP_TIMEOUT, 180),
    )
    resp.raise_for_status()
    out = []
    for rec in resp.json().get("data", []):
        upstream = (rec.get("data_source") or "unknown").lower()
        tle = f"{rec['tle_line1']}\n{rec['tle_line2']}"
        out.append((f"satchecker:{upstream}", tle, rec.get("satellite_name")))
    return resp.status_code, out
