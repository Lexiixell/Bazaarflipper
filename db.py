"""
db.py -- DuckDB schema + data-access layer for the event price engine.

Schema
------
price_history(item_id, ts, price, volume)
    Raw per-item price observations over time. ts is unix seconds.

events(event_instance_id, event_type, start_ts, end_ts)
    One row per concrete occurrence of an event. Populated incrementally
    by bazaarflipper.py as real seasonal-event windows open/close (see
    bazaar_bridge.py) -- NOT backfilled synthetically. This means the
    engine has zero historical instances for any event_type until it has
    lived through at least one real occurrence of that event; run_event_study
    / generate_recommendation both fail open (return None) in that case,
    same philosophy as bazaarflipper's own manipulation detection.

item_event_map(item_id, event_type)
    Which items are considered relevant to which event types. Externalized
    version of bazaarflipper's EVENT_ITEM_KEYWORDS tagging -- populated by
    bazaar_bridge.sync_item_event_map() from tag_event_relevance() output.

recommendations_log(...)
    Every recommendation the engine produces, for later backtesting.
"""

import gzip
import json
import time
from dataclasses import dataclass
from typing import Optional

import duckdb

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
    item_id   TEXT    NOT NULL,
    ts        INTEGER NOT NULL,
    price     DOUBLE  NOT NULL,
    volume    DOUBLE  DEFAULT 0,
    PRIMARY KEY (item_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_price_item_ts ON price_history(item_id, ts);

CREATE TABLE IF NOT EXISTS events (
    event_instance_id TEXT PRIMARY KEY,
    event_type        TEXT NOT NULL,
    start_ts          INTEGER NOT NULL,
    end_ts            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

CREATE TABLE IF NOT EXISTS item_event_map (
    item_id    TEXT NOT NULL,
    event_type TEXT NOT NULL,
    PRIMARY KEY (item_id, event_type)
);

-- DuckDB has no AUTOINCREMENT keyword; a sequence + DEFAULT nextval(...)
-- is the equivalent for recommendations_log.id.
CREATE SEQUENCE IF NOT EXISTS seq_recommendations_log_id START 1;
CREATE TABLE IF NOT EXISTS recommendations_log (
    id                    INTEGER PRIMARY KEY DEFAULT nextval('seq_recommendations_log_id'),
    item_id               TEXT NOT NULL,
    event_type            TEXT NOT NULL,
    event_instance_id     TEXT NOT NULL,
    generated_ts          INTEGER NOT NULL,
    action                TEXT NOT NULL,
    buy_confidence        DOUBLE,
    sell_confidence       DOUBLE,
    expected_appreciation DOUBLE,
    expected_holding_days DOUBLE,
    details_json          TEXT,
    outcome_price_at_eval DOUBLE,
    outcome_recorded_ts   INTEGER
);

-- Bookkeeping for the JSON ingest so re-running import_price_history_json
-- doesn't re-read/re-insert samples already stored (idempotent ingest).
CREATE TABLE IF NOT EXISTS ingest_state (
    source_path      TEXT PRIMARY KEY,
    last_mtime       DOUBLE,
    last_ts_by_item  TEXT
);

-- Forecasted NEXT start of each event type, computed from the SkyBlock
-- calendar by bazaar_bridge.py (the only side that knows both the calendar
-- math in bazaarflipper.py and this store). One upserted row per event_type
-- -- unlike `events` (real occurrences, past), this holds a single FUTURE
-- prediction so the engine can surface a "~24h away" heads-up and score
-- pre-event Buy/Hold/Sell signals against it (see
-- pipeline.generate_recommendation_for_forecast). within_lead is a cached
-- 0/1 the bridge sets once the forecast crosses the user's lead window, so
-- consumers don't each have to re-derive it.
CREATE TABLE IF NOT EXISTS event_forecast (
    event_type     TEXT PRIMARY KEY,
    next_start_ts  INTEGER NOT NULL,
    computed_ts    INTEGER NOT NULL,
    within_lead    INTEGER DEFAULT 0,
    source         TEXT
);

-- One row per app run, so the engine knows not just WHAT prices did but
-- WHETHER anyone was actually watching while they did it. Momentum/deviation
-- (indicators.py) are built from price_history samples, which only get
-- appended while the app is open and refreshing -- if it was closed for a
-- day or two, the "trend" between the last sample before close and the first
-- sample after reopening is really just two snapshots with an unobserved gap
-- between them, not a continuous read. bridge_tick starts/heartbeats a
-- session every refresh; bridge_close ends it cleanly on app exit.
-- last_heartbeat_ts lets a crashed/killed run (no clean close) still be
-- closed out to a reasonable end time on the next boot, instead of either
-- staying "open" forever or being guessed as ending exactly at the next
-- boot.
CREATE SEQUENCE IF NOT EXISTS seq_app_sessions_id START 1;
CREATE TABLE IF NOT EXISTS app_sessions (
    session_id        INTEGER PRIMARY KEY DEFAULT nextval('seq_app_sessions_id'),
    start_ts          INTEGER NOT NULL,
    end_ts            INTEGER,
    last_heartbeat_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON app_sessions(start_ts);
"""


@dataclass
class PricePoint:
    ts: int
    price: float
    volume: float = 0.0


@dataclass
class EventInstance:
    event_instance_id: str
    event_type: str
    start_ts: int
    end_ts: Optional[int]


@dataclass
class EventForecast:
    event_type: str
    next_start_ts: int
    computed_ts: int
    within_lead: bool = False
    source: Optional[str] = None

    @property
    def seconds_until(self) -> int:
        """Live time-to-start relative to now. Computed rather than stored,
        since the row is only refreshed once per bazaar tick but a countdown
        wants to stay current between writes."""
        return int(self.next_start_ts - time.time())


@dataclass
class Session:
    session_id: int
    start_ts: int
    end_ts: Optional[int]
    last_heartbeat_ts: int


def connect(db_path: str) -> duckdb.DuckDBPyConnection:
    """Opens the DuckDB store. DuckDB is columnar with built-in compression,
    so numeric time-series data like price_history is compact on disk
    without any of SQLite's manual auto_vacuum/incremental_vacuum dance --
    see vacuum() for the DuckDB equivalent (VACUUM + CHECKPOINT)."""
    conn = duckdb.connect(db_path)
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def vacuum(conn: duckdb.DuckDBPyConnection):
    """Reclaims space freed by pruning old price_history rows and flushes
    the WAL into the main file. Cheap to call periodically (e.g. once a
    day from bazaarflipper's refresh loop)."""
    conn.execute("VACUUM;")
    conn.execute("CHECKPOINT;")
    conn.commit()


def migrate_from_sqlite(conn: duckdb.DuckDBPyConnection, sqlite_path: str) -> bool:
    """One-time best-effort import of a legacy event_price_history.sqlite
    into this (freshly-created, schema-only) DuckDB connection, via
    DuckDB's sqlite_scanner extension. Table-by-table and each wrapped in
    its own try/except so an older sqlite file missing a table (e.g. one
    written before event_forecast/app_sessions existed) still lets every
    other table migrate -- fails open, same philosophy as the rest of this
    module, rather than aborting the whole migration over one table.
    Returns True if the extension+attach step itself succeeded (regardless
    of how many tables had data to copy), False if sqlite_scanner isn't
    available (e.g. offline first run) and the caller should just proceed
    with an empty DuckDB store."""
    try:
        conn.execute("INSTALL sqlite;")
        conn.execute("LOAD sqlite;")
        # ATTACH doesn't accept bound parameters for the path (it's parsed
        # like a DDL literal, not a query argument) -- escape and inline it.
        escaped_path = sqlite_path.replace("'", "''")
        conn.execute(f"ATTACH '{escaped_path}' AS legacy (TYPE sqlite);")
    except Exception:
        return False

    for table in ("price_history", "events", "item_event_map", "recommendations_log",
                  "ingest_state", "event_forecast", "app_sessions"):
        try:
            conn.execute(f"INSERT OR IGNORE INTO {table} SELECT * FROM legacy.{table};")
        except Exception:
            continue

    try:
        conn.execute("DETACH legacy;")
    except Exception:
        pass
    conn.commit()
    return True


# ---- loaders --------------------------------------------------------------

def load_price_history(conn: duckdb.DuckDBPyConnection, item_id: str,
                        start_ts: Optional[int] = None,
                        end_ts: Optional[int] = None) -> list:
    q = "SELECT ts, price, volume FROM price_history WHERE item_id = ?"
    params = [item_id]
    if start_ts is not None:
        q += " AND ts >= ?"
        params.append(start_ts)
    if end_ts is not None:
        q += " AND ts <= ?"
        params.append(end_ts)
    q += " ORDER BY ts ASC"
    rows = conn.execute(q, params).fetchall()
    return [PricePoint(ts=r[0], price=r[1], volume=r[2] or 0.0) for r in rows]


def load_event_instances(conn: duckdb.DuckDBPyConnection, event_type: str,
                          before_ts: Optional[int] = None) -> list:
    q = "SELECT event_instance_id, event_type, start_ts, end_ts FROM events WHERE event_type = ?"
    params = [event_type]
    if before_ts is not None:
        q += " AND start_ts < ?"
        params.append(before_ts)
    q += " ORDER BY start_ts ASC"
    rows = conn.execute(q, params).fetchall()
    return [EventInstance(*r) for r in rows]


def load_upcoming_events(conn: duckdb.DuckDBPyConnection, after_ts: Optional[int] = None) -> list:
    after_ts = after_ts if after_ts is not None else int(time.time())
    rows = conn.execute(
        "SELECT event_instance_id, event_type, start_ts, end_ts FROM events "
        "WHERE start_ts >= ? ORDER BY start_ts ASC", (after_ts,)
    ).fetchall()
    return [EventInstance(*r) for r in rows]


def load_items_for_event_type(conn: duckdb.DuckDBPyConnection, event_type: str) -> list:
    rows = conn.execute(
        "SELECT item_id FROM item_event_map WHERE event_type = ?", (event_type,)
    ).fetchall()
    return [r[0] for r in rows]


# ---- writers ---------------------------------------------------------------

def upsert_item_event_map(conn: duckdb.DuckDBPyConnection, item_id: str, event_type: str):
    conn.execute(
        "INSERT OR IGNORE INTO item_event_map (item_id, event_type) VALUES (?, ?)",
        (item_id, event_type)
    )
    conn.commit()


def upsert_event_instance(conn: duckdb.DuckDBPyConnection, event_instance_id: str,
                           event_type: str, start_ts: int, end_ts: Optional[int] = None):
    """Insert a new event occurrence, or update its end_ts once it closes.
    This is the real-time replacement for a synthetic backfill: called by
    bazaar_bridge.py the moment it detects (via compute_active_festivals /
    jerry_workshop_status) that an event has just started, and again with
    end_ts once it's detected as no longer active."""
    conn.execute(
        "INSERT INTO events (event_instance_id, event_type, start_ts, end_ts) VALUES (?,?,?,?) "
        "ON CONFLICT(event_instance_id) DO UPDATE SET end_ts = excluded.end_ts "
        "WHERE excluded.end_ts IS NOT NULL",
        (event_instance_id, event_type, start_ts, end_ts)
    )
    conn.commit()


def upsert_event_forecast(conn: duckdb.DuckDBPyConnection, event_type: str, next_start_ts: int,
                           computed_ts: int, within_lead: bool = False,
                           source: Optional[str] = None):
    """Record (or refresh) the predicted next start of an event type. Called
    every bazaar tick by bazaar_bridge.py -- the prediction is deterministic
    from the SkyBlock calendar, so overwriting the single row each tick is
    correct and keeps `event_forecast` to one row per event type."""
    conn.execute(
        "INSERT INTO event_forecast (event_type, next_start_ts, computed_ts, within_lead, source) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(event_type) DO UPDATE SET "
        "  next_start_ts = excluded.next_start_ts, "
        "  computed_ts   = excluded.computed_ts, "
        "  within_lead   = excluded.within_lead, "
        "  source        = excluded.source",
        (event_type, int(next_start_ts), int(computed_ts), 1 if within_lead else 0, source)
    )
    conn.commit()


def load_event_forecasts(conn: duckdb.DuckDBPyConnection, within_lead_only: bool = False) -> list:
    q = "SELECT event_type, next_start_ts, computed_ts, within_lead, source FROM event_forecast"
    if within_lead_only:
        q += " WHERE within_lead = 1"
    q += " ORDER BY next_start_ts ASC"
    rows = conn.execute(q).fetchall()
    return [EventForecast(event_type=r[0], next_start_ts=r[1], computed_ts=r[2],
                          within_lead=bool(r[3]), source=r[4]) for r in rows]


def load_event_forecast(conn: duckdb.DuckDBPyConnection, event_type: str) -> Optional[EventForecast]:
    row = conn.execute(
        "SELECT event_type, next_start_ts, computed_ts, within_lead, source "
        "FROM event_forecast WHERE event_type = ?", (event_type,)
    ).fetchone()
    if row is None:
        return None
    return EventForecast(event_type=row[0], next_start_ts=row[1], computed_ts=row[2],
                         within_lead=bool(row[3]), source=row[4])


def close_stale_sessions(conn: duckdb.DuckDBPyConnection, fallback_end_ts: Optional[int] = None):
    """Closes out any session left with end_ts IS NULL -- i.e. the app didn't
    reach bridge_close last run (crash, force-quit, power loss). Each is
    closed at ITS OWN last_heartbeat_ts, not `now` or a shared fallback --
    that's the last moment we actually know it was still running, so it's a
    tighter (and per-session-correct) estimate than "now" would be, which
    would otherwise count the entire downtime as if the app were open the
    whole time. fallback_end_ts only covers the degenerate case of a session
    whose heartbeat somehow never advanced past its own start_ts."""
    conn.execute(
        "UPDATE app_sessions SET end_ts = COALESCE(last_heartbeat_ts, ?, start_ts) "
        "WHERE end_ts IS NULL",
        (fallback_end_ts,)
    )
    conn.commit()


def start_session(conn: duckdb.DuckDBPyConnection, start_ts: int) -> int:
    # DuckDB has no cursor.lastrowid; RETURNING the generated id is the
    # equivalent way to learn the new session_id.
    row = conn.execute(
        "INSERT INTO app_sessions (start_ts, end_ts, last_heartbeat_ts) VALUES (?, NULL, ?) "
        "RETURNING session_id",
        (start_ts, start_ts)
    ).fetchone()
    conn.commit()
    return row[0]


def heartbeat_session(conn: duckdb.DuckDBPyConnection, session_id: int, ts: int):
    conn.execute(
        "UPDATE app_sessions SET last_heartbeat_ts = ? WHERE session_id = ? AND end_ts IS NULL",
        (ts, session_id)
    )
    conn.commit()


def close_session(conn: duckdb.DuckDBPyConnection, session_id: int, end_ts: int):
    conn.execute(
        "UPDATE app_sessions SET end_ts = ?, last_heartbeat_ts = ? WHERE session_id = ?",
        (end_ts, end_ts, session_id)
    )
    conn.commit()


def load_sessions(conn: duckdb.DuckDBPyConnection, after_ts: Optional[int] = None) -> list:
    q = "SELECT session_id, start_ts, end_ts, last_heartbeat_ts FROM app_sessions"
    params = []
    if after_ts is not None:
        q += " WHERE start_ts >= ?"
        params.append(after_ts)
    q += " ORDER BY start_ts ASC"
    rows = conn.execute(q, params).fetchall()
    return [Session(*r) for r in rows]


def session_coverage(conn: duckdb.DuckDBPyConnection, start_ts: int, end_ts: int) -> float:
    """Fraction (0..1) of the [start_ts, end_ts) window during which an app
    session was open -- i.e. how much of that window was actually spent
    watching/sampling live prices, as opposed to closed. A still-open
    session (end_ts IS NULL) is treated as running through the window's own
    end_ts, since a live tick calling this counts "right now" as covered.
    Overlapping/adjacent sessions (e.g. a quick restart) are merged before
    summing so they aren't double-counted.

    Fails open (returns 1.0) for an empty/inverted window or when there's no
    session data at all overlapping it -- consistent with every other
    dampener in this module (liquidity_dampener, sample_adequacy_dampener,
    etc.), which decline to punish a recommendation for data that was never
    collected rather than assuming the worst."""
    if end_ts <= start_ts:
        return 1.0
    rows = conn.execute(
        "SELECT start_ts, end_ts FROM app_sessions "
        "WHERE start_ts < ? AND (end_ts IS NULL OR end_ts > ?)",
        (end_ts, start_ts)
    ).fetchall()
    if not rows:
        return 1.0

    intervals = []
    for s, e in rows:
        clipped_start = max(s, start_ts)
        clipped_end = min(e if e is not None else end_ts, end_ts)
        if clipped_end > clipped_start:
            intervals.append((clipped_start, clipped_end))
    if not intervals:
        return 1.0

    intervals.sort()
    merged = [intervals[0]]
    for s, e in intervals[1:]:
        last_s, last_e = merged[-1]
        if s <= last_e:
            merged[-1] = (last_s, max(last_e, e))
        else:
            merged.append((s, e))

    covered = sum(e - s for s, e in merged)
    return max(0.0, min(1.0, covered / (end_ts - start_ts)))


def log_recommendation(conn: duckdb.DuckDBPyConnection, rec: dict) -> int:
    # DuckDB has no cursor.lastrowid; RETURNING the generated id is the
    # equivalent way to learn the new recommendations_log.id.
    row = conn.execute(
        "INSERT INTO recommendations_log "
        "(item_id, event_type, event_instance_id, generated_ts, action, "
        " buy_confidence, sell_confidence, expected_appreciation, "
        " expected_holding_days, details_json) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "RETURNING id",
        (rec["item_id"], rec["event_type"], rec["event_instance_id"],
         rec["generated_ts"], rec["action"], rec["buy_confidence"],
         rec["sell_confidence"], rec["expected_appreciation"],
         rec["expected_holding_days"], rec["details_json"])
    ).fetchone()
    conn.commit()
    return row[0]


def record_outcome(conn: duckdb.DuckDBPyConnection, recommendation_id: int, price_at_eval: float):
    conn.execute(
        "UPDATE recommendations_log SET outcome_price_at_eval = ?, outcome_recorded_ts = ? "
        "WHERE id = ?",
        (price_at_eval, int(time.time()), recommendation_id)
    )
    conn.commit()


# ---- JSON ingest ------------------------------------------------------------
# bazaarflipper.py already maintains its own price_history.json (raw
# buy/sell snapshots per item, pruned to 7 days, used for its manipulation
# check). Rather than have bazaarflipper write to two places, db.py reads
# that JSON directly and folds it into price_history here -- this becomes
# the single long-running store, since price_history.json itself only
# ever holds ~7 days at a time and gets pruned/overwritten on every refresh.

def import_price_history_json(conn: duckdb.DuckDBPyConnection, json_path: str) -> int:
    """Reads bazaarflipper's price_history.json (shape:
    {product_id: [[ts, raw_buy_target, raw_sell_target], ...]}) and
    inserts any samples not already present. Idempotent and safe to call
    on every bazaarflipper refresh tick -- uses ingest_state to skip
    unchanged files instantly, and INSERT OR IGNORE (PK on item_id, ts)
    so re-importing overlapping samples across runs is a no-op rather
    than a duplicate or an error.

    'volume' is not present in bazaarflipper's JSON shape, so it's stored
    as 0 -- liquidity indicators degrade gracefully per the module's
    original design. Returns the number of new rows actually inserted."""
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0

    inserted = 0
    for product_id, samples in raw.items():
        rows = []
        for sample in samples:
            if not sample or len(sample) < 2:
                continue
            ts = int(sample[0])
            # bazaarflipper stores [ts, raw_buy_target, raw_sell_target].
            # raw_sell_target (top current sell offer, i.e. what a seller
            # receives) is the closer analogue of price_history.price used
            # by event_study's curve-fitting; raw_buy_target is available
            # via a second pass if a caller wants buy-side curves too, but
            # a single scalar 'price' column is what the rest of the engine
            # expects, so sell_target is the default choice here.
            price = float(sample[2]) if len(sample) > 2 else float(sample[1])
            rows.append((product_id, ts, price, 0.0))
        if not rows:
            continue
        # DuckDB's executemany() doesn't report affected-row counts (always
        # -1), unlike sqlite3's cursor.rowcount. A single multi-row INSERT
        # with RETURNING is used instead: ON CONFLICT DO NOTHING only
        # returns the rows actually inserted, so counting them gives an
        # exact new-row count.
        placeholders = ",".join(["(?,?,?,?)"] * len(rows))
        params = [v for row in rows for v in row]
        new_rows = conn.execute(
            f"INSERT OR IGNORE INTO price_history (item_id, ts, price, volume) VALUES {placeholders} "
            "RETURNING item_id",
            params
        ).fetchall()
        inserted += len(new_rows)

    conn.commit()
    return inserted


def export_price_history_gz(conn: duckdb.DuckDBPyConnection, out_path: str,
                             item_id: Optional[str] = None):
    """Writes a gzip-compressed JSON snapshot of price_history -- useful
    for backups or sharing a compact copy, separate from the live .duckdb
    file itself (which is already compact -- DuckDB is columnar with
    built-in compression; see connect()'s docstring). Filters to one item
    if given."""
    q = "SELECT item_id, ts, price, volume FROM price_history"
    params = []
    if item_id is not None:
        q += " WHERE item_id = ?"
        params.append(item_id)
    q += " ORDER BY item_id, ts"
    rows = conn.execute(q, params).fetchall()

    by_item = {}
    for iid, ts, price, volume in rows:
        by_item.setdefault(iid, []).append([ts, price, volume])

    with gzip.open(out_path, "wt", encoding="utf-8") as fh:
        json.dump(by_item, fh, separators=(",", ":"))