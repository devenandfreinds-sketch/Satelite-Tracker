"""Space-Track.org GP history. INTERNAL ANALYSIS ONLY.

The Space-Track user agreement restricts redistribution, so every row from
here is stored with publishable=0 and must never reach the public dashboard.
Credentials come from SPACETRACK_USER / SPACETRACK_PASSWORD env vars.

Untested until we have an account configured; the query follows
https://www.space-track.org/documentation#/api.
"""
import requests

from .. import config

LOGIN = "https://www.space-track.org/ajaxauth/login"
QUERY = "https://www.space-track.org/basicspacedata/query"


def fetch_history(norad_id, start, end):
    """Return (http_status, [OMM dict, ...]) for epochs in [start, end]."""
    creds = config.spacetrack_credentials()
    if not creds:
        raise RuntimeError("Set SPACETRACK_USER and SPACETRACK_PASSWORD to use Space-Track.")
    user, password = creds
    with requests.Session() as s:
        s.headers["User-Agent"] = config.USER_AGENT
        r = s.post(LOGIN, data={"identity": user, "password": password}, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        url = (
            f"{QUERY}/class/gp_history/NORAD_CAT_ID/{int(norad_id)}"
            f"/EPOCH/{start}--{end}/orderby/EPOCH%20asc/format/json"
        )
        r = s.get(url, timeout=max(config.HTTP_TIMEOUT, 180))
        r.raise_for_status()
        return r.status_code, r.json()
