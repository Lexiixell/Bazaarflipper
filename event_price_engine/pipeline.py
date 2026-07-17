"""
pipeline.py -- orchestrates db.py, event_study.py, indicators.py, scoring.py.
"""

import statistics
import time
from typing import Optional

from . import db
from . import event_study
from . import scoring
from .indicators import IndicatorContext


DEFAULT_MIN_LIQUIDITY_VOLUME = 0.0
RECENT_WINDOW_DAYS = 14


def _recent_prices_and_volumes(conn, item_id, as_of_ts, days=RECENT_WINDOW_DAYS):
    start_ts = as_of_ts - days * 86400
    points = db.load_price_history(conn, item_id, start_ts=start_ts, end_ts=as_of_ts)
    recent_prices = [(p.ts, p.price) for p in points]
    recent_volumes = [(p.ts, p.volume) for p in points]
    return recent_prices, recent_volumes


def _current_price(conn, item_id, as_of_ts):
    points = db.load_price_history(conn, item_id, end_ts=as_of_ts)
    return points[-1].price if points else None


def build_event_study_for(conn, event_type: str, upcoming_instance: db.EventInstance,
                            window=event_study.DEFAULT_SEARCH_WINDOW):
    historical_instances = db.load_event_instances(conn, event_type, before_ts=upcoming_instance.start_ts)
    return historical_instances


def run_event_study_for_item(conn, item_id: str, event_type: str,
                               historical_instances,
                               window=event_study.DEFAULT_SEARCH_WINDOW,
                               anchor: Optional[str] = None):
    price_points_by_instance = {}
    for inst in historical_instances:
        if inst.end_ts is None:
            continue
        lo, hi = window
        start_ts = inst.start_ts + lo * 86400
        end_ts = inst.start_ts + hi * 86400
        padded_start = min(start_ts, inst.end_ts + lo * 86400)
        padded_end = max(end_ts, inst.end_ts + hi * 86400)
        points = db.load_price_history(conn, item_id, start_ts=padded_start, end_ts=padded_end)
        price_points_by_instance[inst.event_instance_id] = points

    closed_instances = [i for i in historical_instances if i.end_ts is not None]
    return event_study.run_event_study(price_points_by_instance, closed_instances, window, anchor)


def generate_recommendation(conn, item_id: str, upcoming_instance: db.EventInstance,
                              min_liquidity_volume: float = DEFAULT_MIN_LIQUIDITY_VOLUME,
                              as_of_ts: Optional[int] = None,
                              indicators=None) -> Optional[scoring.Recommendation]:
    as_of_ts = as_of_ts if as_of_ts is not None else int(time.time())
    event_type = upcoming_instance.event_type

    historical_instances = build_event_study_for(conn, event_type, upcoming_instance)
    if not historical_instances:
        return None

    study = run_event_study_for_item(conn, item_id, event_type, historical_instances)
    if study is None:
        return None

    current_price = _current_price(conn, item_id, as_of_ts)
    if current_price is None:
        return None

    today_relative_day = int(round((as_of_ts - upcoming_instance.start_ts) / 86400.0))
    if study.anchor == "end":
        # stats_by_day / expected_curve are indexed relative to each
        # historical instance's END, so "today" needs to be expressed on
        # that same basis. If the current instance has already ended,
        # its own end_ts is exact. If it's still ongoing (the common
        # case -- this runs against the live/open instance most ticks),
        # there's no real end_ts yet to anchor against; the old code
        # silently fell back to start_ts instead, which put "today" on
        # the wrong number line entirely (start-based day N compared
        # against end-based day N), producing directional_position and
        # deviation signals that were comparing unrelated points on the
        # curve rather than merely being noisy. Estimate the end from
        # the average duration of past closed occurrences instead, so
        # "today" still lands in roughly the right place on the
        # end-based curve while the event is in progress.
        if upcoming_instance.end_ts is not None:
            anchor_ts = upcoming_instance.end_ts
        else:
            closed_durations = [inst.end_ts - inst.start_ts for inst in historical_instances
                                 if inst.end_ts is not None]
            avg_duration = statistics.mean(closed_durations) if closed_durations else 0
            anchor_ts = upcoming_instance.start_ts + avg_duration
        today_relative_day = int(round((as_of_ts - anchor_ts) / 86400.0))

    recent_prices, recent_volumes = _recent_prices_and_volumes(conn, item_id, as_of_ts)

    ctx = IndicatorContext(
        today_relative_day=today_relative_day,
        today_price=current_price,
        recent_prices=recent_prices,
        recent_volumes=recent_volumes,
        stats_by_day=study.stats_by_day,
        expected_curve=study.expected_curve,
        buy_window=study.buy_window,
        sell_window=study.sell_window,
        min_liquidity_volume=min_liquidity_volume,
    )

    rec = scoring.score_recommendation(
        item_id=item_id,
        event_type=event_type,
        event_instance_id=upcoming_instance.event_instance_id,
        ctx=ctx,
        event_study_result=study,
        indicators=indicators,
    )
    return rec


def run_for_upcoming_events(conn, min_liquidity_volume: float = DEFAULT_MIN_LIQUIDITY_VOLUME,
                              as_of_ts: Optional[int] = None, log=True):
    as_of_ts = as_of_ts if as_of_ts is not None else int(time.time())
    upcoming = db.load_upcoming_events(conn, after_ts=as_of_ts)
    recommendations = []

    for instance in upcoming:
        items = db.load_items_for_event_type(conn, instance.event_type)
        for item_id in items:
            rec = generate_recommendation(conn, item_id, instance, min_liquidity_volume, as_of_ts)
            if rec is None:
                continue
            recommendations.append(rec)
            if log:
                db.log_recommendation(conn, {
                    "item_id": rec.item_id,
                    "event_type": rec.event_type,
                    "event_instance_id": rec.event_instance_id,
                    "generated_ts": rec.generated_ts,
                    "action": rec.action,
                    "buy_confidence": rec.buy_confidence,
                    "sell_confidence": rec.sell_confidence,
                    "expected_appreciation": rec.expected_appreciation,
                    "expected_holding_days": rec.expected_holding_days,
                    "details_json": rec.details_json,
                })

    return recommendations