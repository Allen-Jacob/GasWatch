from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.domain import FuelType, StationPrice

SCHEMA_VERSION = 1


class Repository:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stations (
                    provider TEXT NOT NULL,
                    station_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    address TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (provider, station_id)
                );
                CREATE TABLE IF NOT EXISTS price_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    station_id TEXT NOT NULL,
                    location_key TEXT NOT NULL,
                    fuel_type TEXT NOT NULL,
                    price_cents REAL NOT NULL CHECK(price_cents > 0),
                    distance_km REAL NOT NULL,
                    published_at TEXT,
                    fetched_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    UNIQUE(provider, station_id, location_key, fuel_type, price_cents, fetched_at)
                );
                CREATE INDEX IF NOT EXISTS observations_lookup
                    ON price_observations(location_key, fuel_type, fetched_at);
                CREATE TABLE IF NOT EXISTS daily_reports (
                    report_date TEXT NOT NULL,
                    location_key TEXT NOT NULL,
                    fuel_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sent_at TEXT,
                    PRIMARY KEY(report_date, location_key, fuel_type)
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_key TEXT NOT NULL,
                    station_id TEXT,
                    price_cents REAL,
                    sent_at TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                """
            )
            db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
            )

    def save_observations(self, location_key: str, prices: list[StationPrice]) -> int:
        inserted = 0
        with self.connect() as db:
            for price in prices:
                db.execute(
                    """
                    INSERT INTO stations(provider, station_id, name, brand, address, latitude,
                                         longitude, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, station_id) DO UPDATE SET
                        name=excluded.name, brand=excluded.brand, address=excluded.address,
                        latitude=excluded.latitude, longitude=excluded.longitude,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        "gasquebec",
                        price.station_id,
                        price.name,
                        price.brand,
                        price.address,
                        price.latitude,
                        price.longitude,
                        price.fetched_at.isoformat(),
                    ),
                )
                previous = db.execute(
                    """
                    SELECT price_cents, date(fetched_at) AS observed_date
                    FROM price_observations
                    WHERE provider=? AND station_id=? AND location_key=? AND fuel_type=?
                    ORDER BY fetched_at DESC LIMIT 1
                    """,
                    ("gasquebec", price.station_id, location_key, price.fuel_type.value),
                ).fetchone()
                observation_date = price.fetched_at.date().isoformat()
                if (
                    previous is not None
                    and float(previous["price_cents"]) == price.price_cents
                    and previous["observed_date"] == observation_date
                ):
                    continue
                cursor = db.execute(
                    """
                    INSERT OR IGNORE INTO price_observations(
                        provider, station_id, location_key, fuel_type, price_cents, distance_km,
                        published_at, fetched_at, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "gasquebec",
                        price.station_id,
                        location_key,
                        price.fuel_type.value,
                        price.price_cents,
                        price.distance_km,
                        price.published_at.isoformat() if price.published_at else None,
                        price.fetched_at.isoformat(),
                        price.source,
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def daily_minimums(self, location_key: str, fuel_type: FuelType, days: int) -> list[float]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT MIN(price_cents) AS minimum
                FROM price_observations
                WHERE location_key=? AND fuel_type=? AND fetched_at>=?
                GROUP BY date(fetched_at)
                ORDER BY date(fetched_at)
                """,
                (location_key, fuel_type.value, since),
            ).fetchall()
        return [float(row["minimum"]) for row in rows]

    def report_exists(self, report_date: date, location_key: str, fuel_type: FuelType) -> bool:
        with self.connect() as db:
            row = db.execute(
                """SELECT 1 FROM daily_reports
                   WHERE report_date=? AND location_key=? AND fuel_type=?""",
                (report_date.isoformat(), location_key, fuel_type.value),
            ).fetchone()
        return row is not None

    def record_report(
        self, report_date: date, location_key: str, fuel_type: FuelType, content: str, sent: bool
    ) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO daily_reports
                   (report_date, location_key, fuel_type, content, sent_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    report_date.isoformat(),
                    location_key,
                    fuel_type.value,
                    content,
                    datetime.now(UTC).isoformat() if sent else None,
                ),
            )

    def last_alert(self, alert_key: str) -> tuple[datetime, float | None] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT sent_at, price_cents FROM alerts
                   WHERE alert_key=? AND status='sent' ORDER BY sent_at DESC LIMIT 1""",
                (alert_key,),
            ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["sent_at"]), row["price_cents"]

    def record_alert(
        self, alert_key: str, station_id: str, price_cents: float, status: str
    ) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO alerts(alert_key, station_id, price_cents, sent_at, status)
                   VALUES (?, ?, ?, ?, ?)""",
                (alert_key, station_id, price_cents, datetime.now(UTC).isoformat(), status),
            )

    def healthy(self) -> bool:
        try:
            with self.connect() as db:
                return db.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False

    def backup(self, destination: Path | str) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)
