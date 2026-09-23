from __future__ import annotations

import math
import statistics

from app.domain import PriceStats, StationPrice


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percent / 100
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def analyze(prices: list[StationPrice], history: list[float], target: float | None) -> PriceStats:
    if not prices:
        raise ValueError("Aucun prix a analyser")
    values = [price.price_cents for price in prices]
    return PriceStats(
        minimum=min(values),
        average=statistics.fmean(values),
        median=statistics.median(values),
        maximum=max(values),
        station_count=len(values),
        target=target,
        historical_average=statistics.fmean(history) if history else None,
    )
