import pytest
from pydantic import ValidationError

from app.config import Settings
from app.domain import FuelType


def test_simple_location(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME_LATITUDE", "46.8")
    monkeypatch.setenv("HOME_LONGITUDE", "-71.2")
    settings = Settings(_env_file=None)
    assert settings.configured_locations[0].name == "Maison"


def test_multiple_vehicles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME_LATITUDE", "46.8")
    monkeypatch.setenv("HOME_LONGITUDE", "-71.2")
    monkeypatch.setenv("VEHICLES", "ONE,TWO")
    for key, fuel in (("ONE", "REGULAR"), ("TWO", "DIESEL")):
        monkeypatch.setenv(f"VEHICLE_{key}_FUEL", fuel)
        monkeypatch.setenv(f"VEHICLE_{key}_CONSUMPTION", "9")
        monkeypatch.setenv(f"VEHICLE_{key}_TANK_CAPACITY", "60")
        monkeypatch.setenv(f"VEHICLE_{key}_AVERAGE_FILL_LITERS", "40")
    vehicles = Settings(_env_file=None).configured_vehicles
    assert [vehicle.fuel_type for vehicle in vehicles] == [FuelType.REGULAR, FuelType.DIESEL]


def test_manual_target_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME_LATITUDE", "46.8")
    monkeypatch.setenv("HOME_LONGITUDE", "-71.2")
    monkeypatch.setenv("TARGET_PRICE_MODE", "MANUAL")
    with pytest.raises(ValidationError, match="MANUAL_TARGET_PRICE_CENTS"):
        Settings(_env_file=None)
