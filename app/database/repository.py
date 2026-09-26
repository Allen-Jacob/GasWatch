from __future__ import annotations

import os
import sqlite3
import statistics
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.domain import FuelType, StationPrice

SCHEMA_VERSION = 5


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
                    collection_id TEXT,
                    UNIQUE(provider, station_id, location_key, fuel_type, price_cents, fetched_at)
                );
                CREATE INDEX IF NOT EXISTS observations_lookup
                    ON price_observations(location_key, fuel_type, fetched_at);
                CREATE INDEX IF NOT EXISTS observations_station_lookup
                    ON price_observations(location_key, fuel_type, station_id, fetched_at);
                CREATE TABLE IF NOT EXISTS daily_price_statistics (
                    day TEXT NOT NULL,
                    location_key TEXT NOT NULL,
                    fuel_type TEXT NOT NULL,
                    minimum REAL NOT NULL,
                    average REAL NOT NULL,
                    median REAL NOT NULL,
                    maximum REAL NOT NULL,
                    station_count INTEGER NOT NULL,
                    observation_count INTEGER NOT NULL,
                    PRIMARY KEY(day, location_key, fuel_type)
                );
                CREATE TABLE IF NOT EXISTS price_predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    location_key TEXT NOT NULL,
                    fuel_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    horizon_hours INTEGER NOT NULL CHECK(horizon_hours IN (24, 48)),
                    predicted_price_cents REAL NOT NULL,
                    confidence TEXT NOT NULL,
                    actual_price_cents REAL,
                    evaluated_at TEXT,
                    UNIQUE(location_key, fuel_type, created_at, horizon_hours)
                );
                CREATE INDEX IF NOT EXISTS predictions_lookup
                    ON price_predictions(location_key, fuel_type, created_at);
                CREATE TABLE IF NOT EXISTS fuel_fillups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filled_at TEXT NOT NULL,
                    vehicle_key TEXT NOT NULL,
                    vehicle_name TEXT NOT NULL,
                    station_id TEXT,
                    station_name TEXT NOT NULL,
                    price_cents REAL NOT NULL CHECK(price_cents > 0),
                    liters REAL NOT NULL CHECK(liters > 0),
                    total_cad REAL NOT NULL CHECK(total_cad > 0),
                    odometer_km REAL,
                    local_average_cents REAL,
                    savings_cad REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    is_full_tank INTEGER NOT NULL DEFAULT 1,
                    receipt_name TEXT,
                    receipt_type TEXT,
                    receipt_data BLOB,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS fillups_vehicle_date
                    ON fuel_fillups(vehicle_key, filled_at);
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
                CREATE TABLE IF NOT EXISTS runtime_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(price_observations)").fetchall()
            }
            if "collection_id" not in columns:
                db.execute("ALTER TABLE price_observations ADD COLUMN collection_id TEXT")
            fillup_columns = {
                str(row["name"]) for row in db.execute("PRAGMA table_info(fuel_fillups)").fetchall()
            }
            for name, declaration in (
                ("note", "TEXT NOT NULL DEFAULT ''"),
                ("is_full_tank", "INTEGER NOT NULL DEFAULT 1"),
                ("receipt_name", "TEXT"),
                ("receipt_type", "TEXT"),
                ("receipt_data", "BLOB"),
            ):
                if name not in fillup_columns:
                    db.execute(f"ALTER TABLE fuel_fillups ADD COLUMN {name} {declaration}")
            db.execute(
                "UPDATE price_observations SET collection_id='legacy-' || id "
                "WHERE collection_id IS NULL"
            )
            db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS observations_collection_unique
                   ON price_observations(provider, station_id, location_key, fuel_type,
                                         collection_id)"""
            )
            groups = db.execute(
                "SELECT DISTINCT location_key, fuel_type FROM price_observations"
            ).fetchall()
            for group in groups:
                self._refresh_daily_statistics(
                    db, str(group["location_key"]), str(group["fuel_type"])
                )
            db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
            )

    def add_fillup(
        self,
        *,
        filled_at: date,
        vehicle_key: str,
        vehicle_name: str,
        station_name: str,
        price_cents: float,
        liters: float,
        odometer_km: float | None = None,
        station_id: str | None = None,
        local_average_cents: float | None = None,
        note: str = "",
        is_full_tank: bool = True,
        receipt_name: str | None = None,
        receipt_type: str | None = None,
        receipt_data: bytes | None = None,
    ) -> int:
        total = price_cents * liters / 100
        savings = (
            max(local_average_cents - price_cents, 0) * liters / 100
            if local_average_cents is not None
            else 0.0
        )
        with self.connect() as db:
            cursor = db.execute(
                """INSERT INTO fuel_fillups(
                       filled_at, vehicle_key, vehicle_name, station_id, station_name,
                       price_cents, liters, total_cad, odometer_km,
                       local_average_cents, savings_cad, note, is_full_tank,
                       receipt_name, receipt_type, receipt_data, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    filled_at.isoformat(),
                    vehicle_key,
                    vehicle_name,
                    station_id,
                    station_name,
                    price_cents,
                    liters,
                    total,
                    odometer_km,
                    local_average_cents,
                    savings,
                    note.strip(),
                    int(is_full_tank),
                    receipt_name,
                    receipt_type,
                    receipt_data,
                    datetime.now(UTC).isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def fillups(
        self,
        days: int | None = None,
        *,
        since_date: date | None = None,
        until_date: date | None = None,
        vehicle_key: str | None = None,
    ) -> list[dict[str, Any]]:
        since = (
            since_date.isoformat()
            if since_date is not None
            else (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
            if days is not None
            else "0001-01-01"
        )
        conditions = ["filled_at>=?"]
        parameters: list[object] = [since]
        if until_date is not None:
            conditions.append("filled_at<=?")
            parameters.append(until_date.isoformat())
        if vehicle_key:
            conditions.append("vehicle_key=?")
            parameters.append(vehicle_key)
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT id, filled_at, vehicle_key, vehicle_name, station_id, station_name,
                           price_cents, liters, total_cad, odometer_km, local_average_cents,
                           savings_cad, note, is_full_tank, receipt_name,
                           receipt_data IS NOT NULL AS has_receipt, created_at
                    FROM fuel_fillups WHERE {" AND ".join(conditions)}
                    ORDER BY filled_at DESC, id DESC""",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def fillup_statistics(
        self,
        days: int | None = None,
        *,
        since_date: date | None = None,
        until_date: date | None = None,
        vehicle_key: str | None = None,
    ) -> dict[str, Any]:
        rows = list(
            reversed(
                self.fillups(
                    days,
                    since_date=since_date,
                    until_date=until_date,
                    vehicle_key=vehicle_key,
                )
            )
        )
        if not rows:
            return {
                "fillup_count": 0,
                "liters": 0.0,
                "spending_cad": 0.0,
                "average_price_cents": None,
                "consumption_l_per_100km": None,
                "cost_per_100km": None,
                "savings_cad": 0.0,
            }
        liters = sum(float(row["liters"]) for row in rows)
        spending = sum(float(row["total_cad"]) for row in rows)
        driven_km = 0.0
        consumed_liters = 0.0
        previous_by_vehicle: dict[str, dict[str, Any]] = {}
        for row in rows:
            previous = previous_by_vehicle.get(str(row["vehicle_key"]))
            if (
                previous is not None
                and previous["odometer_km"] is not None
                and row["odometer_km"] is not None
                and bool(row["is_full_tank"])
                and bool(previous["is_full_tank"])
            ):
                distance = float(row["odometer_km"]) - float(previous["odometer_km"])
                if distance > 0:
                    driven_km += distance
                    consumed_liters += float(row["liters"])
            previous_by_vehicle[str(row["vehicle_key"])] = row
        consumption = consumed_liters / driven_km * 100 if driven_km else None
        return {
            "fillup_count": len(rows),
            "liters": liters,
            "spending_cad": spending,
            "average_price_cents": spending / liters * 100 if liters else None,
            "consumption_l_per_100km": consumption,
            "cost_per_100km": consumption * spending / liters if consumption and liters else None,
            "savings_cad": sum(float(row["savings_cad"]) for row in rows),
        }

    def update_fillup(
        self,
        fillup_id: int,
        *,
        filled_at: date,
        vehicle_key: str,
        vehicle_name: str,
        station_name: str,
        price_cents: float,
        liters: float,
        odometer_km: float | None,
        note: str,
        is_full_tank: bool,
    ) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT local_average_cents FROM fuel_fillups WHERE id=?", (fillup_id,)
            ).fetchone()
            if row is None:
                return False
            average = float(row["local_average_cents"]) if row["local_average_cents"] else None
            savings = max(average - price_cents, 0) * liters / 100 if average else 0
            cursor = db.execute(
                """UPDATE fuel_fillups SET filled_at=?, vehicle_key=?, vehicle_name=?,
                       station_name=?, price_cents=?, liters=?, total_cad=?, odometer_km=?,
                       savings_cad=?, note=?, is_full_tank=? WHERE id=?""",
                (
                    filled_at.isoformat(),
                    vehicle_key,
                    vehicle_name,
                    station_name,
                    price_cents,
                    liters,
                    price_cents * liters / 100,
                    odometer_km,
                    savings,
                    note.strip(),
                    int(is_full_tank),
                    fillup_id,
                ),
            )
            return cursor.rowcount == 1

    def delete_fillup(self, fillup_id: int) -> bool:
        with self.connect() as db:
            return db.execute("DELETE FROM fuel_fillups WHERE id=?", (fillup_id,)).rowcount == 1

    def fillup_receipt(self, fillup_id: int) -> tuple[str, str, bytes] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT receipt_name, receipt_type, receipt_data FROM fuel_fillups WHERE id=?",
                (fillup_id,),
            ).fetchone()
        if row is None or row["receipt_data"] is None:
            return None
        return str(row["receipt_name"]), str(row["receipt_type"]), bytes(row["receipt_data"])

    def save_observations(
        self, location_key: str, prices: list[StationPrice], collection_id: str | None = None
    ) -> int:
        """Persist one observation per station/fuel for a successful collection.

        Equal prices are intentionally retained. ``collection_id`` makes retries idempotent;
        callers that omit it get an identifier derived from the provider timestamp, which is
        shared by every station returned in one request.
        """
        if not prices:
            return 0
        collection_id = (
            collection_id
            or f"{location_key}:{prices[0].fuel_type.value}:{prices[0].fetched_at.isoformat()}"
        )
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
                cursor = db.execute(
                    """
                    INSERT OR IGNORE INTO price_observations(
                        provider, station_id, location_key, fuel_type, price_cents, distance_km,
                        published_at, fetched_at, source, collection_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        collection_id,
                    ),
                )
                inserted += cursor.rowcount
            self._refresh_daily_statistics(
                db,
                location_key,
                prices[0].fuel_type.value,
                {price.fetched_at.date().isoformat() for price in prices},
            )
        return inserted

    @staticmethod
    def _refresh_daily_statistics(
        db: sqlite3.Connection,
        location_key: str,
        fuel_type: str,
        selected_days: set[str] | None = None,
    ) -> None:
        """Rebuild daily rollups without weighting stations by change frequency."""
        days = (
            [{"day": day} for day in selected_days]
            if selected_days is not None
            else db.execute(
                """SELECT DISTINCT date(fetched_at) AS day FROM price_observations
                   WHERE location_key=? AND fuel_type=?""",
                (location_key, fuel_type),
            ).fetchall()
        )
        for day_row in days:
            day = str(day_row["day"])
            rows = db.execute(
                """WITH station_daily AS (
                       SELECT station_id, AVG(price_cents) AS price
                       FROM price_observations
                       WHERE location_key=? AND fuel_type=? AND date(fetched_at)=?
                       GROUP BY station_id
                   )
                   SELECT price FROM station_daily ORDER BY price""",
                (location_key, fuel_type, day),
            ).fetchall()
            values = [float(row["price"]) for row in rows]
            if not values:
                continue
            observation_count = int(
                db.execute(
                    """SELECT COUNT(*) FROM price_observations
                       WHERE location_key=? AND fuel_type=? AND date(fetched_at)=?""",
                    (location_key, fuel_type, day),
                ).fetchone()[0]
            )
            db.execute(
                """INSERT INTO daily_price_statistics(
                       day, location_key, fuel_type, minimum, average, median, maximum,
                       station_count, observation_count
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(day, location_key, fuel_type) DO UPDATE SET
                       minimum=excluded.minimum, average=excluded.average,
                       median=excluded.median, maximum=excluded.maximum,
                       station_count=excluded.station_count,
                       observation_count=excluded.observation_count""",
                (
                    day,
                    location_key,
                    fuel_type,
                    min(values),
                    statistics.fmean(values),
                    statistics.median(values),
                    max(values),
                    len(values),
                    observation_count,
                ),
            )

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

    def runtime_settings(self) -> dict[str, str]:
        with self.connect() as db:
            rows = db.execute("SELECT key, value FROM runtime_settings ORDER BY key").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def save_runtime_settings(self, values: dict[str, str], env_path: Path | str) -> None:
        allowed = {
            "HOME_LATITUDE",
            "HOME_LONGITUDE",
            "SEARCH_RADIUS_KM",
            "FAVORITE_STATION_IDS",
            "EXCLUDED_STATION_IDS",
        }
        if set(values) - allowed:
            raise ValueError("Reglage non autorise")
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            for key, value in values.items():
                db.execute(
                    """
                    INSERT INTO runtime_settings(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                                   updated_at=excluded.updated_at
                    """,
                    (key, value, now),
                )
        current = self.runtime_settings()
        destination = Path(env_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            "# Genere par l'interface GasWatch\n"
            + "".join(f"{key}={current[key]}\n" for key in sorted(current)),
            encoding="utf-8",
        )
        os.replace(temporary, destination)

    def dashboard_snapshot(self, max_age_minutes: int | None = None) -> list[dict[str, Any]]:
        """Return the latest stored observation for each station/location/fuel."""
        cutoff = (
            (datetime.now(UTC) - timedelta(minutes=max_age_minutes)).isoformat()
            if max_age_minutes is not None
            else "0001-01-01T00:00:00+00:00"
        )
        with self.connect() as db:
            rows = db.execute(
                """
                WITH ranked AS (
                    SELECT o.*, s.name, s.brand, s.address, s.latitude, s.longitude,
                           ROW_NUMBER() OVER (
                               PARTITION BY o.location_key, o.fuel_type, o.station_id
                               ORDER BY o.fetched_at DESC
                           ) AS position
                    FROM price_observations o
                    JOIN stations s
                      ON s.provider=o.provider AND s.station_id=o.station_id
                )
                SELECT location_key, fuel_type, station_id, name, brand, address,
                       latitude, longitude,
                       price_cents, distance_km, fetched_at, source
                FROM ranked
                WHERE position=1 AND fetched_at>=?
                ORDER BY location_key, fuel_type, price_cents, distance_km
                """,
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def dashboard_history(self, days: int | None = 30) -> list[dict[str, Any]]:
        since = (
            (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
            if days is not None
            else "0001-01-01"
        )
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT location_key, fuel_type, day, minimum, average, median, maximum,
                       station_count, observation_count
                FROM daily_price_statistics
                WHERE day>=?
                ORDER BY day
                """,
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    def dashboard_station_history(self, days: int = 30) -> list[dict[str, Any]]:
        """Return a station-balanced daily average for dashboard charts."""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT o.location_key, o.fuel_type, o.station_id, s.name, s.address,
                       date(o.fetched_at) AS day, AVG(o.price_cents) AS price_cents
                FROM price_observations o
                JOIN stations s ON s.provider=o.provider AND s.station_id=o.station_id
                WHERE o.fetched_at>=?
                GROUP BY o.location_key, o.fuel_type, o.station_id, date(o.fetched_at)
                ORDER BY o.location_key, o.fuel_type, s.name, o.station_id, day
                """,
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    def collection_quality(self, location_key: str, fuel_type: FuelType | str) -> dict[str, int]:
        fuel = fuel_type.value if isinstance(fuel_type, FuelType) else fuel_type
        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        with self.connect() as db:
            row = db.execute(
                """SELECT COUNT(DISTINCT collection_id) AS collections,
                          COUNT(DISTINCT station_id) AS usual_stations
                   FROM price_observations
                   WHERE location_key=? AND fuel_type=? AND fetched_at>=?""",
                (location_key, fuel, since),
            ).fetchone()
        return {
            "collection_count_24h": int(row["collections"] or 0),
            "usual_station_count": int(row["usual_stations"] or 0),
        }

    def station_comparison(self, days: int = 30) -> list[dict[str, Any]]:
        """Return fair station metrics based on scheduled observations, not price changes."""
        since_30 = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        since_7 = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        since_24 = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        with self.connect() as db:
            rows = db.execute(
                """WITH current AS (
                       SELECT o.*,
                              ROW_NUMBER() OVER (
                                  PARTITION BY location_key, fuel_type, station_id
                                  ORDER BY fetched_at DESC, id DESC
                              ) AS position
                       FROM price_observations o
                   ), recent24 AS (
                       SELECT location_key, fuel_type, station_id, price_cents,
                              ROW_NUMBER() OVER (
                                  PARTITION BY location_key, fuel_type, station_id
                                  ORDER BY fetched_at, id
                              ) AS position
                       FROM price_observations
                       WHERE fetched_at>=?
                   ), first24 AS (
                       SELECT location_key, fuel_type, station_id, price_cents
                       FROM recent24 WHERE position=1
                   ), aggregates AS (
                       SELECT location_key, fuel_type, station_id,
                              AVG(CASE WHEN fetched_at>=? THEN price_cents END) AS average_7d,
                              AVG(price_cents) AS average_30d
                       FROM price_observations
                       WHERE fetched_at>=?
                       GROUP BY location_key, fuel_type, station_id
                   )
                   SELECT c.location_key, c.fuel_type, c.station_id, c.price_cents,
                          a.average_7d, a.average_30d,
                          CASE WHEN f.price_cents IS NULL THEN NULL
                               ELSE c.price_cents-f.price_cents END AS change_24h
                   FROM current c JOIN aggregates a USING(location_key, fuel_type, station_id)
                   LEFT JOIN first24 f USING(location_key, fuel_type, station_id)
                   WHERE c.position=1
                   ORDER BY c.location_key, c.fuel_type, c.price_cents""",
                (since_24, since_7, since_30),
            ).fetchall()
        return [dict(row) for row in rows]

    def advanced_statistics(
        self, location_key: str, fuel_type: FuelType | str, days: int | None = 30
    ) -> dict[str, Any]:
        """Calculate time-weighted market statistics for one area and fuel."""
        fuel = fuel_type.value if isinstance(fuel_type, FuelType) else fuel_type
        since = (
            (datetime.now(UTC) - timedelta(days=days)).isoformat()
            if days is not None
            else "0001-01-01T00:00:00+00:00"
        )
        with self.connect() as db:
            rows = db.execute(
                """SELECT station_id, price_cents, fetched_at, collection_id
                   FROM price_observations
                   WHERE location_key=? AND fuel_type=? AND fetched_at>=?
                   ORDER BY fetched_at, id""",
                (location_key, fuel, since),
            ).fetchall()
        if not rows:
            return {}
        values = [float(row["price_cents"]) for row in rows]
        by_station: dict[str, list[sqlite3.Row]] = {}
        by_day_station: dict[str, dict[str, list[float]]] = {}
        for row in rows:
            by_station.setdefault(str(row["station_id"]), []).append(row)
            day = datetime.fromisoformat(str(row["fetched_at"])).date().isoformat()
            by_day_station.setdefault(day, {}).setdefault(str(row["station_id"]), []).append(
                float(row["price_cents"])
            )
        market_series = [
            statistics.fmean(statistics.fmean(prices) for prices in stations.values())
            for stations in by_day_station.values()
        ]
        changes: list[float] = []
        stability_periods: list[float] = []
        change_intervals: list[float] = []
        cheapest_counts = dict.fromkeys(by_station, 0)
        for station_rows in by_station.values():
            run_started = datetime.fromisoformat(str(station_rows[0]["fetched_at"]))
            last_observed = run_started
            last_change: datetime | None = None
            for previous, current in zip(station_rows, station_rows[1:], strict=False):
                change = float(current["price_cents"]) - float(previous["price_cents"])
                observed = datetime.fromisoformat(str(current["fetched_at"]))
                if abs(change) >= 0.0001:
                    changes.append(change)
                    stability_periods.append((observed - run_started).total_seconds() / 3600)
                    if last_change is not None:
                        change_intervals.append((observed - last_change).total_seconds() / 3600)
                    last_change = observed
                    run_started = observed
                last_observed = observed
            if last_observed > run_started:
                stability_periods.append((last_observed - run_started).total_seconds() / 3600)
        for stations in by_day_station.values():
            station_averages = {
                station_id: statistics.fmean(prices) for station_id, prices in stations.items()
            }
            floor = min(station_averages.values())
            winners = [
                station_id for station_id, price in station_averages.items() if price == floor
            ]
            if winners:
                share = 1 / len(winners)
                for station_id in winners:
                    cheapest_counts[station_id] += share
        total_periods = max(len(by_day_station), 1)
        current = market_series[-1]
        return {
            "minimum": min(values),
            "average": statistics.fmean(market_series),
            "median": statistics.median(market_series),
            "maximum": max(values),
            "volatility": statistics.pstdev(market_series) if len(market_series) > 1 else 0.0,
            "largest_increase": max(changes, default=0.0),
            "largest_decrease": min(changes, default=0.0),
            "average_change_interval_hours": (
                statistics.fmean(change_intervals) if change_intervals else None
            ),
            "average_stability_hours": (
                statistics.fmean(stability_periods) if stability_periods else None
            ),
            "current_percentile": 100
            * sum(value <= current for value in market_series)
            / len(market_series),
            "cheapest_percentages": {
                station_id: count * 100 / total_periods
                for station_id, count in cheapest_counts.items()
            },
            "observation_count": len(rows),
        }

    def record_prediction(
        self,
        location_key: str,
        fuel_type: FuelType,
        horizon_hours: int,
        predicted_price_cents: float,
        confidence: str,
        created_at: datetime | None = None,
    ) -> None:
        created = created_at or datetime.now(UTC)
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO price_predictions(
                       location_key, fuel_type, created_at, horizon_hours,
                       predicted_price_cents, confidence
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    location_key,
                    fuel_type.value,
                    created.isoformat(),
                    horizon_hours,
                    predicted_price_cents,
                    confidence,
                ),
            )

    def evaluate_predictions(
        self, location_key: str, fuel_type: FuelType, actual_price_cents: float
    ) -> int:
        now = datetime.now(UTC)
        evaluated = 0
        with self.connect() as db:
            pending = db.execute(
                """SELECT id, created_at, horizon_hours FROM price_predictions
                   WHERE location_key=? AND fuel_type=? AND evaluated_at IS NULL""",
                (location_key, fuel_type.value),
            ).fetchall()
            for row in pending:
                target = datetime.fromisoformat(str(row["created_at"])) + timedelta(
                    hours=int(row["horizon_hours"])
                )
                if now < target:
                    continue
                db.execute(
                    """UPDATE price_predictions
                       SET actual_price_cents=?, evaluated_at=? WHERE id=?""",
                    (actual_price_cents, now.isoformat(), row["id"]),
                )
                evaluated += 1
        return evaluated

    def prediction_accuracy(self, location_key: str, fuel_type: FuelType | str) -> dict[str, Any]:
        fuel = fuel_type.value if isinstance(fuel_type, FuelType) else fuel_type
        with self.connect() as db:
            row = db.execute(
                """SELECT COUNT(*) AS count,
                          AVG(ABS(predicted_price_cents-actual_price_cents)) AS mae,
                          AVG(CASE WHEN ABS(predicted_price_cents-actual_price_cents)<=2
                                   THEN 1.0 ELSE 0.0 END) * 100 AS within_two
                   FROM price_predictions
                   WHERE location_key=? AND fuel_type=? AND actual_price_cents IS NOT NULL""",
                (location_key, fuel),
            ).fetchone()
        return {
            "evaluated_count": int(row["count"]),
            "mean_absolute_error": (float(row["mae"]) if row["mae"] is not None else None),
            "within_two_cents_percent": (
                float(row["within_two"]) if row["within_two"] is not None else None
            ),
        }

    def temporal_patterns(
        self, location_key: str, fuel_type: FuelType | str, days: int, tz: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Aggregate collection averages by local weekday/hour and count real changes."""
        fuel = fuel_type.value if isinstance(fuel_type, FuelType) else fuel_type
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self.connect() as db:
            rows = db.execute(
                """SELECT station_id, fetched_at, price_cents, collection_id
                   FROM price_observations
                   WHERE location_key=? AND fuel_type=? AND fetched_at>=?
                   ORDER BY fetched_at, station_id""",
                (location_key, fuel, since),
            ).fetchall()
        zone = ZoneInfo(tz)
        buckets: dict[tuple[int, int], list[float]] = {}
        changes_by_hour: dict[int, int] = {}
        station_previous: dict[str, float] = {}
        for row in rows:
            observed = datetime.fromisoformat(str(row["fetched_at"])).astimezone(zone)
            value = float(row["price_cents"])
            buckets.setdefault((observed.weekday(), observed.hour), []).append(value)
            station_id = str(row["station_id"])
            previous = station_previous.get(station_id)
            if previous is not None and abs(previous - value) >= 0.0001:
                changes_by_hour[observed.hour] = changes_by_hour.get(observed.hour, 0) + 1
            station_previous[station_id] = value
        weekday_names = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
        weekdays = []
        for weekday in range(7):
            values = [
                value
                for (bucket_day, _), bucket_values in buckets.items()
                if bucket_day == weekday
                for value in bucket_values
            ]
            if values:
                weekdays.append(
                    {"weekday": weekday_names[weekday], "average": statistics.fmean(values)}
                )
        hours = []
        for hour in range(24):
            values = [
                value
                for (_, bucket_hour), bucket_values in buckets.items()
                if bucket_hour == hour
                for value in bucket_values
            ]
            if values:
                hours.append(
                    {
                        "hour": hour,
                        "average": statistics.fmean(values),
                        "change_count": changes_by_hour.get(hour, 0),
                        "change_frequency_percent": 100
                        * changes_by_hour.get(hour, 0)
                        / len(values),
                    }
                )
        return {"weekdays": weekdays, "hours": hours}

    def prediction_history(
        self, location_key: str, fuel_type: FuelType | str
    ) -> list[dict[str, Any]]:
        fuel = fuel_type.value if isinstance(fuel_type, FuelType) else fuel_type
        with self.connect() as db:
            rows = db.execute(
                """SELECT created_at, horizon_hours, predicted_price_cents,
                          actual_price_cents, confidence, evaluated_at
                   FROM price_predictions
                   WHERE location_key=? AND fuel_type=?
                   ORDER BY created_at""",
                (location_key, fuel),
            ).fetchall()
        return [dict(row) for row in rows]

    def backup(self, destination: Path | str) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)
