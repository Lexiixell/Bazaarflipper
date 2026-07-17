"""
indicators.py -- independent signal functions.
"""

import statistics
from dataclasses import dataclass


@dataclass
class IndicatorContext:
    today_relative_day: int
    today_price: float
    recent_prices: list
    recent_volumes: list
    stats_by_day: dict
    expected_curve: dict
    buy_window: tuple
    sell_window: tuple
    min_liquidity_volume: float = 0.0
    session_coverage: float = 1.0


@dataclass
class IndicatorResult:
    name: str
    score: float
    kind: str
    explanation: str


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def _window_center(window):
    return (window[0] + window[1]) / 2.0


def relative_day_position_score(ctx: IndicatorContext) -> IndicatorResult:
    buy_center = _window_center(ctx.buy_window)
    sell_center = _window_center(ctx.sell_window)
    span = abs(sell_center - buy_center) or 1.0

    dist_to_buy = abs(ctx.today_relative_day - buy_center)
    dist_to_sell = abs(ctx.today_relative_day - sell_center)

    raw = (dist_to_sell - dist_to_buy) / span * -1.0
    score = _clamp(raw)
    side = "buy" if score < 0 else ("sell" if score > 0 else "neutral")
    return IndicatorResult(
        name="relative_day_position",
        score=score,
        kind="directional",
        explanation=(f"today is day {ctx.today_relative_day}; historical buy window centers on "
                     f"day {buy_center:.0f}, sell window on day {sell_center:.0f} -> leans {side}"),
    )


def deviation_score(ctx: IndicatorContext) -> IndicatorResult:
    stat = ctx.stats_by_day.get(ctx.today_relative_day)
    if stat is None or stat.stdev == 0:
        return IndicatorResult("deviation", 0.0, "directional",
                                "no historical stats (or zero variance) for today's relative day")
    z = (ctx.today_price - stat.mean) / stat.stdev
    score = _clamp(z / 2.5)
    return IndicatorResult(
        name="deviation",
        score=score,
        kind="directional",
        explanation=f"current price is {z:+.2f} standard deviations from the historical mean "
                    f"for day {ctx.today_relative_day} ({stat.mean:,.1f} \u00b1 {stat.stdev:,.1f})",
    )


def momentum_score(ctx: IndicatorContext, short_window: int = 3, long_window: int = 10) -> IndicatorResult:
    prices = [p for _, p in ctx.recent_prices]
    if len(prices) < long_window:
        return IndicatorResult("momentum", 0.0, "directional",
                                "not enough recent price history for a stable momentum read")
    short_ma = statistics.mean(prices[-short_window:])
    long_ma = statistics.mean(prices[-long_window:])
    if long_ma == 0:
        return IndicatorResult("momentum", 0.0, "directional", "long-window average is zero")
    pct_diff = (short_ma - long_ma) / long_ma
    score = _clamp(pct_diff * 4)
    direction = "up" if pct_diff > 0 else "down"
    return IndicatorResult(
        name="momentum",
        score=score,
        kind="directional",
        explanation=f"{short_window}-day average is {pct_diff:+.1%} vs {long_window}-day average "
                    f"(momentum trending {direction})",
    )


def volatility_dampener(ctx: IndicatorContext, high_cv_threshold: float = 0.35) -> IndicatorResult:
    stat = ctx.stats_by_day.get(ctx.today_relative_day)
    if stat is None or stat.mean == 0:
        return IndicatorResult("volatility_dampener", 0.6, "dampener",
                                "no historical stats for today's relative day; moderate default dampening")
    cv = stat.stdev / stat.mean
    if cv <= 0:
        multiplier = 1.0
    else:
        multiplier = _clamp(1.0 - (cv / high_cv_threshold), 0.15, 1.0)
    return IndicatorResult(
        name="volatility_dampener",
        score=multiplier,
        kind="dampener",
        explanation=f"historical coefficient of variation at day {ctx.today_relative_day} is {cv:.2f} "
                    f"-> confidence multiplier {multiplier:.2f}",
    )


def liquidity_dampener(ctx: IndicatorContext) -> IndicatorResult:
    if ctx.min_liquidity_volume <= 0:
        return IndicatorResult("liquidity_dampener", 1.0, "dampener", "no liquidity floor configured")
    volumes = [v for _, v in ctx.recent_volumes] or [0.0]
    avg_volume = statistics.mean(volumes)
    ratio = avg_volume / ctx.min_liquidity_volume
    multiplier = _clamp(ratio, 0.1, 1.0)
    return IndicatorResult(
        name="liquidity_dampener",
        score=multiplier,
        kind="dampener",
        explanation=f"average recent volume {avg_volume:,.0f} vs. floor {ctx.min_liquidity_volume:,.0f} "
                    f"-> confidence multiplier {multiplier:.2f}",
    )


def session_coverage_dampener(ctx: IndicatorContext) -> IndicatorResult:
    """Dampens confidence when the app hasn't actually been open (and
    therefore sampling live prices) for much of the recent window that
    momentum/deviation are read from. price_history only grows while the app
    is running and refreshing -- two snapshots three days apart with nothing
    sampled in between isn't a trend, it's two points connected by a guess.
    coverage=1.0 (fully watched) leaves confidence untouched; coverage=0.0
    (never open during the window) floors the multiplier rather than zeroing
    it outright, since a big price move that's real is still worth a
    reduced-confidence flag rather than being silently discarded."""
    coverage = _clamp(ctx.session_coverage, 0.0, 1.0)
    multiplier = _clamp(0.25 + 0.75 * coverage, 0.25, 1.0)
    return IndicatorResult(
        name="session_coverage_dampener",
        score=multiplier,
        kind="dampener",
        explanation=f"app was actively open/sampling {coverage:.0%} of the recent price "
                    f"window -> confidence multiplier {multiplier:.2f}",
    )


def sample_adequacy_dampener(ctx: IndicatorContext, desired_min_samples: int = 3) -> IndicatorResult:
    stat = ctx.stats_by_day.get(ctx.today_relative_day)
    count = stat.sample_count if stat else 0
    multiplier = _clamp(count / desired_min_samples, 0.1, 1.0)
    return IndicatorResult(
        name="sample_adequacy_dampener",
        score=multiplier,
        kind="dampener",
        explanation=f"{count} historical occurrence(s) contributed data for day "
                    f"{ctx.today_relative_day} (want >= {desired_min_samples}) -> multiplier {multiplier:.2f}",
    )


def days_to_peak_estimate(ctx: IndicatorContext) -> float:
    return _window_center(ctx.sell_window) - _window_center(ctx.buy_window)


DEFAULT_INDICATORS = [
    (relative_day_position_score, "directional", 1.0),
    (deviation_score,             "directional", 1.3),
    (momentum_score,              "directional", 0.5),
    (volatility_dampener,         "dampener",    1.0),
    (liquidity_dampener,          "dampener",    1.0),
    (sample_adequacy_dampener,    "dampener",    1.0),
    (session_coverage_dampener,   "dampener",    1.0),
]