"""CelesTrak GP data (public, redistributable). Our only source for public output.

API docs: https://celestrak.org/NORAD/documentation/gp-data-formats.php
OMM JSON is used rather than TLE text because TLEs cannot carry catalog
numbers above 99999, and CelesTrak already issues some (e.g. TJS-27A, 100174).
"""
import json
import time

import requests

from .. import config

BASE = "https://celestrak.org/NORAD/elements/gp.php"


class CelesTrakError(RuntimeError):
    pass


def _get(params):
    resp = requests.get(
        BASE,
        params={**params, "FORMAT": "JSON"},
        headers={"User-Agent": config.USER_AGENT},
        timeout=config.HTTP_TIMEOUT,
    )
    time.sleep(config.CELESTRAK_DELAY_S)
    # A repeat of a query inside CelesTrak's refresh window gets a 403 saying
    # the data "has not updated". That is a normal no-op, not a failure.
    if resp.status_code == 403 and "has not updated" in resp.text:
        return resp.status_code, []
    if resp.status_code != 200:
        raise CelesTrakError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    text = resp.text.strip()
    # CelesTrak answers a miss with a plain-text body, not an empty list.
    if not text.startswith("["):
        return resp.status_code, []
    return resp.status_code, json.loads(text)


def fetch_catnr(norad_id):
    return _get({"CATNR": int(norad_id)})


def fetch_group(group):
    return _get({"GROUP": group})


def fetch_name(name):
    return _get({"NAME": name})


def to_raw(omm_record):
    """Canonical string form of one OMM record, as stored in elements.raw."""
    return json.dumps(omm_record, sort_keys=True, separators=(",", ":"))
