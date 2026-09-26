from datetime import UTC, datetime

import pytest

from app.domain import FuelType, StationPrice
from app.services.stations import filter_and_rank
from app.services.trip_cost import estimated_round_trip_cost_cad, evaluate_trip, net_savings_cad


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


def test_trip_economics_and_net_ranking() -> None:
    economics = evaluate_trip(165, 155, 50, 2, 10, 8, 2)
    assert economics.gross_savings_cad == pytest.approx(5)
    assert economics.detour_cost_cad == pytest.approx(0.62)
    assert economics.net_savings_cad == pytest.approx(4.38)
    assert economics.verdict == "Ça vaut le détour"

    result = filter_and_rank(
        [station("near", "Proche", 160, 0.5), station("far", "Loin", 150, 10)],
        frozenset(),
        frozenset(),
        False,
        0,
        reference_cents=165,
        liters=50,
        consumption_l_per_100km=10,
        max_detour_km=8,
        min_net_savings=2,
    )
    assert result[0].station_id == "near"
