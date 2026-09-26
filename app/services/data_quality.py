from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class ConfidenceIndicator:
    score: int
    label: str
    freshness: int
    regularity: int
    coverage: int
    completeness: int
    consistency: int
    explanation: str


def price_confidence(
    station: dict[str, object],
    market_prices: list[float],
    *,
    collection_count_24h: int,
    expected_collections_24h: int,
    current_station_count: int,
    usual_station_count: int,
    max_age_minutes: int,
    now: datetime | None = None,
) -> ConfidenceIndicator:
    observed = datetime.fromisoformat(str(station["fetched_at"]))
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    age_minutes = max(((now or datetime.now(UTC)) - observed).total_seconds() / 60, 0)
    freshness = round(max(0, 100 * (1 - age_minutes / max(max_age_minutes, 1))))
    regularity = round(min(100, 100 * collection_count_24h / max(expected_collections_24h, 1)))
    coverage = round(min(100, 100 * current_station_count / max(usual_station_count, 1)))
    required = ("name", "address", "price_cents", "distance_km", "fetched_at", "source")
    completeness = round(
        100 * sum(station.get(field) not in (None, "") for field in required) / len(required)
    )
    median = statistics.median(market_prices) if market_prices else float(station["price_cents"])
    deviation = abs(float(station["price_cents"]) - median)
    consistency = round(max(0, 100 - max(0, deviation - 3) * 8))
    score = round(
        freshness * 0.35
        + regularity * 0.25
        + coverage * 0.20
        + completeness * 0.10
        + consistency * 0.10
    )
    label = "Élevée" if score >= 80 else "Moyenne" if score >= 55 else "Faible"
    concerns = []
    if freshness < 60:
        concerns.append("prix ancien")
    if regularity < 70:
        concerns.append("collectes irrégulières")
    if coverage < 70:
        concerns.append("stations manquantes")
    if completeness < 100:
        concerns.append("données incomplètes")
    if consistency < 70:
        concerns.append("prix inhabituel")
    explanation = ", ".join(concerns) if concerns else "données fraîches et cohérentes"
    return ConfidenceIndicator(
        score, label, freshness, regularity, coverage, completeness, consistency, explanation
    )
