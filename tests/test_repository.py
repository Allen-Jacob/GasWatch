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
    backup = tmp_path / "backup.db"
    repository.backup(backup)
    assert backup.exists()
    assert Repository(backup).healthy()
