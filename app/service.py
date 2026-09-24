from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.database import Repository
from app.domain import FuelType, Location, StationPrice, Vehicle
from app.notifications.base import Notifier
from app.providers.base import FuelPriceProvider
from app.services.analysis import analyze, percentile
from app.services.recommendations import recommend
from app.services.reports import build_report
from app.services.stations import filter_and_rank

logger = logging.getLogger(__name__)


class GasWatchService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        provider: FuelPriceProvider,
        notifier: Notifier,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.provider = provider
        self.notifier = notifier
        self._latest: dict[tuple[str, FuelType], list[StationPrice]] = {}

    async def collect(self) -> None:
        fuels = {vehicle.fuel_type for vehicle in self.settings.configured_vehicles}
        for location in self._locations():
            for fuel_type in fuels:
                try:
                    prices = await self.provider.get_stations(
                        location.latitude,
                        location.longitude,
                        location.radius_km,
                        fuel_type,
                    )
                    selected = filter_and_rank(
                        prices,
                        self.settings.preferred_brands,
                        self._favorite_ids(),
                        self.settings.preferred_only,
                        self.settings.preferred_price_tolerance_cents,
                    )
                    # Preferences influence recommendations and notifications,
                    # but the dashboard should retain every nearby station.
                    self.repository.save_observations(location.key, prices)
                    self._latest[(location.key, fuel_type)] = selected
                    logger.info(
                        "Collecte terminee",
                        extra={
                            "location": location.key,
                            "fuel_type": fuel_type.value,
                            "stations": len(selected),
                        },
                    )
                    await self._maybe_alert(location, fuel_type, selected)
                except Exception:
                    logger.exception(
                        "Echec de la collecte pour %s/%s", location.key, fuel_type.value
                    )

    def _target(self, history: list[float]) -> float | None:
        if self.settings.target_price_mode == "MANUAL":
            return self.settings.manual_target_price_cents
        if len(history) < self.settings.minimum_history_days:
            return None
        return percentile(history, self.settings.target_price_percentile)

    def _vehicles_for(self, fuel_type: FuelType) -> list[Vehicle]:
        return [
            vehicle
            for vehicle in self.settings.configured_vehicles
            if vehicle.fuel_type == fuel_type
        ]

    def _locations(self) -> tuple[Location, ...]:
        runtime = self.repository.runtime_settings()
        configured = list(self.settings.configured_locations)
        if not runtime or not configured:
            return tuple(configured)
        primary = configured[0]
        configured[0] = Location(
            key=primary.key,
            name=primary.name,
            latitude=float(runtime.get("HOME_LATITUDE", primary.latitude)),
            longitude=float(runtime.get("HOME_LONGITUDE", primary.longitude)),
            radius_km=float(runtime.get("SEARCH_RADIUS_KM", primary.radius_km)),
        )
        return tuple(configured)

    def _favorite_ids(self) -> frozenset[str]:
        runtime = self.repository.runtime_settings().get("FAVORITE_STATION_IDS", "")
        saved = {item.strip() for item in runtime.split(",") if item.strip()}
        return self.settings.favorite_ids | frozenset(saved)

    def _fresh(self, prices: list[StationPrice]) -> list[StationPrice]:
        cutoff = datetime.now(UTC) - timedelta(minutes=self.settings.max_price_age_minutes)
        return [price for price in prices if price.fetched_at >= cutoff]

    async def _maybe_alert(
        self, location: Location, fuel_type: FuelType, prices: list[StationPrice]
    ) -> None:
        if not self.settings.price_alerts_enabled:
            return
        fresh = self._fresh(prices)
        if not fresh:
            return
        history = self.repository.daily_minimums(
            location.key, fuel_type, self.settings.history_days
        )
        target = self._target(history)
        if target is None:
            return
        station = fresh[0]
        if station.price_cents > target:
            return
        alert_key = f"target:{location.key}:{fuel_type.value}"
        last = self.repository.last_alert(alert_key)
        if last is not None:
            sent_at, old_price = last
            cooldown = timedelta(hours=self.settings.alert_cooldown_hours)
            meaningful_drop = (
                old_price is not None
                and old_price - station.price_cents >= self.settings.alert_min_price_drop_cents
            )
            if datetime.now(UTC) - sent_at < cooldown and not meaningful_drop:
                return
        message = (
            f"{fuel_type.value}: {station.price_cents:.1f} c/L chez {station.name}\n"
            f"Cible: {target:.1f} c/L — {location.name}\n"
            f"{station.source}. Prix recent, a verifier a la pompe."
        )
        try:
            await self.notifier.send("⛽ GasWatch — Alerte prix", message, "high")
        except Exception:
            self.repository.record_alert(
                alert_key, station.station_id, station.price_cents, "failed"
            )
            logger.exception("Echec de l'alerte ntfy")
        else:
            self.repository.record_alert(alert_key, station.station_id, station.price_cents, "sent")

    async def send_daily_reports(self) -> None:
        today = datetime.now(UTC).date()
        for location in self._locations():
            for fuel_type in {vehicle.fuel_type for vehicle in self.settings.configured_vehicles}:
                if self.repository.report_exists(today, location.key, fuel_type):
                    continue
                prices = self._fresh(self._latest.get((location.key, fuel_type), []))
                if not prices:
                    logger.warning("Aucune donnee fraiche pour le rapport %s", location.key)
                    continue
                history = self.repository.daily_minimums(
                    location.key, fuel_type, self.settings.history_days
                )
                target = self._target(history)
                stats = analyze(prices, history, target)
                sections: list[str] = []
                for vehicle in self._vehicles_for(fuel_type):
                    recommendation = recommend(
                        stats,
                        vehicle,
                        minimum_history_days_met=len(history) >= self.settings.minimum_history_days,
                        good_threshold=self.settings.good_price_threshold_cents,
                        high_threshold=self.settings.high_price_threshold_cents,
                        very_high_threshold=self.settings.very_high_price_threshold_cents,
                    )
                    sections.append(
                        build_report(
                            location,
                            vehicle,
                            prices[0],
                            stats,
                            recommendation,
                            self.settings.tz,
                        )
                    )
                content = "\n\n———\n\n".join(sections)
                try:
                    await self.notifier.send("⛽ GasWatch — Rapport quotidien", content)
                except Exception:
                    logger.exception("Echec du rapport ntfy")
                else:
                    self.repository.record_report(today, location.key, fuel_type, content, True)

    async def close(self) -> None:
        await self.provider.close()
        await self.notifier.close()
