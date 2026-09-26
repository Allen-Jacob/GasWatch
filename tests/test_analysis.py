from datetime import UTC, datetime

from app.domain import FuelType, StationPrice
from app.services.analysis import analyze, forecast_prices, percentile, predict_price_direction


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


def test_predict_price_direction_uses_recent_local_trend() -> None:
    direction, slope, confidence = predict_price_direction([150, 151, 152, 153, 154])
    assert direction == "up"
    assert slope == 1
    assert confidence == "fort"

    assert predict_price_direction([154, 153, 152])[0] == "down"
    assert predict_price_direction([150, 150.1, 150.1])[0] == "stable"
    assert predict_price_direction([150])[0] == "unknown"


def test_forecast_requires_enough_reliable_data() -> None:
    assert forecast_prices([150, 151, 152]) == []
    forecasts = forecast_prices([150, 151, 152, 153, 154, 155, 156])
    assert [forecast.horizon_hours for forecast in forecasts] == [24, 48]
    assert forecasts[0].price_cents == 157
