from dataclasses import dataclass


def pump_savings_cad(reference_cents: float, station_cents: float, liters: float) -> float:
    return max(reference_cents - station_cents, 0) * liters / 100


@dataclass(frozen=True, slots=True)
class TripEconomics:
    normal_distance_km: float
    detour_km: float
    gross_savings_cad: float
    detour_cost_cad: float
    net_savings_cad: float
    verdict: str


def estimated_detour_cost_cad(
    detour_km: float, consumption_l_per_100km: float, price_cents: float
) -> float:
    liters = detour_km * consumption_l_per_100km / 100
    return liters * price_cents / 100


def estimated_round_trip_cost_cad(
    distance_km: float, consumption_l_per_100km: float, price_cents: float
) -> float:
    """Compatibility helper: ``distance_km`` is the one-way distance."""
    return estimated_detour_cost_cad(distance_km * 2, consumption_l_per_100km, price_cents)


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


def evaluate_trip(
    reference_cents: float,
    station_cents: float,
    liters: float,
    normal_distance_km: float,
    consumption_l_per_100km: float,
    max_detour_km: float,
    min_net_savings: float,
) -> TripEconomics:
    # Gas Quebec exposes geographic distance, not a route delta. Until a routing
    # provider is configured, the conservative estimate is a return trip.
    detour_km = normal_distance_km * 2
    gross = pump_savings_cad(reference_cents, station_cents, liters)
    cost = estimated_detour_cost_cad(detour_km, consumption_l_per_100km, station_cents)
    net = gross - cost
    if detour_km > max_detour_km:
        verdict = "Station trop éloignée"
    elif net < min_net_savings:
        verdict = "Économie trop faible"
    else:
        verdict = "Ça vaut le détour"
    return TripEconomics(normal_distance_km, detour_km, gross, cost, net, verdict)
