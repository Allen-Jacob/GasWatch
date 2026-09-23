from datetime import UTC, datetime

from app.database import Repository
from app.domain import FuelType, StationPrice


def test_repository_persists_observation_and_backup(tmp_path) -> None:
    repository = Repository(tmp_path / "gaswatch.db")
    repository.initialize()
    observation = StationPrice(
        "id-1",
        "Station",
        "Marque",
        "Adresse",
        46.8,
        -71.2,
        2,
        FuelType.REGULAR,
        155.9,
        datetime.now(UTC),
        source="Attribution",
    )
    assert repository.save_observations("HOME", [observation]) == 1
    assert repository.save_observations("HOME", [observation]) == 0
    assert repository.daily_minimums("HOME", FuelType.REGULAR, 30) == [155.9]
    assert repository.dashboard_snapshot()[0]["name"] == "Station"
    assert repository.dashboard_history(30)[0]["station_count"] == 1
    assert repository.dashboard_station_history(30)[0]["price_cents"] == 155.9
    backup = tmp_path / "backup.db"
    repository.backup(backup)
    assert backup.exists()
    assert Repository(backup).healthy()


def test_runtime_settings_are_persisted_and_exported(tmp_path) -> None:
    repository = Repository(tmp_path / "gaswatch.db")
    repository.initialize()
    env_path = tmp_path / ".env"
    repository.save_runtime_settings(
        {
            "HOME_LATITUDE": "46.8",
            "HOME_LONGITUDE": "-71.2",
            "SEARCH_RADIUS_KM": "12",
            "FAVORITE_STATION_IDS": "station-1",
        },
        env_path,
    )
    assert repository.runtime_settings()["FAVORITE_STATION_IDS"] == "station-1"
    assert "HOME_LATITUDE=46.8" in env_path.read_text()
