"""
event_study.py -- turns raw price history + a set of historical event
occurrences into a "typical shape" curve indexed by relative day.
"""

import statistics
from dataclasses import dataclass
from typing import Optional

try:
    import numpy as _np # type: ignore
except ImportError:  # pragma: no cover
    _np = None


@dataclass
class RelativeDayStat:
    relative_day: int
    mean: float
    median: float
    stdev: float
    sample_count: int
    pct_change_from_baseline: Optional[float] = None


@dataclass
class EventStudyResult:
    event_type: str
    anchor: str
    window: tuple
    stats_by_day: dict
    expected_curve: dict
    buy_window: tuple
    sell_window: tuple
    buy_window_price: float
    sell_window_price: float
    occurrences_used: int


BASELINE_WINDOW = (-30, -21)
DEFAULT_SEARCH_WINDOW = (-30, 14)
SMOOTHING_HALF_WIDTH = 2


def _price_at_or_before(price_points, target_ts):
    best = None
    for p in price_points:
        if p.ts <= target_ts:
            best = p
        else:
            break
    return best.price if best else None


def build_relative_day_series(price_points, event_start_ts, event_end_ts,
                                anchor="start", window=DEFAULT_SEARCH_WINDOW):
    zero_ts = event_start_ts if anchor == "start" else event_end_ts
    day_seconds = 86400
    series = {}
    lo, hi = window
    for rel_day in range(lo, hi + 1):
        target_ts = zero_ts + rel_day * day_seconds
        price = _price_at_or_before(price_points, target_ts)
        if price is not None:
            series[rel_day] = price
    return series


def choose_best_anchor(all_price_points_by_instance, instances, window=DEFAULT_SEARCH_WINDOW):
    def avg_variance_for_anchor(anchor):
        by_day = {}
        for inst in instances:
            points = all_price_points_by_instance.get(inst.event_instance_id, [])
            series = build_relative_day_series(points, inst.start_ts, inst.end_ts, anchor, window)
            for day, price in series.items():
                by_day.setdefault(day, []).append(price)
        variances = [statistics.pvariance(v) for v in by_day.values() if len(v) >= 2]
        return statistics.mean(variances) if variances else None

    var_start = avg_variance_for_anchor("start")
    var_end = avg_variance_for_anchor("end")
    if var_start is None and var_end is None:
        return "start"
    if var_end is None:
        return "start"
    if var_start is None:
        return "end"
    return "start" if var_start <= var_end else "end"


def compute_relative_day_stats(all_price_points_by_instance, instances, anchor="start",
                                 window=DEFAULT_SEARCH_WINDOW):
    by_day = {}
    for inst in instances:
        points = all_price_points_by_instance.get(inst.event_instance_id, [])
        series = build_relative_day_series(points, inst.start_ts, inst.end_ts, anchor, window)
        for day, price in series.items():
            by_day.setdefault(day, []).append(price)

    baseline_values = []
    for day in range(BASELINE_WINDOW[0], BASELINE_WINDOW[1] + 1):
        baseline_values.extend(by_day.get(day, []))
    baseline_mean = statistics.mean(baseline_values) if baseline_values else None

    stats_by_day = {}
    for day, values in sorted(by_day.items()):
        mean = statistics.mean(values)
        median = statistics.median(values)
        stdev = statistics.pstdev(values) if len(values) >= 2 else 0.0
        pct_change = ((mean - baseline_mean) / baseline_mean * 100.0) if baseline_mean else None
        stats_by_day[day] = RelativeDayStat(
            relative_day=day, mean=mean, median=median, stdev=stdev,
            sample_count=len(values), pct_change_from_baseline=pct_change,
        )
    return stats_by_day


def _moving_average(ordered_days, values, half_width=SMOOTHING_HALF_WIDTH):
    smoothed = {}
    n = len(ordered_days)
    for i, day in enumerate(ordered_days):
        lo = max(0, i - half_width)
        hi = min(n, i + half_width + 1)
        smoothed[day] = statistics.mean(values[lo:hi])
    return smoothed


def _quadratic_regression_curve(ordered_days, values):
    if _np is None or len(ordered_days) < 4:
        return None
    x = _np.array(ordered_days, dtype=float)
    y = _np.array(values, dtype=float)
    coeffs = _np.polyfit(x, y, deg=2)
    poly = _np.poly1d(coeffs)
    return {day: float(poly(day)) for day in ordered_days}


def fit_expected_curve(stats_by_day):
    ordered_days = sorted(stats_by_day.keys())
    if not ordered_days:
        return {}
    values = [stats_by_day[d].mean for d in ordered_days]

    fitted = _quadratic_regression_curve(ordered_days, values)
    if fitted is not None:
        return fitted
    return _moving_average(ordered_days, values)


def find_buy_sell_windows(expected_curve, search_window=DEFAULT_SEARCH_WINDOW, window_width=5):
    days = sorted(d for d in expected_curve if search_window[0] <= d <= search_window[1])
    if not days:
        return (0, 0, 0.0), (0, 0, 0.0)

    def window_avg(center_idx):
        lo = max(0, center_idx - window_width // 2)
        hi = min(len(days), center_idx + window_width // 2 + 1)
        window_days = days[lo:hi]
        avg = statistics.mean(expected_curve[d] for d in window_days)
        return window_days[0], window_days[-1], avg

    best_buy_idx = min(range(len(days)), key=lambda i: window_avg(i)[2])
    buy_window = window_avg(best_buy_idx)

    candidates = [i for i, d in enumerate(days) if d > buy_window[1]]
    if not candidates:
        # The historical low sits so late in the search window that
        # there's no day left afterward to look for a high -- falling
        # back to searching the WHOLE window (the old behavior) could
        # pick a "sell" day that's chronologically before "buy", which
        # produces a negative expected-holding-days figure downstream
        # (scoring.days_to_peak_estimate). Falling back to the last
        # available day instead keeps sell >= buy chronologically no
        # matter what.
        candidates = [len(days) - 1]
    best_sell_idx = max(candidates, key=lambda i: window_avg(i)[2])
    sell_window = window_avg(best_sell_idx)

    return buy_window, sell_window


def run_event_study(all_price_points_by_instance, instances, window=DEFAULT_SEARCH_WINDOW,
                     anchor=None) -> Optional[EventStudyResult]:
    if not instances:
        return None
    if anchor is None:
        anchor = choose_best_anchor(all_price_points_by_instance, instances, window)

    stats_by_day = compute_relative_day_stats(all_price_points_by_instance, instances, anchor, window)
    if not stats_by_day:
        return None

    expected_curve = fit_expected_curve(stats_by_day)
    buy_window, sell_window = find_buy_sell_windows(expected_curve, window)

    event_type = instances[0].event_type
    return EventStudyResult(
        event_type=event_type,
        anchor=anchor,
        window=window,
        stats_by_day=stats_by_day,
        expected_curve=expected_curve,
        buy_window=(buy_window[0], buy_window[1]),
        sell_window=(sell_window[0], sell_window[1]),
        buy_window_price=buy_window[2],
        sell_window_price=sell_window[2],
        occurrences_used=len(instances),
    )