from __future__ import annotations

import unicodedata

from app.domain import StationPrice


def normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def filter_and_rank(
    prices: list[StationPrice],
    preferred_brands: frozenset[str],
    favorite_ids: frozenset[str],
    preferred_only: bool,
    tolerance_cents: float,
) -> list[StationPrice]:
    normalized_preferences = {normalize_name(item) for item in preferred_brands}

    def preferred(station: StationPrice) -> bool:
        haystack = normalize_name(f"{station.brand} {station.name}")
        return station.station_id in favorite_ids or any(
            brand in haystack for brand in normalized_preferences
        )

    candidates = [station for station in prices if preferred(station)] if preferred_only else prices
    if not candidates:
        return []
    best_price = min(station.price_cents for station in candidates)
    return sorted(
        candidates,
        key=lambda station: (
            not (preferred(station) and station.price_cents <= best_price + tolerance_cents),
            station.price_cents,
            station.distance_km,
        ),
    )
