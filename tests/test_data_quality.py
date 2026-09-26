from datetime import UTC, datetime, timedelta

from app.services.data_quality import price_confidence


def test_price_confidence_explains_stale_outlier() -> None:
    indicator = price_confidence(
        {
            "name": "Station",
            "address": "Adresse",
            "price_cents": 190,
            "distance_km": 2,
            "fetched_at": (datetime.now(UTC) - timedelta(hours=4)).isoformat(),
            "source": "Source",
        },
        [150, 151, 152, 190],
        collection_count_24h=3,
        expected_collections_24h=24,
        current_station_count=4,
        usual_station_count=10,
        max_age_minutes=180,
    )

    assert indicator.label == "Faible"
    assert "prix ancien" in indicator.explanation
    assert "prix inhabituel" in indicator.explanation


def test_price_confidence_is_high_for_complete_regular_data() -> None:
    indicator = price_confidence(
        {
            "name": "Station",
            "address": "Adresse",
            "price_cents": 151,
            "distance_km": 2,
            "fetched_at": datetime.now(UTC).isoformat(),
            "source": "Source",
        },
        [150, 151, 152],
        collection_count_24h=24,
        expected_collections_24h=24,
        current_station_count=3,
        usual_station_count=3,
        max_age_minutes=180,
    )

    assert indicator.score >= 95
    assert indicator.label == "Élevée"
