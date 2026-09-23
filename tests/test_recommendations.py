from app.domain import FuelType, PriceStats, RecommendationCode, Vehicle
from app.services.recommendations import recommend


def vehicle(**overrides: object) -> Vehicle:
    values = {
        "key": "CAR",
        "name": "Auto",
        "fuel_type": FuelType.REGULAR,
        "consumption_l_per_100km": 10,
        "tank_capacity_l": 50,
        "average_fill_l": 40,
    }
    values.update(overrides)
    return Vehicle(**values)  # type: ignore[arg-type]


def recommendation(current: float, historical: float, target: float, car: Vehicle | None = None):
    return recommend(
        PriceStats(current, current + 2, current + 1, current + 4, 3, target, historical),
        car or vehicle(),
        minimum_history_days_met=True,
        good_threshold=5,
        high_threshold=5,
        very_high_threshold=10,
    )


def test_fill_now_at_target_and_below_history() -> None:
    assert recommendation(145, 155, 148).code == RecommendationCode.FILL_NOW


def test_wait_and_high_price() -> None:
    assert recommendation(156, 150, 145).code == RecommendationCode.WAIT
    assert recommendation(161, 150, 145).code == RecommendationCode.HIGH_PRICE


def test_low_tank_overrides_high_price() -> None:
    car = vehicle(tank_level_percent=22, daily_distance_km=30, minimum_reserve_percent=20)
    assert recommendation(170, 150, 145, car).code == RecommendationCode.FILL_NOW


def test_insufficient_history() -> None:
    result = recommend(
        PriceStats(150, 155, 154, 160, 4),
        vehicle(),
        minimum_history_days_met=False,
        good_threshold=5,
        high_threshold=5,
        very_high_threshold=10,
    )
    assert result.code == RecommendationCode.INSUFFICIENT_DATA
