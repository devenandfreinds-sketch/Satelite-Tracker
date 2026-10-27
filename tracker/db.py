"""SQLite storage. Every element set we ever see is kept; nothing is overwritten."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS satellites (
    norad_id      INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    cospar_id     TEXT,
    category      TEXT NOT NULL,
    operator      TEXT,
    fetch_via     TEXT NOT NULL,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS elements (
    id                   INTEGER PRIMARY KEY,
    norad_id             INTEGER NOT NULL,
    epoch_utc            TEXT NOT NULL,
    epoch_jd             REAL NOT NULL,
    source               TEXT NOT NULL,
    publishable          INTEGER NOT NULL,
    fetched_at           TEXT NOT NULL,
    object_name          TEXT,
    fmt                  TEXT NOT NULL,   -- 'omm' (JSON) or 'tle' (two lines)
    raw                  TEXT NOT NULL,   -- exact element set as received
    mean_motion_rev_day  REAL,
    eccentricity         REAL,
    inclination_deg      REAL,
    raan_deg             REAL,
    argp_deg             REAL,
    mean_anomaly_deg     REAL,
    bstar                REAL,
    sma_km               REAL,
    perigee_alt_km       REAL,
    apogee_alt_km        REAL,
    UNIQUE (norad_id, epoch_utc, source)
);
CREATE INDEX IF NOT EXISTS idx_elements_sat_epoch ON elements (norad_id, epoch_jd);

CREATE TABLE IF NOT EXISTS fetch_log (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    query       TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    http_status INTEGER,
    n_records   INTEGER,
    n_new       INTEGER,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_query ON fetch_log (source, query, fetched_at);
"""

ELEMENT_COLS = [
    "norad_id", "epoch_utc", "epoch_jd", "source", "publishable", "fetched_at",
    "object_name", "fmt", "raw", "mean_motion_rev_day", "eccentricity",
    "inclination_deg", "raan_deg", "argp_deg", "mean_anomaly_deg", "bstar",
    "sma_km", "perigee_alt_km", "apogee_alt_km",
]


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def connect(path=None):
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_elements(conn, rows):
    """Insert element rows, skipping ones already stored. Returns count added."""
    fetched_at = utcnow_iso()
    before = conn.total_changes
    for row in rows:
        row = dict(row)
        row.setdefault("fetched_at", fetched_at)
        row["publishable"] = int(row["source"] in config.PUBLISHABLE_SOURCES)
        conn.execute(
            f"INSERT OR IGNORE INTO elements ({','.join(ELEMENT_COLS)}) "
            f"VALUES ({','.join('?' * len(ELEMENT_COLS))})",
            [row.get(c) for c in ELEMENT_COLS],
        )
    return conn.total_changes - before


def log_fetch(conn, source, query, http_status=None, n_records=None, n_new=None, error=None):
    conn.execute(
        "INSERT INTO fetch_log (source, query, fetched_at, http_status, n_records, n_new, error) "
        "VALUES (?,?,?,?,?,?,?)",
        (source, query, utcnow_iso(), http_status, n_records, n_new, error),
    )


def hours_since_last_fetch(conn, source, query):
    row = conn.execute(
        "SELECT MAX(fetched_at) FROM fetch_log WHERE source=? AND query=? AND error IS NULL",
        (source, query),
    ).fetchone()
    if not row or not row[0]:
        return None
    last = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() / 3600


def upsert_satellites(conn, sats):
    conn.executemany(
        "INSERT INTO satellites (norad_id, name, cospar_id, category, operator, fetch_via, notes) "
        "VALUES (:norad_id, :name, :cospar_id, :category, :operator, :fetch_via, :notes) "
        "ON CONFLICT(norad_id) DO UPDATE SET name=excluded.name, cospar_id=excluded.cospar_id, "
        "category=excluded.category, operator=excluded.operator, fetch_via=excluded.fetch_via, "
        "notes=excluded.notes",
        sats,
    )


def load_history(conn, norad_id, sources=None, publishable_only=False):
    """All stored element sets for one object, oldest first, one per epoch.

    When the same epoch arrives from several sources, the publishable copy wins.
    """
    import pandas as pd

    q = "SELECT * FROM elements WHERE norad_id=?"
    args = [norad_id]
    if publishable_only:
        q += " AND publishable=1"
    if sources:
        q += f" AND source IN ({','.join('?' * len(sources))})"
        args += list(sources)
    df = pd.read_sql_query(q, conn, params=args)
    if df.empty:
        return df
    df = df.sort_values(["epoch_jd", "publishable"], ascending=[True, False])
    df = df.drop_duplicates(subset="epoch_utc", keep="first")
    df["epoch"] = pd.to_datetime(df["epoch_utc"], utc=True)
    return df.reset_index(drop=True)
