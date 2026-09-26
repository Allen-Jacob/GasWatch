from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from app.domain import PriceStats, StationPrice


@dataclass(frozen=True, slots=True)
class Forecast:
    horizon_hours: int
    price_cents: float
    confidence: str
    reliable: bool


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


def predict_price_direction(values: list[float]) -> tuple[str, float, str]:
    """Estimate the next short-term direction from the user's latest daily prices.

    This deliberately remains a small, explainable trend estimate rather than a
    market forecast.  Only the seven most recent locally stored daily values are
    used.  The returned slope is expressed in cents per litre per day.
    """
    recent = values[-7:]
    if len(recent) < 2:
        return "unknown", 0.0, "insuffisante"

    x_average = (len(recent) - 1) / 2
    y_average = statistics.fmean(recent)
    denominator = sum((index - x_average) ** 2 for index in range(len(recent)))
    slope = (
        sum((index - x_average) * (value - y_average) for index, value in enumerate(recent))
        / denominator
    )

    # Tiny movements are generally noise at the scale of displayed pump prices.
    direction = "stable" if abs(slope) < 0.15 else "up" if slope > 0 else "down"
    if len(recent) < 4:
        confidence = "faible"
    else:
        predicted = [y_average + slope * (index - x_average) for index in range(len(recent))]
        total_variation = sum((value - y_average) ** 2 for value in recent)
        residual_variation = sum(
            (value - estimate) ** 2 for value, estimate in zip(recent, predicted, strict=True)
        )
        fit = 1 - residual_variation / total_variation if total_variation else 1.0
        confidence = "fort" if fit >= 0.75 else "modéré" if fit >= 0.4 else "faible"
    return direction, slope, confidence


def forecast_prices(values: list[float]) -> list[Forecast]:
    """Return conservative 24/48-hour forecasts only when the fit is usable."""
    recent = values[-14:]
    if len(recent) < 7:
        return []
    direction, slope, confidence = predict_price_direction(recent)
    if direction == "unknown" or confidence == "faible":
        return []
    # Limit extrapolation to a plausible short-term pump-price movement.
    daily_slope = max(min(slope, 5.0), -5.0)
    return [
        Forecast(hours, recent[-1] + daily_slope * hours / 24, confidence, True)
        for hours in (24, 48)
    ]


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
