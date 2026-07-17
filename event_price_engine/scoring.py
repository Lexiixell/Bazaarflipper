"""
scoring.py -- combination layer + confidence scoring.
"""

import json
import sqlite3
import time
from dataclasses import dataclass, asdict
from typing import Optional

from .indicators import IndicatorContext, DEFAULT_INDICATORS, days_to_peak_estimate


@dataclass
class Recommendation:
    item_id: str
    event_type: str
    event_instance_id: str
    generated_ts: int
    action: str
    buy_confidence: float
    sell_confidence: float
    expected_appreciation: float
    expected_holding_days: float
    explanation: list
    details_json: str


def combine_indicators(ctx: IndicatorContext, indicators=None):
    indicators = indicators or DEFAULT_INDICATORS
    results = []
    weighted_sum = 0.0
    weight_total = 0.0
    dampener_product = 1.0

    for fn, kind, weight in indicators:
        result = fn(ctx)
        results.append(result)
        if kind == "directional":
            weighted_sum += result.score * weight
            weight_total += weight
        elif kind == "dampener":
            dampener_product *= max(0.0, min(1.0, result.score))

    raw_score = (weighted_sum / weight_total) if weight_total > 0 else 0.0
    return raw_score, dampener_product, results


def _score_to_confidence_pct(raw_score: float, dampener: float, direction: str) -> float:
    if direction == "buy":
        directional_component = max(0.0, -raw_score)
    else:
        directional_component = max(0.0, raw_score)
    return round(directional_component * dampener * 100.0, 1)


def score_recommendation(item_id: str, event_type: str, event_instance_id: str,
                          ctx: IndicatorContext, event_study_result,
                          indicators=None) -> Recommendation:
    raw_score, dampener, results = combine_indicators(ctx, indicators)

    buy_conf = _score_to_confidence_pct(raw_score, dampener, "buy")
    sell_conf = _score_to_confidence_pct(raw_score, dampener, "sell")

    if buy_conf >= sell_conf and buy_conf >= 40:
        action = "buy"
    elif sell_conf > buy_conf and sell_conf >= 40:
        action = "sell"
    else:
        action = "hold"

    buy_price = event_study_result.buy_window_price
    sell_price = event_study_result.sell_window_price
    expected_appreciation = (
        ((sell_price - buy_price) / buy_price) * 100.0 if buy_price else 0.0
    )
    expected_holding_days = max(0.0, days_to_peak_estimate(ctx))

    explanation = [r.explanation for r in results]
    details = {
        "raw_directional_score": round(raw_score, 3),
        "dampener_multiplier": round(dampener, 3),
        "indicators": [asdict(r) for r in results],
        "buy_window": event_study_result.buy_window,
        "sell_window": event_study_result.sell_window,
        "anchor": event_study_result.anchor,
        "occurrences_used": event_study_result.occurrences_used,
    }

    return Recommendation(
        item_id=item_id,
        event_type=event_type,
        event_instance_id=event_instance_id,
        generated_ts=int(time.time()),
        action=action,
        buy_confidence=buy_conf,
        sell_confidence=sell_conf,
        expected_appreciation=round(expected_appreciation, 1),
        expected_holding_days=round(expected_holding_days, 1),
        explanation=explanation,
        details_json=json.dumps(details),
    )


def historical_accuracy_for_event_type(conn: sqlite3.Connection, event_type: str,
                                         tolerance_pct: float = 3.0) -> Optional[float]:
    rows = conn.execute(
        "SELECT action, expected_appreciation, outcome_price_at_eval, "
        "       buy_confidence, sell_confidence "
        "FROM recommendations_log "
        "WHERE event_type = ? AND outcome_recorded_ts IS NOT NULL",
        (event_type,)
    ).fetchall()
    if not rows:
        return None

    hits = 0
    total = 0
    for action, expected_appreciation, outcome_price, buy_conf, sell_conf in rows:
        if action == "hold":
            continue
        total += 1
        moved_up = expected_appreciation > 0
        if action == "buy" and moved_up:
            hits += 1
        elif action == "sell" and not moved_up:
            hits += 1
    if total == 0:
        return None
    return round(hits / total * 100.0, 1)