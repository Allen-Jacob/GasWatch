import sqlite3
from datetime import UTC, date, datetime

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


def test_unchanged_price_is_retained_once_per_successful_collection(tmp_path) -> None:
    repository = Repository(tmp_path / "gaswatch.db")
    repository.initialize()
    morning = datetime(2026, 9, 24, 12, tzinfo=UTC)
    evening = datetime(2026, 9, 24, 22, tzinfo=UTC)
    original = StationPrice(
        "id-1",
        "Costco",
        "Costco",
        "Adresse",
        46.8,
        -71.2,
        4.2,
        FuelType.REGULAR,
        145.9,
        morning,
    )
    refreshed = StationPrice(
        "id-1",
        "Costco",
        "Costco",
        "Adresse",
        46.8,
        -71.2,
        4.0,
        FuelType.REGULAR,
        145.9,
        evening,
    )

    assert repository.save_observations("HOME", [original]) == 1
    assert repository.save_observations("HOME", [refreshed]) == 1

    rows = repository.dashboard_snapshot()
    assert len(rows) == 1
    assert rows[0]["fetched_at"] == evening.isoformat()
    assert rows[0]["distance_km"] == 4.0
    assert len(repository.dashboard_station_history(30)) == 1

    with repository.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0] == 2


def test_collection_id_makes_a_retry_idempotent(tmp_path) -> None:
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
    )

    assert repository.save_observations("HOME", [observation], "poll-1") == 1
    assert repository.save_observations("HOME", [observation], "poll-1") == 0


def test_daily_statistics_weight_each_station_equally(tmp_path) -> None:
    repository = Repository(tmp_path / "gaswatch.db")
    repository.initialize()
    observed = datetime.now(UTC)

    def observation(station_id: str, price: float, hour: int) -> StationPrice:
        return StationPrice(
            station_id,
            station_id,
            "Marque",
            "Adresse",
            46.8,
            -71.2,
            2,
            FuelType.REGULAR,
            price,
            observed.replace(hour=hour),
        )

    repository.save_observations("HOME", [observation("frequent", 100, 10)], "poll-1")
    repository.save_observations("HOME", [observation("frequent", 200, 11)], "poll-2")
    repository.save_observations("HOME", [observation("steady", 300, 12)], "poll-3")

    daily = repository.dashboard_history(30)[0]
    assert daily["average"] == 225  # mean(mean(100, 200), mean(300))
    stats = repository.advanced_statistics("HOME", FuelType.REGULAR, 30)
    assert stats["average"] == 225


def test_v2_migration_preserves_observations_and_builds_rollups(tmp_path) -> None:
    path = tmp_path / "gaswatch.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE price_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
                station_id TEXT NOT NULL, location_key TEXT NOT NULL,
                fuel_type TEXT NOT NULL, price_cents REAL NOT NULL,
                distance_km REAL NOT NULL, published_at TEXT, fetched_at TEXT NOT NULL,
                source TEXT NOT NULL,
                UNIQUE(provider, station_id, location_key, fuel_type, price_cents, fetched_at)
            );
            INSERT INTO price_observations(provider, station_id, location_key, fuel_type,
                price_cents, distance_km, fetched_at, source)
            VALUES ('gasquebec', 'old-1', 'HOME', 'REGULAR', 149.9, 2,
                '2026-09-24T12:00:00+00:00', 'legacy');
            """
        )

    repository = Repository(path)
    repository.initialize()

    with repository.connect() as db:
        row = db.execute("SELECT price_cents, collection_id FROM price_observations").fetchone()
    assert row["price_cents"] == 149.9
    assert row["collection_id"].startswith("legacy-")
    assert repository.dashboard_history(30)[0]["average"] == 149.9


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
            "EXCLUDED_STATION_IDS": "station-2",
        },
        env_path,
    )
    assert repository.runtime_settings()["FAVORITE_STATION_IDS"] == "station-1"
    assert repository.runtime_settings()["EXCLUDED_STATION_IDS"] == "station-2"
    assert "HOME_LATITUDE=46.8" in env_path.read_text()


def test_fillup_journal_calculates_savings_and_real_consumption(tmp_path) -> None:
    repository = Repository(tmp_path / "gaswatch.db")
    repository.initialize()
    repository.add_fillup(
        filled_at=date(2026, 9, 1),
        vehicle_key="CAR",
        vehicle_name="Auto",
        station_name="Costco",
        price_cents=150,
        liters=40,
        odometer_km=10_000,
        local_average_cents=160,
    )
    repository.add_fillup(
        filled_at=date(2026, 9, 15),
        vehicle_key="CAR",
        vehicle_name="Auto",
        station_name="Costco",
        price_cents=155,
        liters=50,
        odometer_km=10_500,
        local_average_cents=160,
    )

    stats = repository.fillup_statistics()
    assert stats["liters"] == 90
    assert stats["spending_cad"] == 137.5
    assert stats["savings_cad"] == 6.5
    assert stats["consumption_l_per_100km"] == 10


def test_fillups_can_be_filtered_updated_deleted_and_store_receipt(tmp_path) -> None:
    repository = Repository(tmp_path / "gaswatch.db")
    repository.initialize()
    fillup_id = repository.add_fillup(
        filled_at=date(2026, 9, 20),
        vehicle_key="CAR",
        vehicle_name="Auto",
        station_name="Costco",
        price_cents=150,
        liters=40,
        note="Premier plein",
        is_full_tank=False,
        receipt_name="recu.pdf",
        receipt_type="application/pdf",
        receipt_data=b"%PDF-test",
    )
    repository.add_fillup(
        filled_at=date(2026, 9, 21),
        vehicle_key="OTHER",
        vehicle_name="Autre",
        station_name="Shell",
        price_cents=160,
        liters=20,
    )

    assert len(repository.fillups(vehicle_key="CAR")) == 1
    assert repository.fillup_receipt(fillup_id) == (
        "recu.pdf",
        "application/pdf",
        b"%PDF-test",
    )
    assert repository.update_fillup(
        fillup_id,
        filled_at=date(2026, 9, 22),
        vehicle_key="CAR",
        vehicle_name="Auto",
        station_name="Costco Québec",
        price_cents=151,
        liters=41,
        odometer_km=12345,
        note="Corrigé",
        is_full_tank=True,
    )
    updated = repository.fillups(vehicle_key="CAR")[0]
    assert updated["station_name"] == "Costco Québec"
    assert updated["note"] == "Corrigé"
    assert updated["is_full_tank"] == 1
    assert repository.delete_fillup(fillup_id)
    assert repository.fillup_receipt(fillup_id) is None
