from app.domain import FuelType
from app.providers.gasquebec import GasQuebecProvider


async def test_provider_parses_documented_nearby_response(respx_mock) -> None:
    respx_mock.get("https://example.test/api/stations/nearby").respond(
        200,
        json={
            "source": {"name": "Regie essence Quebec", "publisher": "La Regie"},
            "stations": [
                {
                    "stationId": "rq2-1",
                    "name": "Costco Quebec",
                    "address": "1 rue Test",
                    "city": "Quebec",
                    "citySlug": "quebec",
                    "lat": 46.8,
                    "lng": -71.2,
                    "price": 159.9,
                    "distanceKm": 3.2,
                }
            ],
        },
    )
    provider = GasQuebecProvider("https://example.test")
    try:
        prices = await provider.get_stations(46.8, -71.2, 15, FuelType.REGULAR)
    finally:
        await provider.close()
    assert prices[0].price_cents == 159.9
    assert prices[0].source == "Regie essence Quebec (La Regie), presente par Gas Quebec"
