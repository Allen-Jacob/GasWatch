from datetime import UTC, datetime

import respx

from app.config import Settings
from app.database import Repository
from app.domain import FuelType, Location, StationPrice
from app.notifications.ntfy import NtfyNotifier
from app.service import GasWatchService


@respx.mock
async def test_ntfy_uses_topic_path_and_bearer_token() -> None:
    route = respx.post("https://ntfy.example.test/gaswatch").respond(200)
    notifier = NtfyNotifier("https://ntfy.example.test", "gaswatch", "secret")
    try:
        await notifier.send("Titre", "Message", "high")
    finally:
        await notifier.close()
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer secret"
    assert request.headers["Title"] == "=?utf-8?q?Titre?="
    assert request.headers["Priority"] == "high"
    assert request.content == b"Message"


@respx.mock
async def test_ntfy_escapes_topic_and_encodes_unicode_title() -> None:
    route = respx.post("https://ntfy.example.test/gaswatch%20priv%C3%A9").respond(200)
    notifier = NtfyNotifier("https://ntfy.example.test", "gaswatch privé")
    try:
        await notifier.send("⛽ GasWatch — Test", "Ça fonctionne")
    finally:
        await notifier.close()

    request = route.calls[0].request
    assert request.headers["Title"].isascii()
    assert request.content == "Ça fonctionne".encode()


def test_smart_alert_mentions_low_tank_detour_and_percentile(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME_LATITUDE", "46.8")
    monkeypatch.setenv("HOME_LONGITUDE", "-71.2")
    monkeypatch.setenv("VEHICLES", "CAR")
    monkeypatch.setenv("VEHICLE_CAR_NAME", "Auto")
    monkeypatch.setenv("VEHICLE_CAR_FUEL", "REGULAR")
    monkeypatch.setenv("VEHICLE_CAR_CONSUMPTION", "10")
    monkeypatch.setenv("VEHICLE_CAR_TANK_CAPACITY", "50")
    monkeypatch.setenv("VEHICLE_CAR_AVERAGE_FILL_LITERS", "40")
    monkeypatch.setenv("VEHICLE_CAR_TANK_LEVEL_PERCENT", "22")
    monkeypatch.setenv("VEHICLE_CAR_DAILY_DISTANCE_KM", "30")
    settings = Settings(_env_file=None, database_path=tmp_path / "gaswatch.db")
    repository = Repository(settings.database_path)
    repository.initialize()
    service = GasWatchService(settings, repository, object(), object())  # type: ignore[arg-type]
    station = StationPrice(
        "one",
        "Costco",
        "Costco",
        "Adresse",
        46.8,
        -71.2,
        5,
        FuelType.REGULAR,
        145,
        datetime.now(UTC),
    )

    message = service._smart_alert_message(
        Location("HOME", "Maison", 46.8, -71.2, 15),
        FuelType.REGULAR,
        station,
        [
            station,
            StationPrice(
                "two",
                "Shell",
                "Shell",
                "Adresse",
                46.8,
                -71.2,
                1,
                FuelType.REGULAR,
                150,
                datetime.now(UTC),
            ),
        ],
        [140, 145, 150, 155, 160],
        148,
    )

    assert "reservoir est probablement bas" in message
    assert "detour ne vaut que" in message
    assert "percentile" in message
