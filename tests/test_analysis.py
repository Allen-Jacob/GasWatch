from datetime import UTC, datetime

from app.domain import FuelType, StationPrice
from app.services.analysis import analyze, percentile


def station(price: float) -> StationPrice:
    return StationPrice(
        "id",
        "Station",
        "Marque",
        "Adresse",
        46.8,
        -71.2,
        2,
        FuelType.REGULAR,
        price,
        datetime.now(UTC),
    )


def test_percentile_interpolates() -> None:
    assert percentile([100, 110, 120, 130, 140], 25) == 110
    assert percentile([], 25) is None


def test_analyze_current_and_historical_prices() -> None:
    stats = analyze([station(150), station(160), station(170)], [140, 150, 160], 145)
    assert stats.minimum == 150
    assert stats.average == 160
    assert stats.median == 160
    assert stats.maximum == 170
    assert stats.historical_average == 150
