"""
event_price_engine package -- thin class-shaped facade over the
function-based modules (db.py, event_study.py, indicators.py, scoring.py,
pipeline.py), so bazaarflipper.py's

    from event_price_engine import Database, IndicatorEngine, EventStudy, ScoreEngine, Pipeline

works as written. Each class is a stateful, ergonomic wrapper -- no new
logic lives here, everything just delegates to the underlying module
functions so those stay independently testable/swappable exactly as
originally designed.
"""

import time
from typing import Optional

from . import db as _db
from . import event_study as _event_study
from . import indicators as _indicators
from . import scoring as _scoring
from . import pipeline as _pipeline

from .db import PricePoint, EventInstance, EventForecast, Session
from .event_study import EventStudyResult, RelativeDayStat
from .indicators import IndicatorContext, IndicatorResult, DEFAULT_INDICATORS
from .scoring import Recommendation


class Database:
    """Wraps db.py's connection + all loaders/writers, including the
    JSON-ingest bridge for bazaarflipper's price_history.json."""

    def __init__(self, db_path: str):
        self.path = db_path
        self.conn = _db.connect(db_path)

    # -- price history --
    def import_price_history_json(self, json_path: str) -> int:
        return _db.import_price_history_json(self.conn, json_path)

    def ingest_price_samples(self, samples_by_item: dict) -> int:
        return _db.ingest_price_samples(self.conn, samples_by_item)

    def export_price_history_gz(self, out_path: str, item_id: Optional[str] = None):
        _db.export_price_history_gz(self.conn, out_path, item_id)

    def load_price_history(self, item_id: str, start_ts=None, end_ts=None) -> list:
        return _db.load_price_history(self.conn, item_id, start_ts, end_ts)

    def vacuum(self):
        _db.vacuum(self.conn)

    # -- events --
    def upsert_event_instance(self, event_instance_id: str, event_type: str,
                               start_ts: int, end_ts: Optional[int] = None):
        _db.upsert_event_instance(self.conn, event_instance_id, event_type, start_ts, end_ts)

    def load_event_instances(self, event_type: str, before_ts=None) -> list:
        return _db.load_event_instances(self.conn, event_type, before_ts)

    def load_upcoming_events(self, after_ts=None) -> list:
        return _db.load_upcoming_events(self.conn, after_ts)

    # -- event forecasts (predicted next start per event type) --
    def upsert_event_forecast(self, event_type: str, next_start_ts: int, computed_ts: int,
                               within_lead: bool = False, source: Optional[str] = None):
        _db.upsert_event_forecast(self.conn, event_type, next_start_ts, computed_ts,
                                   within_lead, source)

    def load_event_forecasts(self, within_lead_only: bool = False) -> list:
        return _db.load_event_forecasts(self.conn, within_lead_only)

    def load_event_forecast(self, event_type: str):
        return _db.load_event_forecast(self.conn, event_type)

    # -- app sessions (boot/close tracking, for gap-aware confidence) --
    def resume_or_start_session(self, now_ts: int) -> int:
        """Closes out any session orphaned by a crash/force-quit last run,
        then opens a fresh one for this run. Call once per process, when the
        bridge's DB connection is first created."""
        _db.close_stale_sessions(self.conn, now_ts)
        return _db.start_session(self.conn, now_ts)

    def heartbeat_session(self, session_id: int, ts: int):
        _db.heartbeat_session(self.conn, session_id, ts)

    def close_session(self, session_id: int, end_ts: int):
        _db.close_session(self.conn, session_id, end_ts)

    def load_sessions(self, after_ts: Optional[int] = None) -> list:
        return _db.load_sessions(self.conn, after_ts)

    def session_coverage(self, start_ts: int, end_ts: int) -> float:
        return _db.session_coverage(self.conn, start_ts, end_ts)

    # -- item/event map --
    def upsert_item_event_map(self, item_id: str, event_type: str):
        _db.upsert_item_event_map(self.conn, item_id, event_type)

    def load_items_for_event_type(self, event_type: str) -> list:
        return _db.load_items_for_event_type(self.conn, event_type)

    # -- recommendations log --
    def log_recommendation(self, rec: dict) -> int:
        return _db.log_recommendation(self.conn, rec)

    def record_outcome(self, recommendation_id: int, price_at_eval: float):
        _db.record_outcome(self.conn, recommendation_id, price_at_eval)

    def close(self):
        self.conn.close()


class EventStudy:
    """Wraps event_study.py's free functions for one (item, event_type)
    pair at a time."""

    def __init__(self, window=_event_study.DEFAULT_SEARCH_WINDOW):
        self.window = window

    def run(self, price_points_by_instance: dict, instances: list,
            anchor: Optional[str] = None) -> Optional[_event_study.EventStudyResult]:
        return _event_study.run_event_study(price_points_by_instance, instances, self.window, anchor)

    def choose_best_anchor(self, price_points_by_instance: dict, instances: list) -> str:
        return _event_study.choose_best_anchor(price_points_by_instance, instances, self.window)


class IndicatorEngine:
    """Wraps indicators.py's registry so callers can run all default (or
    custom) indicators against a context without importing the module
    functions directly."""

    def __init__(self, indicators=None):
        self.indicators = indicators or _indicators.DEFAULT_INDICATORS

    def run_all(self, ctx: IndicatorContext) -> list:
        return [fn(ctx) for fn, _kind, _weight in self.indicators]

    def days_to_peak_estimate(self, ctx: IndicatorContext) -> float:
        return _indicators.days_to_peak_estimate(ctx)


class ScoreEngine:
    """Wraps scoring.py's combination + confidence logic."""

    def __init__(self, indicators=None):
        self.indicators = indicators

    def score(self, item_id: str, event_type: str, event_instance_id: str,
              ctx: IndicatorContext, event_study_result) -> Recommendation:
        return _scoring.score_recommendation(
            item_id, event_type, event_instance_id, ctx, event_study_result, self.indicators
        )

    def historical_accuracy_for_event_type(self, db: Database, event_type: str,
                                            tolerance_pct: float = 3.0) -> Optional[float]:
        return _scoring.historical_accuracy_for_event_type(db.conn, event_type, tolerance_pct)


class Pipeline:
    """Top-level facade matching pipeline.py's entry points. Holds a
    Database internally so bazaarflipper only needs to construct one
    object to get recommendations end-to-end."""

    def __init__(self, db_path: str, min_liquidity_volume: float = _pipeline.DEFAULT_MIN_LIQUIDITY_VOLUME,
                 indicators=None):
        self.db = Database(db_path)
        self.min_liquidity_volume = min_liquidity_volume
        self.indicators = indicators

    def generate_recommendation(self, item_id: str, upcoming_instance: EventInstance,
                                 as_of_ts: Optional[int] = None) -> Optional[Recommendation]:
        return _pipeline.generate_recommendation(
            self.db.conn, item_id, upcoming_instance,
            self.min_liquidity_volume, as_of_ts, self.indicators
        )

    def generate_recommendation_for_forecast(self, item_id: str, event_type: str,
                                              next_start_ts: int,
                                              as_of_ts: Optional[int] = None) -> Optional[Recommendation]:
        """Pre-event signal for a not-yet-started event forecast to begin at
        next_start_ts. Delegates to pipeline.generate_recommendation_for_forecast,
        which scores against a synthetic future instance without touching the
        events table."""
        return _pipeline.generate_recommendation_for_forecast(
            self.db.conn, item_id, event_type, next_start_ts,
            self.min_liquidity_volume, as_of_ts, self.indicators
        )

    def run_for_upcoming_events(self, as_of_ts: Optional[int] = None, log: bool = True) -> list:
        return _pipeline.run_for_upcoming_events(
            self.db.conn, self.min_liquidity_volume, as_of_ts, log
        )

    def sync_price_history(self, json_path: str) -> int:
        """Convenience passthrough so bazaarflipper's refresh loop can do
        pipeline.sync_price_history(PRICE_HISTORY_PATH) each tick without
        reaching into .db directly."""
        return self.db.import_price_history_json(json_path)

    def close(self):
        self.db.close()