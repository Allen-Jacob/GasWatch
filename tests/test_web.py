from datetime import UTC, datetime

from app.config import Settings
from app.database import Repository
from app.domain import FuelType, StationPrice
from app.web import (
    STATIC_DIR,
    _decode_form,
    _updated_station_preferences,
    render_dashboard,
    render_fillups,
    render_statistics,
)


def test_multipart_form_decodes_receipt() -> None:
    boundary = "gaswatch-boundary"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="note"\r\n\r\nEssence\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="receipt"; filename="recu.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n%PDF-test\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    form, files = _decode_form(body, f"multipart/form-data; boundary={boundary}")

    assert form["note"] == ["Essence"]
    assert files["receipt"] == ("recu.pdf", "application/pdf", b"%PDF-test")


def test_home_screen_icons_are_packaged() -> None:
    assert (STATIC_DIR / "app-icon.svg").read_text().startswith("<svg")
    assert (STATIC_DIR / "apple-touch-icon.png").read_bytes().startswith(b"\x89PNG")


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
    assert "Mes réglages" in page
    assert 'class="settings-menu"' in page
    assert "Ajouter aux favoris" in page
    assert "Ne plus afficher cette station" in page
    assert 'name="favorite_station_id"' not in page
    assert "Variation" in page
    assert "Moyenne des stations suivies" in page
    assert "Historique de cette station" in page
    assert "Prix sur les 30 derniers jours" in page
    assert "data-station" in page
    assert "Verdict ·" in page
    assert "Analyse en cours" in page
    assert '<details class="buy-advice learning"' in page
    assert "chart-point" in page
    assert "data-chart" in page
    assert 'data-tooltip="2026-' in page
    assert "brand-costco" in page
    assert "Tendance à venir" in page
    assert "price-direction" in page
    assert "https://maps.apple.com/?q=Costco+Quebec%2C+440+rue+Bouvier" in page
    assert 'rel="apple-touch-icon" sizes="180x180"' in page
    assert "--green:#71d99b" in page
    assert "Médiane actuelle" in page
    assert "Économie brute / plein" in page
    assert "Coût estimé du détour" in page
    assert "Économie nette" in page
    assert "Confiance" in page
    assert "Fraîcheur" in page
    statistics_page = render_statistics(repository, settings)
    assert "Minimum, moyenne et maximum" in statistics_page
    assert "24 h" in statistics_page
    assert "data-toggle-series" in statistics_page
    assert "Distribution des prix" in statistics_page
    assert "Calendrier des prix minimums" in statistics_page
    assert "Classement des stations" in statistics_page
    fillups_page = render_fillups(repository, settings)
    assert "Enregistrer un plein" in fillups_page
    assert "Économies cette année" in fillups_page
    assert "Exporter CSV" in fillups_page
    assert "Reçu (JPG, PNG ou PDF" in fillups_page
    assert "Coût mensuel et consommation" in fillups_page
    assert "Réservoir rempli complètement" in fillups_page


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
