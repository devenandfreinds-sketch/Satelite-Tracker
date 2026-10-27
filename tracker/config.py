"""Paths, network settings, and credential lookup. Nothing secret lives in this file."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SMT_DATA_DIR", REPO_ROOT / "data"))
DB_PATH = Path(os.environ.get("SMT_DB", DATA_DIR / "tracker.sqlite"))
WATCHLIST_PATH = REPO_ROOT / "config" / "watchlist.csv"
REPORTS_DIR = REPO_ROOT / "reports"

USER_AGENT = "satelite-tracker/0.1 (student research project; github.com/devenandfreinds-sketch/Satelite-Tracker)"
HTTP_TIMEOUT = 60

# CelesTrak refreshes GP data roughly every 2 hours and blocks clients that
# hammer it. Never re-request the same query inside this window.
CELESTRAK_MIN_REFETCH_HOURS = 2.0
CELESTRAK_DELAY_S = 1.0

# Which sources may appear in anything we publish. Space-Track's user
# agreement forbids redistribution, so its rows are internal-only, even when
# they arrive via a third-party archive such as SatChecker.
PUBLISHABLE_SOURCES = {"celestrak", "satchecker:celestrak"}


def spacetrack_credentials():
    """Return (user, password) from the environment, or None if unset."""
    user = os.environ.get("SPACETRACK_USER")
    password = os.environ.get("SPACETRACK_PASSWORD")
    if user and password:
        return user, password
    return None
