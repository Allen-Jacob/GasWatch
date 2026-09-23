from __future__ import annotations

from datetime import UTC, datetime

import httpx

from app.domain import FuelType, StationPrice
from app.providers.base import FuelPriceProvider, ProviderError, RateLimitError


class GasQuebecProvider(FuelPriceProvider):
    """Client for the documented Gas Quebec read API."""

    def __init__(self, base_url: str, timeout_seconds: float = 15) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={"User-Agent": "GasWatch/0.1 (+personal self-hosted use)"},
        )

    async def get_stations(
        self,
        latitude: float,
        longitude: float,
        radius_km: float,
        fuel_type: FuelType,
    ) -> list[StationPrice]:
        try:
            response = await self._client.get(
                "/api/stations/nearby",
                params={
                    "lat": latitude,
                    "lng": longitude,
                    "radius": radius_km,
                    "fuelType": fuel_type.provider_value,
                    "limit": 10,
                    "sort": "price",
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderError("Gas Quebec est temporairement indisponible") from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            seconds = int(retry_after) if retry_after and retry_after.isdigit() else None
            raise RateLimitError(seconds)
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"Reponse Gas Quebec invalide ({response.status_code})") from exc

        source_data = payload.get("source")
        if isinstance(source_data, dict):
            name = source_data.get("name", "Regie essence Quebec")
            publisher = source_data.get("publisher", "Regie de l'energie du Quebec")
            source = f"{name} ({publisher}), presente par Gas Quebec"
        else:
            source = source_data or (
                "Donnees de prix de Regie essence Quebec, presentees par Gas Quebec"
            )
        fetched_at = datetime.now(UTC)
        stations: list[StationPrice] = []
        for item in payload.get("stations", []):
            price = item.get("price")
            if price is None:
                continue
            try:
                stations.append(
                    StationPrice(
                        station_id=str(item["stationId"]),
                        name=str(item["name"]),
                        brand=str(item.get("brand") or item["name"]),
                        address=str(item.get("address") or ""),
                        latitude=float(item["lat"]),
                        longitude=float(item["lng"]),
                        distance_km=float(item["distanceKm"]),
                        fuel_type=fuel_type,
                        price_cents=float(price),
                        fetched_at=fetched_at,
                        source=str(source),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderError("Une station retournee par Gas Quebec est invalide") from exc
        return stations

    async def close(self) -> None:
        await self._client.aclose()
