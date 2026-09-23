from datetime import UTC, datetime

from app.config import Settings
from app.database import Repository
from app.domain import FuelType, StationPrice
from app.web import render_dashboard


def test_dashboard_renders_saved_stations(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME_LATITUDE", "46.8")
    monkeypatch.setenv("HOME_LONGITUDE", "-71.2")
    settings = Settings(_env_file=None, database_path=tmp_path / "gaswatch.db")
    repository = Repository(settings.database_path)
    repository.initialize()
    repository.save_observations(
        "HOME",
        [
            StationPrice(
                "id-1",
                "Costco Quebec",
                "Costco",
                "440 rue Bouvier",
                46.8,
                -71.2,
                5.5,
                FuelType.REGULAR,
                159.9,
                datetime.now(UTC),
                source="Attribution officielle",
            )
        ],
    )
    page = render_dashboard(repository, settings)
    assert "Costco Quebec" in page
    assert "159.9" in page
    assert "stations conservees" in page
    assert "Mes reglages" in page
    assert "Station favorite" in page
    assert "Variation" in page
    assert "Moyenne des stations suivies" in page
    assert "Historique de cette station" in page
    assert "Prix sur les 30 derniers jours" in page
    assert "data-station" in page
    assert "--accent:#d7c7ad" in page


def test_dashboard_has_empty_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME_LATITUDE", "46.8")
    monkeypatch.setenv("HOME_LONGITUDE", "-71.2")
    settings = Settings(_env_file=None, database_path=tmp_path / "gaswatch.db")
    repository = Repository(settings.database_path)
    repository.initialize()
    assert "Premiere collecte en cours" in render_dashboard(repository, settings)
