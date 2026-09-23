from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.domain import Location, PriceStats, Recommendation, StationPrice, Vehicle
from app.services.trip_cost import pump_savings_cad

LABELS = {
    "FILL_NOW": "BON MOMENT POUR FAIRE LE PLEIN",
    "GOOD_PRICE": "BON PRIX",
    "NORMAL_PRICE": "PRIX NORMAL",
    "WAIT": "ATTENDRE SI POSSIBLE",
    "HIGH_PRICE": "PRIX ELEVE",
    "INSUFFICIENT_DATA": "HISTORIQUE INSUFFISANT",
}


def build_report(
    location: Location,
    vehicle: Vehicle,
    station: StationPrice,
    stats: PriceStats,
    recommendation: Recommendation,
    timezone: str,
) -> str:
    local_time = datetime.now(ZoneInfo(timezone)).strftime("%H:%M")
    target = f"{stats.target:.1f} c/L" if stats.target is not None else "a determiner"
    history = (
        f"{stats.historical_average:.1f} c/L"
        if stats.historical_average is not None
        else "insuffisante"
    )
    savings = (
        pump_savings_cad(stats.historical_average, station.price_cents, vehicle.average_fill_l)
        if stats.historical_average is not None
        else None
    )
    savings_line = (
        f"Economie brute estimee ({vehicle.average_fill_l:g} L): {savings:.2f} $\n"
        if savings is not None
        else ""
    )
    return (
        f"⛽ GasWatch — {vehicle.name}\n\n"
        f"📍 {location.name} — rayon geographique de {location.radius_km:g} km\n"
        f"Station recommandee: {station.name}\n"
        f"Prix recent: {station.price_cents:.1f} c/L\n"
        f"Distance a vol d'oiseau: {station.distance_km:.1f} km\n"
        f"Moyenne locale actuelle: {stats.average:.1f} c/L\n"
        f"Moyenne des minimums historiques: {history}\n"
        f"Prix cible: {target}\n\n"
        f"{LABELS[recommendation.code.value]}\n"
        f"{recommendation.reason}\n"
        f"{savings_line}"
        f"Donnees: {station.source}\n"
        f"Recupere a {local_time}; verifier le prix a la pompe."
    )
