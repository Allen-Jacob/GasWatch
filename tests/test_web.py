from datetime import UTC, datetime

from app.config import Settings
from app.database import Repository
from app.domain import FuelType, StationPrice
from app.web import _updated_station_preferences, render_dashboard


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
    assert "stations disponibles" in page
    assert "Mes reglages" in page
    assert "Ajouter aux favoris" in page
    assert "Ne plus afficher cette station" in page
    assert 'name="favorite_station_id"' not in page
    assert "Variation" in page
    assert "Moyenne des stations suivies" in page
    assert "Historique de cette station" in page
    assert "Prix sur les 30 derniers jours" in page
    assert "data-station" in page
    assert "Verdict du jour" in page
    assert "Analyse en cours" in page
    assert "chart-point" in page
    assert "data-chart" in page
    assert 'data-tooltip="2026-' in page
    assert "brand-costco" in page
    assert "Tendance à venir" in page
    assert "price-direction" in page
    assert "--green:#71d99b" in page


def test_dashboard_has_empty_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME_LATITUDE", "46.8")
    monkeypatch.setenv("HOME_LONGITUDE", "-71.2")
    settings = Settings(_env_file=None, database_path=tmp_path / "gaswatch.db")
    repository = Repository(settings.database_path)
    repository.initialize()
    assert "Premiere collecte en cours" in render_dashboard(repository, settings)


def test_dashboard_does_not_cap_visible_stations(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME_LATITUDE", "46.8")
    monkeypatch.setenv("HOME_LONGITUDE", "-71.2")
    settings = Settings(_env_file=None, database_path=tmp_path / "gaswatch.db")
    repository = Repository(settings.database_path)
    repository.initialize()
    repository.save_observations(
        "HOME",
        [
            StationPrice(
                f"id-{index}",
                f"Station {index}",
                "Marque",
                f"Adresse {index}",
                46.8,
                -71.2,
                float(index),
                FuelType.REGULAR,
                150 + index,
                datetime.now(UTC),
            )
            for index in range(15)
        ],
    )

    page = render_dashboard(repository, settings)

    assert "15 stations disponibles" in page
    assert "Station 14" in page
    assert "Voir les 7 autres stations" in page


def test_station_preferences_can_favorite_exclude_and_restore() -> None:
    runtime: dict[str, str] = {}

    runtime.update(_updated_station_preferences(runtime, "station-1", "favorite"))
    assert runtime["FAVORITE_STATION_IDS"] == "station-1"
    assert runtime["EXCLUDED_STATION_IDS"] == ""

    runtime.update(_updated_station_preferences(runtime, "station-1", "exclude"))
    assert runtime["FAVORITE_STATION_IDS"] == ""
    assert runtime["EXCLUDED_STATION_IDS"] == "station-1"

    runtime.update(_updated_station_preferences(runtime, "station-1", "include"))
    assert runtime["EXCLUDED_STATION_IDS"] == ""
