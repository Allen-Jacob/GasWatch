def pump_savings_cad(reference_cents: float, station_cents: float, liters: float) -> float:
    return max(reference_cents - station_cents, 0) * liters / 100


def estimated_round_trip_cost_cad(
    distance_km: float, consumption_l_per_100km: float, price_cents: float
) -> float:
    liters = distance_km * 2 * consumption_l_per_100km / 100
    return liters * price_cents / 100


def net_savings_cad(
    reference_cents: float,
    station_cents: float,
    liters: float,
    distance_km: float,
    consumption_l_per_100km: float,
) -> float:
    return pump_savings_cad(reference_cents, station_cents, liters) - estimated_round_trip_cost_cad(
        distance_km, consumption_l_per_100km, station_cents
    )
