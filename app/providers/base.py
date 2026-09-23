from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain import FuelType, StationPrice


class ProviderError(RuntimeError):
    """A recoverable provider failure."""


class RateLimitError(ProviderError):
    def __init__(self, retry_after_seconds: int | None = None) -> None:
        super().__init__("Limite de requetes du fournisseur atteinte")
        self.retry_after_seconds = retry_after_seconds


class FuelPriceProvider(ABC):
    @abstractmethod
    async def get_stations(
        self,
        latitude: float,
        longitude: float,
        radius_km: float,
        fuel_type: FuelType,
    ) -> list[StationPrice]: ...

    async def close(self) -> None:
        return None
