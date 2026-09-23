from __future__ import annotations

from app.domain import PriceStats, Recommendation, RecommendationCode, Vehicle


def recommend(
    stats: PriceStats,
    vehicle: Vehicle,
    *,
    minimum_history_days_met: bool,
    good_threshold: float,
    high_threshold: float,
    very_high_threshold: float,
) -> Recommendation:
    usable_range = vehicle.usable_range_km()
    urgent = (
        usable_range is not None
        and vehicle.daily_distance_km is not None
        and usable_range < vehicle.daily_distance_km * 2
    )
    if urgent:
        return Recommendation(
            RecommendationCode.FILL_NOW,
            "Autonomie estimee insuffisante pour deux jours de deplacements; "
            "mieux vaut faire le plein.",
        )
    if not minimum_history_days_met or stats.historical_average is None or stats.target is None:
        return Recommendation(
            RecommendationCode.INSUFFICIENT_DATA,
            "Historique local insuffisant pour une recommandation fiable.",
        )
    current = stats.minimum
    if current <= stats.target and current <= stats.historical_average - good_threshold:
        return Recommendation(
            RecommendationCode.FILL_NOW,
            "Le minimum actuel atteint la cible et est nettement sous la moyenne historique.",
        )
    delta = current - stats.historical_average
    if delta <= -good_threshold:
        return Recommendation(RecommendationCode.GOOD_PRICE, "Prix sous la moyenne historique.")
    if delta >= very_high_threshold:
        return Recommendation(RecommendationCode.HIGH_PRICE, "Prix tres au-dessus de l'historique.")
    if delta >= high_threshold:
        return Recommendation(
            RecommendationCode.WAIT,
            "Prix au-dessus de l'historique; attendre peut etre raisonnable "
            "si l'autonomie le permet.",
        )
    return Recommendation(RecommendationCode.NORMAL_PRICE, "Prix proche de la moyenne historique.")
