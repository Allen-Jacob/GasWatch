from datetime import UTC, datetime

import pytest

from app.domain import FuelType, StationPrice
from app.services.stations import filter_and_rank
from app.services.trip_cost import estimated_round_trip_cost_cad, net_savings_cad


def station(identifier: str, name: str, price: float, distance: float) -> StationPrice:
    return StationPrice(
        identifier,
        name,
        name,
        "",
        46,
        -71,
        distance,
        FuelType.REGULAR,
        price,
        datetime.now(UTC),
    )


def test_preferred_station_wins_within_tolerance() -> None:
    result = filter_and_rank(
        [station("1", "Autre", 150, 1), station("2", "Costco", 151, 2)],
        frozenset({"costco"}),
        frozenset(),
        False,
        2,
    )
    assert result[0].station_id == "2"


def test_exclusive_preference_can_return_no_station() -> None:
    result = filter_and_rank(
        [station("1", "Autre", 150, 1)], frozenset({"costco"}), frozenset(), True, 2
    )
    assert result == []


def test_trip_cost_and_net_savings() -> None:
    assert estimated_round_trip_cost_cad(10, 10, 160) == pytest.approx(3.2)
    assert net_savings_cad(165, 160, 50, 10, 10) == pytest.approx(-0.7)
