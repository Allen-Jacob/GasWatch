from __future__ import annotations

import unicodedata

from app.domain import StationPrice
from app.services.trip_cost import evaluate_trip


def normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def filter_and_rank(
    prices: list[StationPrice],
    preferred_brands: frozenset[str],
    favorite_ids: frozenset[str],
    preferred_only: bool,
    tolerance_cents: float,
    *,
    reference_cents: float | None = None,
    liters: float | None = None,
    consumption_l_per_100km: float | None = None,
    max_detour_km: float = 8,
    min_net_savings: float = 2,
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
    use_net = all(value is not None for value in (reference_cents, liters, consumption_l_per_100km))

    def rank(station: StationPrice) -> tuple[object, ...]:
        preferred_rank = not (
            preferred(station) and station.price_cents <= best_price + tolerance_cents
        )
        if use_net:
            economics = evaluate_trip(
                float(reference_cents),
                station.price_cents,
                float(liters),
                station.distance_km,
                float(consumption_l_per_100km),
                max_detour_km,
                min_net_savings,
            )
            eligible = economics.detour_km <= max_detour_km
            return (preferred_rank, not eligible, -economics.net_savings_cad, station.price_cents)
        return (preferred_rank, station.price_cents, station.distance_km)

    return sorted(
        candidates,
        key=rank,
    )
