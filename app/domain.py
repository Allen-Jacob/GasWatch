from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class FuelType(StrEnum):
    REGULAR = "REGULAR"
    PREMIUM = "PREMIUM"
    DIESEL = "DIESEL"

    @property
    def provider_value(self) -> str:
        return {
            self.REGULAR: "ordinaire",
            self.PREMIUM: "super",
            self.DIESEL: "diesel",
        }[self]


class RecommendationCode(StrEnum):
    FILL_NOW = "FILL_NOW"
    GOOD_PRICE = "GOOD_PRICE"
    NORMAL_PRICE = "NORMAL_PRICE"
    WAIT = "WAIT"
    HIGH_PRICE = "HIGH_PRICE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True, slots=True)
class Location:
    key: str
    name: str
    latitude: float
    longitude: float
    radius_km: float


@dataclass(frozen=True, slots=True)
class Vehicle:
    key: str
    name: str
    fuel_type: FuelType
    consumption_l_per_100km: float
    tank_capacity_l: float
    average_fill_l: float
    tank_level_percent: float | None = None
    daily_distance_km: float | None = None
    minimum_reserve_percent: float = 20.0

    def usable_range_km(self) -> float | None:
        if self.tank_level_percent is None:
            return None
        usable_percent = max(self.tank_level_percent - self.minimum_reserve_percent, 0)
        usable_liters = self.tank_capacity_l * usable_percent / 100
        return usable_liters / self.consumption_l_per_100km * 100


@dataclass(frozen=True, slots=True)
class StationPrice:
    station_id: str
    name: str
    brand: str
    address: str
    latitude: float
    longitude: float
    distance_km: float
    fuel_type: FuelType
    price_cents: float
    fetched_at: datetime
    published_at: datetime | None = None
    source: str = "Gas Quebec"


@dataclass(frozen=True, slots=True)
class PriceStats:
    minimum: float
    average: float
    median: float
    maximum: float
    station_count: int
    target: float | None = None
    historical_average: float | None = None


@dataclass(frozen=True, slots=True)
class Recommendation:
    code: RecommendationCode
    reason: str
