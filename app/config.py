from __future__ import annotations

import os
from functools import cached_property
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain import FuelType, Location, Vehicle


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _env_float(name: str, *, required: bool = True) -> float | None:
    raw = os.getenv(name)
    if raw in (None, ""):
        if required:
            raise ValueError(f"La variable {name} est obligatoire")
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"La variable {name} doit etre un nombre") from exc


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "GasWatch"
    tz: str = "America/Toronto"
    log_level: str = "INFO"
    database_path: Path = Path("/app/data/gaswatch.db")
    data_provider: str = "gasquebec"
    gasquebec_base_url: str = "https://www.gasquebec.ca"
    provider_timeout_seconds: float = Field(default=15, gt=0, le=60)

    home_latitude: float | None = None
    home_longitude: float | None = None
    search_radius_km: float = Field(default=15, gt=0, le=200)
    locations: str = ""
    vehicles: str = ""
    fuel_type: FuelType = FuelType.REGULAR

    preferred_stations: str = ""
    preferred_only: bool = False
    preferred_price_tolerance_cents: float = Field(default=2, ge=0)
    favorite_station_ids: str = ""

    price_check_interval_minutes: int = Field(default=60, ge=10)
    max_price_age_minutes: int = Field(default=180, gt=0)
    history_days: int = Field(default=30, ge=7, le=365)
    target_price_percentile: float = Field(default=25, ge=0, le=100)
    target_price_mode: str = "AUTO"
    manual_target_price_cents: float | None = Field(default=None, gt=0)
    minimum_history_days: int = Field(default=3, ge=1)
    good_price_threshold_cents: float = Field(default=5, ge=0)
    high_price_threshold_cents: float = Field(default=5, ge=0)
    very_high_price_threshold_cents: float = Field(default=10, ge=0)

    daily_report_enabled: bool = True
    daily_report_time: str = "07:00"
    price_alerts_enabled: bool = True
    alert_cooldown_hours: int = Field(default=12, ge=1)
    alert_min_price_drop_cents: float = Field(default=3, ge=0)

    ntfy_enabled: bool = False
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""

    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = Field(default=8080, ge=1, le=65535)
    runtime_env_path: Path = Path("/app/data/.env")

    @field_validator("target_price_mode")
    @classmethod
    def valid_target_mode(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"AUTO", "MANUAL"}:
            raise ValueError("TARGET_PRICE_MODE doit etre AUTO ou MANUAL")
        return normalized

    @field_validator("daily_report_time")
    @classmethod
    def valid_time(cls, value: str) -> str:
        pieces = value.split(":")
        if len(pieces) != 2 or not all(piece.isdigit() for piece in pieces):
            raise ValueError("DAILY_REPORT_TIME doit utiliser le format HH:MM")
        hour, minute = map(int, pieces)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("DAILY_REPORT_TIME est invalide")
        return value

    @model_validator(mode="after")
    def validate_combinations(self) -> Settings:
        ZoneInfo(self.tz)
        if self.data_provider != "gasquebec":
            raise ValueError("DATA_PROVIDER pris en charge: gasquebec")
        if self.target_price_mode == "MANUAL" and self.manual_target_price_cents is None:
            raise ValueError("MANUAL_TARGET_PRICE_CENTS est requis en mode MANUAL")
        if self.ntfy_enabled and not self.ntfy_topic:
            raise ValueError("NTFY_TOPIC est requis lorsque NTFY_ENABLED=true")
        if not self.locations and (self.home_latitude is None or self.home_longitude is None):
            raise ValueError("HOME_LATITUDE et HOME_LONGITUDE sont requis sans LOCATIONS")
        return self

    @cached_property
    def configured_locations(self) -> tuple[Location, ...]:
        keys = _csv(self.locations)
        if not keys:
            return (
                Location(
                    key="HOME",
                    name="Maison",
                    latitude=float(self.home_latitude),
                    longitude=float(self.home_longitude),
                    radius_km=self.search_radius_km,
                ),
            )
        result: list[Location] = []
        for raw_key in keys:
            key = raw_key.upper()
            prefix = f"LOCATION_{key}_"
            result.append(
                Location(
                    key=key,
                    name=os.getenv(prefix + "NAME", key.title()),
                    latitude=float(_env_float(prefix + "LATITUDE")),
                    longitude=float(_env_float(prefix + "LONGITUDE")),
                    radius_km=float(_env_float(prefix + "RADIUS_KM", required=False) or 15),
                )
            )
        return tuple(result)

    @cached_property
    def configured_vehicles(self) -> tuple[Vehicle, ...]:
        keys = _csv(self.vehicles)
        if not keys:
            return (
                Vehicle(
                    key="DEFAULT",
                    name="Vehicule",
                    fuel_type=self.fuel_type,
                    consumption_l_per_100km=10,
                    tank_capacity_l=60,
                    average_fill_l=40,
                ),
            )
        result: list[Vehicle] = []
        for raw_key in keys:
            key = raw_key.upper()
            prefix = f"VEHICLE_{key}_"
            vehicle = Vehicle(
                key=key,
                name=os.getenv(prefix + "NAME", key.title()),
                fuel_type=FuelType(os.getenv(prefix + "FUEL", self.fuel_type.value).upper()),
                consumption_l_per_100km=float(_env_float(prefix + "CONSUMPTION")),
                tank_capacity_l=float(_env_float(prefix + "TANK_CAPACITY")),
                average_fill_l=float(_env_float(prefix + "AVERAGE_FILL_LITERS")),
                tank_level_percent=_env_float(prefix + "TANK_LEVEL_PERCENT", required=False),
                daily_distance_km=_env_float(prefix + "DAILY_DISTANCE_KM", required=False),
                minimum_reserve_percent=float(
                    _env_float(prefix + "MINIMUM_RESERVE_PERCENT", required=False) or 20
                ),
            )
            if (
                min(
                    vehicle.consumption_l_per_100km,
                    vehicle.tank_capacity_l,
                    vehicle.average_fill_l,
                )
                <= 0
            ):
                raise ValueError(f"Les valeurs du vehicule {key} doivent etre positives")
            if vehicle.average_fill_l > vehicle.tank_capacity_l:
                raise ValueError(f"Le plein moyen du vehicule {key} depasse le reservoir")
            if (
                vehicle.tank_level_percent is not None
                and not 0 <= vehicle.tank_level_percent <= 100
            ):
                raise ValueError(f"Le niveau du reservoir de {key} doit etre entre 0 et 100")
            result.append(vehicle)
        return tuple(result)

    @property
    def preferred_brands(self) -> frozenset[str]:
        return frozenset(item.casefold() for item in _csv(self.preferred_stations))

    @property
    def favorite_ids(self) -> frozenset[str]:
        return frozenset(_csv(self.favorite_station_ids))
