"""
bazaar_bridge.py -- the only file that needs to know about both
bazaarflipper.py and event_price_engine. Call `bridge_tick(app)` once per
bazaarflipper refresh (from BazaarFlipperApp._on_fetch_success, right
after self.price_history is updated) and it will:

  1. Detect real event start/end transitions using bazaarflipper's own
     compute_active_festivals() / jerry_workshop_status() output (already
     computed every refresh) and log them into the `events` table via
     Database.upsert_event_instance -- this is the option (b) approach:
     real event windows recorded as they actually occur, no synthetic
     backfill.
  2. Keep item_event_map in sync with bazaarflipper's own
     EVENT_ITEM_KEYWORDS / tag_event_relevance().
  3. Ingest bazaarflipper's price_history.json into the DuckDB store via
     Database.import_price_history_json (idempotent, cheap to call every
     tick).
"""

import time
from typing import Optional

from event_price_engine import Database


DB_FILENAME = "event_price_history.duckdb"
LEGACY_SQLITE_FILENAME = "event_price_history.sqlite"

# All event keys the bridge tracks as concrete event instances (with
# start/end timestamps). "dungeon_supply" is deliberately excluded --
# it's tied to Paul's perk being in office at all, with no start/end
# the way a festival window has.
TRACKED_FESTIVAL_KEYS = {
    "mining_fiesta",
    "fishing_festival",
    "mythological_ritual",
    "harvest_festival",
    "oringo",
    "year_of_pig",
}


class BridgeState:
    """Tracks which event_keys were active as of the last tick, purely
    in-memory, so bridge_tick can detect start/end *transitions* rather
    than re-upserting the same open instance every refresh. Rebuilt from
    the DB's own open (end_ts IS NULL) rows on first use, so a restart
    mid-event doesn't lose track of it."""

    def __init__(self, db: Database):
        self.db = db
        self.open_instance_id_by_key = self._load_open_instances()
        # event_key -> bool: was this event inside the user's lead window as
        # of the last tick? Seeded from whatever the DB last persisted so a
        # restart doesn't re-fire a "just entered 24h window" transition for
        # an event that was already flagged before the restart.
        self.event_in_lead_window = self._load_in_lead_flags()
        # This run's app_sessions row. resume_or_start_session first closes
        # out any session orphaned by a crash/force-quit last run (using its
        # own last heartbeat as the end, not "now" -- see db.close_stale_sessions),
        # then opens a fresh one for THIS run. One BridgeState lives for the
        # whole process (cached on app._event_price_bridge_state), so this
        # only runs once per boot, exactly matching "when it was booted."
        self.session_id = db.resume_or_start_session(int(time.time()))

    def _load_open_instances(self) -> dict:
        rows = self.db.conn.execute(
            "SELECT event_instance_id, event_type FROM events WHERE end_ts IS NULL"
        ).fetchall()
        return {event_type: event_instance_id for event_instance_id, event_type in rows}

    def _load_in_lead_flags(self) -> dict:
        return {fc.event_type: fc.within_lead
                for fc in self.db.load_event_forecasts()}


def _make_instance_id(event_key: str, start_ts: int) -> str:
    return f"{event_key}:{start_ts}"


def _sync_festival_transitions(db: Database, state: BridgeState, active_festivals: list,
                                jerry_active: bool, harvest_active: bool,
                                oringo_active: bool, year_of_pig_active: bool,
                                now_ts: int):
    """Detect event start/end transitions and record them in the DB.

    `active_festivals` comes from bazaarflipper.compute_active_festivals()
    and covers perk-gated events (Mining Fiesta, Fishing Festival,
    Mythological Ritual). The remaining booleans cover calendar-gated
    events that don't depend on which mayor is elected."""
    currently_active_keys = {f["event_key"] for f in active_festivals if f["active_now"]}
    if jerry_active:
        currently_active_keys.add("jerry_workshop")
    if harvest_active:
        currently_active_keys.add("harvest_festival")
    if oringo_active:
        currently_active_keys.add("oringo")
    if year_of_pig_active:
        currently_active_keys.add("year_of_pig")
    currently_active_keys &= (TRACKED_FESTIVAL_KEYS | {"jerry_workshop"})

    previously_open_keys = set(state.open_instance_id_by_key.keys())

    # Newly started: not open before, active now.
    for key in currently_active_keys - previously_open_keys:
        instance_id = _make_instance_id(key, now_ts)
        db.upsert_event_instance(instance_id, key, start_ts=now_ts, end_ts=None)
        state.open_instance_id_by_key[key] = instance_id

    # Just ended: open before, not active now -- close it out with end_ts.
    for key in previously_open_keys - currently_active_keys:
        instance_id = state.open_instance_id_by_key.pop(key)
        db.upsert_event_instance(instance_id, key, start_ts=0, end_ts=now_ts)


def _sync_item_event_map(db: Database, all_flips: list, tag_event_relevance_fn):
    """Re-derives item_event_map from bazaarflipper's own tagging function
    on every tick -- cheap (INSERT OR IGNORE, PK-deduped) and keeps it
    current as Hypixel adds new item ids that match existing keywords."""
    for flip in all_flips:
        product_id = flip["id"]
        for event_key in tag_event_relevance_fn(product_id):
            if event_key in TRACKED_FESTIVAL_KEYS | {"jerry_workshop"}:
                db.upsert_item_event_map(product_id, event_key)


def _sync_event_forecasts(db: Database, state: BridgeState, forecasts: list,
                           lead_seconds: float, now_ts: int):
    """Persist each event's predicted next start into the event_forecast table
    and detect which events have JUST crossed into the lead window since the
    last tick.

    `forecasts` is bazaarflipper.forecast_events() output. An event counts as
    "in the lead window" only if it recurs rarely enough for the window to
    mean something (recurrence_seconds > lead_seconds) and its start is now
    within lead_seconds. Returns the list of event_keys that transitioned from
    outside->inside the window this tick (for a one-time heads-up), so callers
    don't re-announce the same upcoming event every refresh."""
    entered_lead = []
    for fc in forecasts:
        event_key = fc["event_key"]
        seconds_until = fc["next_start_ts"] - now_ts
        within = (fc["recurrence_seconds"] > lead_seconds
                   and 0 < seconds_until <= lead_seconds)
        db.upsert_event_forecast(event_key, fc["next_start_ts"], now_ts,
                                  within_lead=within, source=fc.get("source"))
        if within and not state.event_in_lead_window.get(event_key, False):
            entered_lead.append(event_key)
        state.event_in_lead_window[event_key] = within
    return entered_lead


def bridge_tick(app, db_dir: Optional[str] = None):
    """Call once per bazaarflipper refresh, after self.all_flips,
    self.active_festivals, self.jerry_status, self.harvest_status,
    self.oringo_status, self.year_of_pig_status, and self.price_history
    are all up to date for this tick."""
    import os

    if db_dir is None:
        from bazaarflipper import APP_DATA_DIR
        db_dir = APP_DATA_DIR
    db_path = os.path.join(db_dir, DB_FILENAME)

    db = getattr(app, "_event_price_db", None)
    state = getattr(app, "_event_price_bridge_state", None)
    if db is None:
        legacy_path = os.path.join(db_dir, LEGACY_SQLITE_FILENAME)
        needs_migration = not os.path.exists(db_path) and os.path.exists(legacy_path)

        db = Database(db_path)
        if needs_migration:
            # One-time carryover from the old SQLite store into the fresh
            # DuckDB one -- see db.migrate_from_sqlite. Best-effort: if it
            # fails (e.g. no network for the sqlite_scanner extension on
            # first use), the app just proceeds with an empty DuckDB store
            # rather than blocking startup on it.
            try:
                from . import db as _db
                _db.migrate_from_sqlite(db.conn, legacy_path)
            except Exception:
                import traceback
                traceback.print_exc()
        state = BridgeState(db)
        app._event_price_db = db
        app._event_price_bridge_state = state

    now_ts = int(time.time())

    # Every successful refresh means the app was demonstrably open and
    # sampling prices right now -- advance this run's session so
    # session_coverage_dampener (indicators.py) can tell a continuously-open
    # stretch apart from one with hours/days of unwatched gap in the middle.
    try:
        db.heartbeat_session(state.session_id, now_ts)
    except Exception:
        import traceback
        traceback.print_exc()

    jerry_active = bool(app.jerry_status.get("active"))
    harvest_active = bool(getattr(app, "harvest_status", {}).get("active"))
    oringo_active = bool(getattr(app, "oringo_status_info", {}).get("active"))
    year_of_pig_active = bool(getattr(app, "year_of_pig_status_info", {}).get("active"))

    _sync_festival_transitions(db, state, app.active_festivals,
                                jerry_active, harvest_active,
                                oringo_active, year_of_pig_active,
                                now_ts)

    from bazaarflipper import (tag_event_relevance, PRICE_HISTORY_PATH,
                                forecast_events, DEFAULT_EVENT_LEAD_HOURS)
    _sync_item_event_map(db, app.all_flips, tag_event_relevance)

    inserted = db.import_price_history_json(PRICE_HISTORY_PATH)

    # Forecast the next start of each cleanly-forecastable event and record it,
    # so the engine can flag "~24h away" and pre-position related items. Uses
    # the app's configured lead window (Settings) when present, else the
    # default. Fails open like the rest of the bridge - a bad forecast must
    # never break the price ingest above.
    entered_lead = []
    try:
        lead_seconds = float(getattr(app, "event_lead_seconds",
                                      DEFAULT_EVENT_LEAD_HOURS * 3600))
        forecasts = forecast_events(now_ts, app.mayor_info)
        entered_lead = _sync_event_forecasts(db, state, forecasts, lead_seconds, now_ts)
    except Exception:
        import traceback
        traceback.print_exc()

    return {
        "new_price_rows": inserted,
        "open_events": list(state.open_instance_id_by_key.keys()),
        "events_entering_lead_window": entered_lead,
    }


def bridge_close(app):
    """Call once from BazaarFlipperApp.on_close, before the app exits, so
    this run's session gets an exact end_ts instead of being recovered later
    (on the NEXT boot) via close_stale_sessions' last-heartbeat fallback --
    that fallback is only ever a few minutes off (bounded by the refresh
    interval), but an explicit close is exact and means "how long was I
    actually open" doesn't quietly lose that last few minutes every single
    run. No-ops if bridge_tick never ran this session (e.g. app closed
    before the first successful fetch)."""
    db = getattr(app, "_event_price_db", None)
    state = getattr(app, "_event_price_bridge_state", None)
    if db is None or state is None:
        return
    try:
        db.close_session(state.session_id, int(time.time()))
    except Exception:
        import traceback
        traceback.print_exc()
