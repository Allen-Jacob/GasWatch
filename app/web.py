from __future__ import annotations

import csv
import html
import io
import json
import logging
import secrets
import statistics
import threading
from collections import defaultdict
from datetime import UTC, date, datetime
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

from app.config import Settings
from app.database import Repository
from app.domain import PriceStats, RecommendationCode, Vehicle
from app.services.analysis import forecast_prices, percentile, predict_price_direction
from app.services.data_quality import price_confidence
from app.services.recommendations import recommend
from app.services.trip_cost import evaluate_trip, pump_savings_cad

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("static")
MAX_RECEIPT_BYTES = 5 * 1024 * 1024
ALLOWED_RECEIPT_TYPES = {"image/jpeg", "image/png", "application/pdf"}


def _decode_form(
    body: bytes, content_type: str
) -> tuple[dict[str, list[str]], dict[str, tuple[str, str, bytes]]]:
    if not content_type.startswith("multipart/form-data"):
        return parse_qs(body.decode("utf-8"), keep_blank_values=True), {}
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    )
    form: dict[str, list[str]] = {}
    files: dict[str, tuple[str, str, bytes]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename:
            files[name] = (Path(filename).name, part.get_content_type(), payload)
        else:
            form.setdefault(name, []).append(payload.decode("utf-8"))
    return form, files


def _period_days(value: str, default: int = 30) -> int | None:
    if value == "all":
        return None
    try:
        return max(1, min(int(value), 3650))
    except ValueError:
        return default


def _station_ids(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _updated_station_preferences(
    runtime: dict[str, str], station_id: str, action: str
) -> dict[str, str]:
    favorites = _station_ids(runtime.get("FAVORITE_STATION_IDS", ""))
    excluded = _station_ids(runtime.get("EXCLUDED_STATION_IDS", ""))
    if action == "favorite":
        favorites.add(station_id)
        excluded.discard(station_id)
    elif action == "unfavorite":
        favorites.discard(station_id)
    elif action == "exclude":
        excluded.add(station_id)
        favorites.discard(station_id)
    elif action == "include":
        excluded.discard(station_id)
    else:
        raise ValueError("Action de station invalide")
    return {
        "FAVORITE_STATION_IDS": ",".join(sorted(favorites)),
        "EXCLUDED_STATION_IDS": ",".join(sorted(excluded)),
    }


def _age_label(timestamp: str) -> tuple[str, str]:
    observed = datetime.fromisoformat(timestamp)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    minutes = max(int((datetime.now(UTC) - observed).total_seconds() / 60), 0)
    if minutes < 2:
        return "a l'instant", "fresh"
    if minutes < 60:
        return f"il y a {minutes} min", "fresh"
    hours = minutes // 60
    if hours < 24:
        return f"il y a {hours} h", "aging"
    return f"il y a {hours // 24} j", "stale"


def _brand_logo(brand: object, name: object) -> str:
    """Return a compact, offline brand mark with a generic fallback."""
    label = str(brand or name).strip()
    normalized = label.casefold().replace("-", " ")
    brands = (
        ("costco", "costco", "Costco"),
        ("irving", "irving", "Irving"),
        ("petro canada", "petro-canada", "Petro"),
        ("esso", "esso", "Esso"),
        ("shell", "shell", "Shell"),
        ("ultramar", "ultramar", "Ultra"),
        ("canadian tire", "canadian-tire", "CT"),
        ("couchetard", "couche-tard", "C-T"),
        ("couche tard", "couche-tard", "C-T"),
    )
    for needle, css_name, wordmark in brands:
        if needle in normalized:
            return f'<span class="brand-logo brand-{css_name}" aria-hidden="true">{wordmark}</span>'
    initial = next((character.upper() for character in label if character.isalnum()), "⛽")
    return (
        f'<span class="brand-logo brand-generic" aria-hidden="true">{html.escape(initial)}</span>'
    )


def _price_trend(points: list[float]) -> str:
    if len(points) < 2:
        return '<span class="price-direction unknown" title="Tendance en calcul">·</span>'
    change = points[-1] - points[-2]
    if abs(change) < 0.05:
        return '<span class="price-direction stable" title="Prix stable">→</span>'
    direction = "up" if change > 0 else "down"
    label = "En hausse" if change > 0 else "En baisse"
    arrow = "↑" if change > 0 else "↓"
    return (
        f'<span class="price-direction {direction}" '
        f'title="{label} de {abs(change):.1f} c/L depuis le relevé précédent" '
        f'aria-label="{label} de {abs(change):.1f} cents par litre">{arrow}</span>'
    )


def _apple_maps_url(station: dict[str, object]) -> str:
    query = f"{station['name']}, {station['address']}"
    coordinates = f"{float(station['latitude']):.6f},{float(station['longitude']):.6f}"
    return "https://maps.apple.com/?" + urlencode({"q": query, "ll": coordinates})


def _forecast(prices: list[float]) -> tuple[str, str, str, str]:
    direction, slope, confidence = predict_price_direction(prices)
    estimates = forecast_prices(prices)
    estimate_detail = (
        " · "
        + " · ".join(
            f"est. {forecast.horizon_hours} h {forecast.price_cents:.1f} c/L"
            for forecast in estimates
        )
        if estimates
        else ""
    )
    if direction == "unknown":
        return "unknown", "·", "Tendance à venir : en calcul", "Il faut au moins 2 jours"
    if direction == "stable":
        return (
            "stable",
            "→",
            "Tendance à venir : plutôt stable",
            f"Signal {confidence} · {len(prices[-7:])} jours de vos données{estimate_detail}",
        )
    label = "hausse" if direction == "up" else "baisse"
    arrow = "↑" if direction == "up" else "↓"
    return (
        direction,
        arrow,
        f"Tendance à venir : {label} probable",
        f"Signal {confidence} · {slope:+.1f} c/L par jour · vos {len(prices[-7:])} derniers jours{estimate_detail}",
    )


def _sparkline(
    points: list[float],
    label: str = "Evolution du prix",
    point_labels: list[str] | None = None,
) -> str:
    if not points:
        return '<div class="empty-chart">Historique en construction</div>'
    width, height, padding = 560, 118, 8
    low, high = min(points), max(points)
    spread = max(high - low, 1)
    coords: list[str] = []
    markers: list[str] = []
    for index, value in enumerate(points):
        x = padding + index * (width - 2 * padding) / max(len(points) - 1, 1)
        y = height - padding - (value - low) * (height - 2 * padding) / spread
        coords.append(f"{x:.1f},{y:.1f}")
        point_label = (
            point_labels[index] if point_labels and index < len(point_labels) else "Releve"
        )
        tooltip = html.escape(f"{point_label} · {value:.1f} c/L", quote=True)
        markers.append(
            f'<circle class="chart-point" cx="{x:.1f}" cy="{y:.1f}" r="5" '
            f'tabindex="0" data-tooltip="{tooltip}"><title>{tooltip}</title></circle>'
        )
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" data-chart '
        f'aria-label="{html.escape(label)}"><polyline points="{" ".join(coords)}" />'
        f'{"".join(markers)}<text x="8" y="16">{high:.1f}</text>'
        f'<text x="8" y="110">{low:.1f}</text></svg>'
    )


def _recommendation_banner(
    stations: list[dict[str, object]],
    market_history: list[dict[str, object]],
    location_key: str,
    fuel_type: str,
    location_name: str,
    settings: Settings,
) -> str:
    daily_minimums = [float(item["minimum"]) for item in market_history]
    daily_averages = [float(item["average"]) for item in market_history]
    target = (
        settings.manual_target_price_cents
        if settings.target_price_mode == "MANUAL"
        else percentile(daily_minimums, settings.target_price_percentile)
        if len(daily_minimums) >= settings.minimum_history_days
        else None
    )
    prices = [float(item["price_cents"]) for item in stations]
    historical_average = statistics.fmean(daily_minimums) if daily_minimums else None
    stats = PriceStats(
        minimum=min(prices),
        average=statistics.fmean(prices),
        median=statistics.median(prices),
        maximum=max(prices),
        station_count=len(prices),
        target=target,
        historical_average=historical_average,
    )
    vehicle = next(
        (
            candidate
            for candidate in settings.configured_vehicles
            if candidate.fuel_type.value == fuel_type
        ),
        settings.configured_vehicles[0],
    )
    result = recommend(
        stats,
        vehicle,
        minimum_history_days_met=len(daily_minimums) >= settings.minimum_history_days,
        good_threshold=settings.good_price_threshold_cents,
        high_threshold=settings.high_price_threshold_cents,
        very_high_threshold=settings.very_high_price_threshold_cents,
    )
    presentation = {
        RecommendationCode.FILL_NOW: (
            "excellent",
            "Excellent moment pour faire le plein",
            "Faire le plein",
        ),
        RecommendationCode.GOOD_PRICE: ("good", "Bon moment pour acheter", "Bon prix"),
        RecommendationCode.NORMAL_PRICE: ("normal", "Prix dans la normale", "Prix normal"),
        RecommendationCode.WAIT: ("wait", "Attendre peut valoir la peine", "Attendre"),
        RecommendationCode.HIGH_PRICE: ("high", "Prix eleve en ce moment", "Prix eleve"),
        RecommendationCode.INSUFFICIENT_DATA: (
            "learning",
            "Analyse en cours",
            "Donnees en apprentissage",
        ),
    }
    tone, heading, badge = presentation[result.code]
    comparison = (
        f"{stats.minimum - historical_average:+.1f} c/L vs moyenne {settings.history_days} jours"
        if historical_average is not None
        else "Comparaison disponible bientot"
    )
    target_label = f"{target:.1f} c/L" if target is not None else "En calcul"
    banner_id = "advice-" + "".join(
        character if character.isalnum() else "-" for character in f"{location_key}-{fuel_type}"
    )
    forecast_tone, forecast_arrow, forecast_title, forecast_detail = _forecast(daily_averages)
    return f"""
    <details class="buy-advice {tone}" aria-labelledby="{html.escape(banner_id)}">
      <summary class="advice-summary">
        <span class="advice-icon" aria-hidden="true">↗</span>
        <span class="advice-copy"><span class="eyebrow">Verdict · {html.escape(location_name)}</span>
          <strong id="{html.escape(banner_id)}">{html.escape(heading)}</strong></span>
        <span class="forecast-compact {forecast_tone}" title="{html.escape(forecast_detail)}">
          <span aria-hidden="true">{forecast_arrow}</span>{html.escape(forecast_title)}</span>
        <span class="advice-badge">{html.escape(badge)}</span>
      </summary>
      <div class="advice-details">
        <p>{html.escape(result.reason)}</p>
        <div class="forecast {forecast_tone}">
          <span class="forecast-arrow" aria-hidden="true">{forecast_arrow}</span>
          <span><strong>{html.escape(forecast_title)}</strong>
            <small>{html.escape(forecast_detail)}</small></span>
        </div>
        <div class="advice-metrics">
          <div><span>Meilleur prix</span><strong>{stats.minimum:.1f} c/L</strong></div>
          <div><span>Comparaison</span><strong>{html.escape(comparison)}</strong></div>
          <div><span>Cible personnelle</span><strong>{html.escape(target_label)}</strong></div>
          <div><span>Historique</span><strong>{len(daily_minimums)} jour{"s" if len(daily_minimums) != 1 else ""}</strong></div>
        </div>
      </div>
    </details>
    """


def render_dashboard(repository: Repository, settings: Settings) -> str:
    runtime = repository.runtime_settings()
    favorite_ids = _station_ids(runtime.get("FAVORITE_STATION_IDS", ""))
    excluded_ids = _station_ids(runtime.get("EXCLUDED_STATION_IDS", ""))
    all_snapshot = repository.dashboard_snapshot()
    snapshot = [
        row
        for row in repository.dashboard_snapshot(settings.max_price_age_minutes)
        if str(row["station_id"]) not in excluded_ids
    ]
    history = repository.dashboard_history(settings.history_days)
    station_history = repository.dashboard_station_history(settings.history_days)
    station_comparison = {
        (str(row["location_key"]), str(row["fuel_type"]), str(row["station_id"])): row
        for row in repository.station_comparison(30)
    }
    grouped_history: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in history:
        grouped_history[(row["location_key"], row["fuel_type"])].append(row)
    grouped_station_history: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in station_history:
        grouped_station_history[
            (str(row["location_key"]), str(row["fuel_type"]), str(row["station_id"]))
        ].append(row)

    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in snapshot:
        groups[(row["location_key"], row["fuel_type"])].append(row)

    primary_location = settings.configured_locations[0]
    latitude = runtime.get("HOME_LATITUDE", str(primary_location.latitude))
    longitude = runtime.get("HOME_LONGITUDE", str(primary_location.longitude))
    radius = runtime.get("SEARCH_RADIUS_KM", str(primary_location.radius_km))
    location_names = {location.key: location.name for location in settings.configured_locations}
    excluded_by_id = {
        str(row["station_id"]): row
        for row in all_snapshot
        if str(row["station_id"]) in excluded_ids
    }
    excluded_controls = "".join(
        f"""
        <form method="post" action="/station-preference" class="excluded-station">
          <input type="hidden" name="csrf" value="{{csrf_token}}">
          <input type="hidden" name="station_id" value="{html.escape(station_id)}">
          <input type="hidden" name="action" value="include">
          <span><strong>{html.escape(str(row["name"]))}</strong>
            <small>{html.escape(str(row["address"]))}</small></span>
          <button type="submit">Reafficher</button>
        </form>
        """
        for station_id, row in excluded_by_id.items()
    )
    vehicle_summary = ", ".join(
        f"{vehicle.name} ({vehicle.fuel_type.value}, {vehicle.average_fill_l:.0f} L)"
        for vehicle in settings.configured_vehicles
    )

    def metric(value: float | None, suffix: str = " c/L") -> str:
        return f"{value:+.1f}{suffix}" if value is not None else "—"

    sections: list[str] = []
    advice_banners: list[str] = []
    for (location_key, fuel_type), stations in groups.items():
        vehicle = next(
            (
                candidate
                for candidate in settings.configured_vehicles
                if candidate.fuel_type.value == fuel_type
            ),
            settings.configured_vehicles[0],
        )
        reference_price = statistics.fmean(float(item["price_cents"]) for item in stations)
        quality = repository.collection_quality(location_key, fuel_type)
        expected_collections = max(1, 24 * 60 // settings.price_check_interval_minutes)
        market_prices = [float(item["price_cents"]) for item in stations]
        economics = {
            str(item["station_id"]): evaluate_trip(
                reference_price,
                float(item["price_cents"]),
                vehicle.average_fill_l,
                float(item["distance_km"]),
                vehicle.consumption_l_per_100km,
                settings.max_detour_km,
                settings.min_net_savings,
            )
            for item in stations
        }
        stations.sort(
            key=lambda item: (
                economics[str(item["station_id"])].detour_km > settings.max_detour_km,
                -economics[str(item["station_id"])].net_savings_cad,
                float(item["price_cents"]),
            )
        )
        best = stations[0]
        average = sum(float(item["price_cents"]) for item in stations) / len(stations)
        median = statistics.median(float(item["price_cents"]) for item in stations)
        maximum = max(float(item["price_cents"]) for item in stations)
        age, freshness = _age_label(str(best["fetched_at"]))
        station_cards: list[tuple[bool, str]] = []
        for item in stations:
            station_id = str(item["station_id"])
            is_favorite = station_id in favorite_ids
            station_history_rows = grouped_station_history[(location_key, fuel_type, station_id)]
            station_points = [float(point["price_cents"]) for point in station_history_rows]
            station_days = [str(point["day"]) for point in station_history_rows]
            station_min = min(station_points) if station_points else None
            station_max = max(station_points) if station_points else None
            station_change = (
                station_points[-1] - station_points[0] if len(station_points) > 1 else None
            )
            comparison = station_comparison.get((location_key, fuel_type, station_id), {})
            trip = economics[station_id]
            confidence = price_confidence(
                item,
                market_prices,
                collection_count_24h=quality["collection_count_24h"],
                expected_collections_24h=expected_collections,
                current_station_count=len(stations),
                usual_station_count=max(quality["usual_station_count"], len(stations)),
                max_age_minutes=settings.max_price_age_minutes,
            )
            fill_cost = float(item["price_cents"]) * vehicle.average_fill_l / 100
            trip_verdict = (
                "Station la plus rentable"
                if station_id == str(best["station_id"])
                and trip.net_savings_cad >= settings.min_net_savings
                else trip.verdict
            )
            logo = _brand_logo(item.get("brand"), item.get("name"))
            price_trend = _price_trend(station_points)
            maps_url = html.escape(_apple_maps_url(item), quote=True)
            station_key = "station-" + "".join(
                character if character.isalnum() else "-"
                for character in f"{location_key}-{fuel_type}-{station_id}"
            )
            favorite_action = "unfavorite" if is_favorite else "favorite"
            favorite_label = "Retirer des favoris" if is_favorite else "Ajouter aux favoris"
            station_cards.append(
                (
                    is_favorite,
                    f"""
                <form method="post" action="/station-preference" class="station-card-form">
                  <input type="hidden" name="csrf" value="{{csrf_token}}">
                  <input type="hidden" name="station_id" value="{html.escape(station_id)}">
                  <input type="hidden" name="return_hash" value="{html.escape(station_key)}">
                  <details class="station-card {"is-favorite" if is_favorite else ""}" id="{html.escape(station_key)}" data-station>
                  <summary>
                    <span class="station-name">{logo}<span class="station-copy">
                      <strong>{html.escape(str(item["name"]))}</strong>
                      <a class="station-address" href="{maps_url}" target="_blank" rel="noopener noreferrer"
                        title="Ouvrir dans Apple Maps">{html.escape(str(item["address"]))}<span aria-hidden="true"> ↗</span></a></span></span>
                    <span class="station-price"><span>{float(item["price_cents"]):.1f}<small> c/L</small></span>{price_trend}</span>
                    <span>{float(item["distance_km"]):.1f} km</span>
                    <span>{html.escape(_age_label(str(item["fetched_at"]))[0])}</span>
                    <span class="station-actions">
                      <button type="submit" class="icon-button favorite-button {"active" if is_favorite else ""}"
                        name="action" value="{favorite_action}"
                        aria-label="{favorite_label}: {html.escape(str(item["name"]))}, {html.escape(str(item["address"]))}"
                        title="{favorite_label}">{"★" if is_favorite else "☆"}</button>
                      <button type="submit" class="icon-button exclude-button" name="action" value="exclude"
                        aria-label="Exclure: {html.escape(str(item["name"]))}, {html.escape(str(item["address"]))}"
                        title="Ne plus afficher cette station">×</button>
                    </span>
                  </summary>
                  <div class="station-detail">
                    <div><p class="eyebrow">Historique de cette station</p>
                      <h3>Prix sur les {settings.history_days} derniers jours</h3></div>
                    {_sparkline(station_points, f"Prix sur {settings.history_days} jours pour {item['name']}", station_days)}
                    <div class="station-stats">
                      <div><span>Minimum</span><strong>{metric(station_min).lstrip("+")}</strong></div>
                      <div><span>Maximum</span><strong>{metric(station_max).lstrip("+")}</strong></div>
                      <div><span>Moyenne 7 j</span><strong>{metric(float(comparison["average_7d"]) if comparison.get("average_7d") is not None else None).lstrip("+")}</strong></div>
                      <div><span>Moyenne 30 j</span><strong>{metric(float(comparison["average_30d"]) if comparison.get("average_30d") is not None else None).lstrip("+")}</strong></div>
                      <div><span>Variation 24 h</span><strong>{metric(float(comparison["change_24h"]) if comparison.get("change_24h") is not None else station_change)}</strong></div>
                      <div><span>Écart local</span><strong>{metric(float(item["price_cents"]) - average)}</strong></div>
                      <div><span>Plein estimé</span><strong>{fill_cost:.2f} $</strong></div>
                      <div><span>Distance normale</span><strong>{trip.normal_distance_km:.1f} km</strong></div>
                      <div><span>Détour estimé</span><strong>{trip.detour_km:.1f} km</strong></div>
                      <div><span>Coût estimé du détour</span><strong>{trip.detour_cost_cad:.2f} $</strong></div>
                      <div><span>Économie brute</span><strong>{trip.gross_savings_cad:.2f} $</strong></div>
                      <div><span>Économie nette</span><strong>{trip.net_savings_cad:+.2f} $</strong></div>
                      <div><span>Verdict</span><strong>{html.escape(trip_verdict)}</strong></div>
                      <div><span>Confiance</span><strong>{confidence.score}/100 · {confidence.label}</strong></div>
                    </div>
                    <small>Qualité : {html.escape(confidence.explanation)}. Fraîcheur {confidence.freshness}/100 · régularité {confidence.regularity}/100 · couverture {confidence.coverage}/100 · cohérence {confidence.consistency}/100.</small><br>
                    <small>Calcul sur {vehicle.average_fill_l:.0f} L et {vehicle.consumption_l_per_100km:.1f} L/100 km. Le détour est estimé à partir de la distance géographique aller-retour.</small>
                  </div>
                  </details>
                </form>
                """,
                )
            )
        favorite_cards = [card for is_favorite, card in station_cards if is_favorite]
        other_cards = [card for is_favorite, card in station_cards if not is_favorite]
        if favorite_cards:
            visible_cards = favorite_cards
            hidden_cards = other_cards
        else:
            visible_cards = other_cards[:8]
            hidden_cards = other_cards[8:]
        more_stations = (
            f"""
            <details class="more-stations">
              <summary>Voir les {len(hidden_cards)} autres stations</summary>
              <div>{"".join(hidden_cards)}</div>
            </details>
            """
            if hidden_cards
            else ""
        )
        station_list = "".join(visible_cards) + more_stations
        market_history = grouped_history[(location_key, fuel_type)]
        averages = [float(item["average"]) for item in market_history]
        market_days = [str(item["day"]) for item in market_history]
        chart = _sparkline(
            averages,
            "Moyenne quotidienne des stations suivies",
            market_days,
        )
        history_min = min(averages) if averages else None
        history_max = max(averages) if averages else None
        history_avg = sum(averages) / len(averages) if averages else None
        change = averages[-1] - averages[0] if len(averages) > 1 else None

        def period_change(
            days: int,
            averages: list[float] = averages,
            market_history: list[dict[str, object]] = market_history,
        ) -> float | None:
            if len(averages) < 2:
                return None
            cutoff = datetime.now(UTC).date().toordinal() - days
            candidates = [
                (datetime.fromisoformat(str(row["day"])).date().toordinal(), float(row["average"]))
                for row in market_history
                if datetime.fromisoformat(str(row["day"])).date().toordinal() <= cutoff
            ]
            return averages[-1] - candidates[-1][1] if candidates else None

        change_24h = period_change(1)
        change_7d = period_change(7)
        change_30d = period_change(30)
        historical_delta = average - history_avg if history_avg is not None else None
        potential_savings = pump_savings_cad(
            average,
            float(best["price_cents"]),
            next(
                (
                    vehicle.average_fill_l
                    for vehicle in settings.configured_vehicles
                    if vehicle.fuel_type.value == fuel_type
                ),
                settings.configured_vehicles[0].average_fill_l,
            ),
        )
        best_logo = _brand_logo(best.get("brand"), best.get("name"))
        market_price_trend = _price_trend(averages)

        stats_cards = (
            f"<div><span>Minimum</span><strong>{metric(history_min).lstrip('+')}</strong></div>"
            f"<div><span>Moyenne</span><strong>{metric(history_avg).lstrip('+')}</strong></div>"
            f"<div><span>Maximum</span><strong>{metric(history_max).lstrip('+')}</strong></div>"
            f"<div><span>Variation</span><strong>{metric(change)}</strong></div>"
        )
        current_cards = "".join(
            (
                f"<div><span>Minimum actuel</span><strong>{float(best['price_cents']):.1f} c/L</strong></div>",
                f"<div><span>Moyenne actuelle</span><strong>{average:.1f} c/L</strong></div>",
                f"<div><span>Médiane actuelle</span><strong>{median:.1f} c/L</strong></div>",
                f"<div><span>Maximum actuel</span><strong>{maximum:.1f} c/L</strong></div>",
                f"<div><span>Variation 24 h</span><strong>{metric(change_24h)}</strong></div>",
                f"<div><span>Variation 7 j</span><strong>{metric(change_7d)}</strong></div>",
                f"<div><span>Variation 30 j</span><strong>{metric(change_30d)}</strong></div>",
                f"<div><span>Écart min–max</span><strong>{maximum - float(best['price_cents']):.1f} c/L</strong></div>",
                f"<div><span>Vs historique</span><strong>{metric(historical_delta)}</strong></div>",
                f"<div><span>Stations suivies</span><strong>{len(stations)}</strong></div>",
                f"<div><span>Dernière collecte</span><strong>{html.escape(age)}</strong></div>",
                f"<div><span>Économie brute / plein</span><strong>{potential_savings:.2f} $</strong></div>",
            )
        )
        advice_banners.append(
            _recommendation_banner(
                stations,
                market_history,
                location_key,
                fuel_type,
                location_names.get(location_key, location_key),
                settings,
            )
        )
        sections.append(
            f"""
            <section class="market">
              <div class="market-head">
                <div><p class="eyebrow">{html.escape(fuel_type)}</p>
                  <h2>{html.escape(location_names.get(location_key, location_key))}</h2></div>
                <span class="status {freshness}">{html.escape(age)}</span>
              </div>
              <div class="summary-grid">
                <article class="hero-price"><span>Station la plus rentable</span>
                  <strong>{float(best["price_cents"]):.1f}<small> c/L</small>{_price_trend([float(point["price_cents"]) for point in grouped_station_history[(location_key, fuel_type, str(best["station_id"]))]])}</strong>
                  <p class="hero-station">{best_logo}{html.escape(str(best["name"]))}</p></article>
                <article><span>Moyenne locale</span><strong>{average:.1f}<small> c/L</small>{market_price_trend}</strong>
                  <p>{len(stations)} stations disponibles</p></article>
                <article class="trend"><span>Moyenne des stations suivies — {settings.history_days} jours</span>
                  {chart}<div class="history-stats">{stats_cards}</div></article>
              </div>
              <div class="kpi-grid">{current_cards}</div>
              <div class="station-list-head"><span>Station</span><span>Prix</span>
                <span>Distance*</span><span>Releve</span></div>
              <div class="station-list">{station_list}</div>
            </section>
            """
        )

    if not sections:
        sections.append(
            """
            <section class="empty"><div class="pump">⛽</div><h2>Premiere collecte en cours</h2>
            <p>Les stations apparaitront ici des qu'une collecte aura ete enregistree.</p></section>
            """
        )

    generated = datetime.now(UTC).astimezone(ZoneInfo(settings.tz)).strftime("%Y-%m-%d %H:%M %Z")
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta name="description" content="Tableau de bord local GasWatch">
<meta name="theme-color" content="#111110"><meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="GasWatch">
<meta http-equiv="refresh" content="60"><title>GasWatch</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<style>
:root{{--ink:#f4f1e8;--muted:#aaa69d;--panel:#171715;--panel2:#201f1c;--line:#393732;
--accent:#e6c77a;--soft:#fff3d3;--dim:#7d7971;--black:#0e0e0d;--green:#71d99b;
--amber:#f0b45f;--red:#ef7d72;--blue:#70b8d7}}*{{box-sizing:border-box}}
@media(prefers-color-scheme:light){{:root{{--ink:#24211d;--muted:#6b665d;--panel:#fffdf7;--panel2:#f3eee2;--line:#d8d0bf;--accent:#8a6513;--soft:#49360c;--dim:#81796b;--black:#f6f2e8}}}}
body{{margin:0;background:var(--black);color:var(--ink);font:16px/1.5 ui-sans-serif,system-ui,sans-serif}}
.main-nav{{display:flex;gap:6px;margin:-12px 0 24px;overflow:auto}}.main-nav a{{color:var(--muted);text-decoration:none;padding:8px 12px;border-radius:9px}}.main-nav a:hover,.main-nav a.active{{color:var(--black);background:var(--accent)}}
.shell{{width:min(1180px,calc(100% - 32px));margin:auto;padding:34px 0 64px}}
header{{position:relative;z-index:5;display:flex;align-items:end;justify-content:space-between;margin-bottom:28px;border-bottom:1px solid var(--line);padding-bottom:20px}}
h1{{font-size:clamp(2rem,5vw,4rem);letter-spacing:-.06em;line-height:.9;margin:0}}h1 b{{color:var(--accent)}}
header p,.meta,article p{{color:var(--muted);margin:.35rem 0 0;font-size:.875rem}}
.header-tools{{display:flex;align-items:center;gap:14px;text-align:right}}.settings-menu{{position:relative}}
.settings-menu>summary{{display:grid;place-items:center;width:42px;height:42px;padding:0;list-style:none;cursor:pointer;border:1px solid var(--line);border-radius:11px;background:var(--panel);color:var(--accent);font-size:1.25rem}}
.settings-menu>summary::-webkit-details-marker{{display:none}}.settings-menu>summary:hover,.settings-menu[open]>summary{{background:var(--panel2);border-color:var(--accent)}}
.settings-menu>.settings{{position:absolute;top:calc(100% + 12px);right:0;width:min(720px,calc(100vw - 32px));z-index:30;text-align:left;box-shadow:0 22px 70px #000c}}
.buy-advice{{position:relative;margin-bottom:16px;border:1px solid color-mix(in srgb,var(--verdict) 52%,var(--line));
border-radius:18px;background:linear-gradient(120deg,color-mix(in srgb,var(--verdict) 14%,var(--panel)),var(--panel) 62%);overflow:hidden}}
.buy-advice::after{{content:'';position:absolute;width:180px;height:180px;right:-70px;top:-100px;border-radius:50%;background:var(--verdict);opacity:.09}}
.buy-advice.excellent,.buy-advice.good{{--verdict:var(--green)}}.buy-advice.normal{{--verdict:var(--blue)}}
.buy-advice.wait,.buy-advice.learning{{--verdict:var(--amber)}}.buy-advice.high{{--verdict:var(--red)}}
.advice-summary{{position:relative;z-index:1;display:grid;grid-template-columns:auto minmax(160px,1fr) minmax(210px,auto) auto auto;gap:12px;align-items:center;padding:13px 16px;cursor:pointer;list-style:none}}
.advice-summary::-webkit-details-marker{{display:none}}.advice-summary::after{{content:'+';display:grid;place-items:center;width:24px;height:24px;border-radius:7px;background:#ffffff0b;color:var(--verdict);font-weight:900}}
.buy-advice[open] .advice-summary::after{{content:'−'}}.advice-summary:hover{{background:#ffffff05}}
.advice-icon{{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:var(--verdict);color:#10110f;font-size:1.1rem;font-weight:900}}
.advice-copy span,.advice-copy strong{{display:block}}.advice-copy strong{{font-size:1rem;margin-top:1px}}
.advice-badge{{position:relative;z-index:1;color:var(--verdict);border:1px solid color-mix(in srgb,var(--verdict) 55%,transparent);background:#111;
padding:5px 9px;border-radius:99px;font-size:.7rem;font-weight:800}}
.forecast-compact{{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:.78rem;font-weight:750}}.forecast-compact>span{{display:grid;place-items:center;width:24px;height:24px;border-radius:7px;font-size:1rem;font-weight:950}}
.forecast-compact.up>span{{color:var(--red);background:#ef7d7218}}.forecast-compact.down>span{{color:var(--green);background:#71d99b18}}.forecast-compact.stable>span{{color:var(--blue);background:#70b8d718}}.forecast-compact.unknown>span{{color:var(--dim);background:#ffffff0a}}
.advice-details{{position:relative;z-index:1;padding:0 16px 16px;border-top:1px solid var(--line)}}.advice-details>p{{color:var(--muted);font-size:.88rem;margin:14px 0}}
.forecast{{display:flex;align-items:center;gap:11px;padding:12px 14px;border-radius:11px;
background:#10100f;border:1px solid var(--line)}}.forecast-arrow{{display:grid;place-items:center;flex:0 0 32px;height:32px;border-radius:9px;font-size:1.25rem;font-weight:900}}
.forecast strong,.forecast small{{display:block}}.forecast strong{{font-size:.9rem}}.forecast small{{color:var(--muted);font-size:.75rem;margin-top:1px}}
.forecast.up .forecast-arrow{{color:#18110e;background:var(--red)}}.forecast.down .forecast-arrow{{color:#0c1510;background:var(--green)}}
.forecast.stable .forecast-arrow{{color:#101417;background:var(--blue)}}.forecast.unknown .forecast-arrow{{color:#171512;background:var(--amber)}}
.advice-metrics{{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--line);padding-top:16px;margin-top:14px}}
.advice-metrics div{{padding:0 16px;border-right:1px solid var(--line)}}.advice-metrics div:first-child{{padding-left:0}}.advice-metrics div:last-child{{border:0}}
.advice-metrics span{{display:block;color:var(--muted);font-size:.67rem;text-transform:uppercase;letter-spacing:.07em}}
.advice-metrics strong{{display:block;font-size:.92rem;margin-top:3px}}
.market{{background:var(--panel);border:1px solid var(--line);border-radius:18px;overflow:hidden;margin-bottom:24px}}
.market-head{{display:flex;align-items:center;justify-content:space-between;padding:24px 26px 18px}}
.eyebrow{{color:var(--accent);font-size:.75rem;font-weight:800;letter-spacing:.15em;margin:0;text-transform:uppercase}}
h2{{margin:2px 0 0;font-size:1.5rem}}h3{{margin:2px 0 0;font-size:1.1rem}}
.status{{font-size:.78rem;padding:6px 10px;border-radius:99px;background:var(--panel2);border:1px solid var(--line)}}
.status.fresh{{color:var(--soft)}}.status.aging{{color:var(--accent)}}.status.stale{{color:var(--dim)}}
.summary-grid{{display:grid;grid-template-columns:1fr 1fr 2fr;border-block:1px solid var(--line)}}
article{{min-height:150px;padding:22px 26px;border-right:1px solid var(--line)}}article:last-child{{border:0}}
article>span{{color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.08em}}
article strong{{display:block;font-size:2.3rem;margin-top:12px;letter-spacing:-.04em}}article small{{font-size:.9rem;color:var(--muted)}}
.hero-price{{background:var(--panel2)}}.hero-price strong{{color:var(--accent)}}.hero-station{{display:flex;align-items:center;gap:7px}}
.chart{{display:block;width:100%;height:90px;margin-top:8px;cursor:crosshair}}
.chart polyline{{fill:none;stroke:var(--accent);stroke-width:4;stroke-linejoin:round;stroke-linecap:round}}
.chart-point{{fill:var(--panel);stroke:var(--accent);stroke-width:3;pointer-events:none;transition:r .15s ease,fill .15s ease}}
.chart-point.active,.chart-point:focus{{r:7;fill:var(--accent);outline:none}}
.chart text{{fill:var(--muted);font-size:12px}}.empty-chart{{color:var(--muted);padding-top:35px}}
#chart-tooltip{{position:fixed;z-index:20;pointer-events:none;opacity:0;transform:translate(-50%,-115%);padding:7px 10px;
border-radius:8px;background:var(--soft);color:#171512;font-size:.78rem;font-weight:800;box-shadow:0 8px 30px #0009;transition:opacity .12s ease;white-space:nowrap}}
#chart-tooltip.visible{{opacity:1}}
.settings{{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:22px 26px;margin:0}}
.settings h2{{margin-bottom:14px}}.settings-form{{display:grid;grid-template-columns:1fr 1fr .7fr auto;gap:12px;align-items:end}}
label{{display:grid;gap:6px;color:var(--muted);font-size:.8rem}}input,select,button{{font:inherit;border-radius:9px;border:1px solid var(--line);padding:10px 12px}}
input,select{{background:#10100f;color:var(--ink);min-width:0}}button{{background:var(--accent);color:#171512;font-weight:800;cursor:pointer}}
button:hover{{background:var(--soft)}}.settings>p{{color:var(--muted);font-size:.8rem;margin:12px 0 0}}
.excluded-list{{margin-top:18px;padding-top:16px;border-top:1px solid var(--line)}}.excluded-list h3{{font-size:.9rem;margin:0 0 10px}}
.excluded-station{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 0;border-top:1px solid #292824}}
.excluded-station:first-of-type{{border-top:0}}.excluded-station span{{min-width:0}}.excluded-station strong,.excluded-station small{{display:block}}
.excluded-station small{{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.excluded-station button{{padding:7px 10px;background:transparent;color:var(--accent)}}
.notice{{background:var(--panel2);color:var(--soft);border:1px solid var(--accent);padding:10px 14px;border-radius:9px;margin-bottom:14px}}
.history-stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:8px}}.history-stats div{{background:#111110;padding:8px;border-radius:8px}}
.kpi-grid{{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;background:var(--line);border-bottom:1px solid var(--line)}}.kpi-grid div{{background:var(--panel);padding:13px 16px}}.kpi-grid span{{display:block;color:var(--muted);font-size:.66rem;text-transform:uppercase}}.kpi-grid strong{{font-size:.94rem}}
.history-stats span{{display:block;color:var(--muted);font-size:.65rem;text-transform:uppercase}}.history-stats strong{{font-size:.9rem;margin:2px 0 0;letter-spacing:0}}
.station-list-head,.station-card summary{{display:grid;grid-template-columns:minmax(240px,1fr) 120px 100px 115px 82px 18px;align-items:center;gap:14px;padding:13px 18px 13px 26px}}
.station-list-head{{color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;border-bottom:1px solid var(--line)}}
.station-card-form{{display:block}}.station-card{{border-bottom:1px solid var(--line)}}.station-card:last-child{{border-bottom:0}}
.station-card.is-favorite{{box-shadow:inset 3px 0 var(--accent)}}
.station-card summary{{cursor:pointer;list-style:none;transition:background .18s ease}}.station-card summary::-webkit-details-marker{{display:none}}
.station-card summary:hover,.station-card[open] summary{{background:var(--panel2)}}
.station-card summary::after{{content:'+';color:var(--accent);font-size:1.25rem;text-align:center}}
.station-card[open] summary::after{{content:'−'}}
.station-card summary>span{{color:var(--muted);font-size:.88rem}}.station-name{{display:flex;align-items:center;gap:10px;min-width:0}}
.station-copy{{min-width:0}}.station-name strong{{display:block;color:var(--ink);font-size:1rem}}
.station-address{{display:block;color:var(--muted);font-size:.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-decoration:none}}
.station-address:hover,.station-address:focus{{color:var(--accent);text-decoration:underline}}
.brand-logo{{display:inline-grid;place-items:center;flex:0 0 42px;height:30px;border-radius:7px;border:1px solid #ffffff2a;
font-size:.62rem;font-weight:950;letter-spacing:-.04em;line-height:1;text-transform:none;box-shadow:inset 0 0 0 1px #0002}}
.brand-costco{{color:#e51b23;background:#fff;text-decoration:underline;text-decoration-color:#1869a7;text-decoration-thickness:2px}}
.brand-irving{{color:#fff;background:#168244}}.brand-petro-canada{{color:#fff;background:#d91e2b}}
.brand-esso{{color:#114b9b;background:#fff;border-color:#e32636;border-radius:50%}}
.brand-shell{{color:#d71920;background:#ffd62c}}.brand-ultramar{{color:#fff;background:#13489d}}
.brand-canadian-tire{{color:#fff;background:#d71920}}.brand-couche-tard{{color:#fff;background:#e1262f}}
.brand-generic{{color:var(--soft);background:#34322d}}
.station-price{{display:flex;align-items:center;gap:7px;color:var(--accent)!important;font-size:1.2rem!important;font-weight:800}}
.station-price small{{font-size:.75rem}}.price-direction{{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:7px;
font-size:.95rem!important;font-weight:950;vertical-align:.15em;letter-spacing:0}}
.price-direction.up{{color:var(--red);background:#ef7d7218}}.price-direction.down{{color:var(--green);background:#71d99b18}}
.price-direction.stable{{color:var(--blue);background:#70b8d718}}.price-direction.unknown{{color:var(--dim);background:#ffffff0a}}
.station-actions{{display:flex;gap:5px}}.icon-button{{display:grid;place-items:center;width:36px;height:34px;padding:0;background:transparent;color:var(--muted);font-size:1.15rem}}
.icon-button:hover,.icon-button:focus{{background:var(--panel);color:var(--accent)}}.favorite-button.active{{color:var(--accent);background:#2a2518}}
.exclude-button:hover,.exclude-button:focus{{color:var(--red);border-color:var(--red)}}
.station-detail{{display:grid;grid-template-columns:1fr 2fr;gap:16px 28px;padding:20px 26px 26px;background:#111110;border-top:1px solid var(--line)}}
.station-detail .chart{{height:118px;margin:0}}.station-stats{{grid-column:1/-1;display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
.station-stats div{{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:var(--panel)}}
.station-stats span{{display:block;color:var(--muted);font-size:.68rem;text-transform:uppercase}}.station-stats strong{{font-size:1rem}}
.more-stations>summary{{cursor:pointer;list-style:none;text-align:center;padding:15px;color:var(--accent);font-weight:800;background:#121210}}
.more-stations>summary::-webkit-details-marker{{display:none}}.more-stations>summary::after{{content:' ↓'}}.more-stations[open]>summary::after{{content:' ↑'}}
.empty{{text-align:center;padding:80px 24px;background:var(--panel);border:1px solid var(--line);border-radius:18px}}
.pump{{font-size:3rem}}footer{{display:flex;justify-content:space-between;gap:20px;color:var(--muted);font-size:.8rem;margin-top:24px}}
@media(max-width:760px){{.shell{{width:min(100% - 20px,1180px);padding-top:22px}}header{{align-items:start}}.header-tools>p{{display:none}}
.settings-menu>.settings{{width:calc(100vw - 20px);right:-1px}}.advice-summary{{grid-template-columns:auto minmax(0,1fr) auto;padding:12px}}
.forecast-compact{{grid-column:2/-1;grid-row:2}}.advice-badge{{display:none}}.advice-summary::after{{grid-column:3;grid-row:1}}
.advice-metrics{{grid-template-columns:1fr 1fr;gap:14px 0}}.advice-metrics div:nth-child(2){{border:0}}.advice-metrics div:nth-child(3){{padding-left:0}}
.settings{{padding:18px}}.settings-form{{grid-template-columns:1fr 1fr}}.settings-form button{{grid-column:1/-1}}
.summary-grid{{grid-template-columns:1fr 1fr}}article{{padding:18px;min-height:130px}}article.trend{{grid-column:1/-1;border-top:1px solid var(--line)}}
.kpi-grid{{grid-template-columns:1fr 1fr 1fr}}
.station-list-head{{display:none}}.station-card summary{{grid-template-columns:minmax(0,1fr) auto auto 16px;padding:13px 12px 13px 18px;gap:8px}}
.station-card summary>span:nth-child(3),.station-card summary>span:nth-child(4){{display:none}}
.station-price{{padding-right:3px}}.station-actions{{gap:3px}}.icon-button{{width:32px;height:32px}}.station-detail{{grid-template-columns:1fr;padding:18px}}.station-stats{{grid-template-columns:1fr 1fr}}
footer{{display:block}}}}
</style></head><body><main class="shell"><header><div><h1>Gas<b>Watch</b></h1>
<p>Prix recents autour de vos emplacements</p></div><div class="header-tools">
<p>Actualisation automatique<br>toutes les 60 secondes</p><details class="settings-menu" id="settings">
<summary aria-label="Ouvrir mes réglages" title="Mes réglages">⚙</summary>
<section class="settings"><h2>Mes réglages</h2>
<form method="post" action="/settings" class="settings-form">
<input type="hidden" name="csrf" value="{{csrf_token}}">
<label>Latitude<input name="latitude" inputmode="decimal" required value="{html.escape(latitude)}"></label>
<label>Longitude<input name="longitude" inputmode="decimal" required value="{html.escape(longitude)}"></label>
<label>Rayon (km)<input name="radius" inputmode="decimal" required value="{html.escape(radius)}"></label>
<button type="submit">Enregistrer</button></form>
<p>Utilisez l'etoile a cote d'une station pour la garder en haut. La position est envoyee uniquement a Gas Quebec pour la recherche.</p>
<div class="settings-summary"><p><strong>Collectes</strong><br>Toutes les {settings.price_check_interval_minutes} minutes · conservation horaire continue</p><p><strong>Véhicules</strong><br>{html.escape(vehicle_summary)}</p><p><strong>Notifications</strong><br>{"ntfy activé" if settings.ntfy_enabled else "ntfy désactivé"} · rapport quotidien {"activé" if settings.daily_report_enabled else "désactivé"}</p></div>
{f'<div class="excluded-list"><h3>Stations exclues</h3>{excluded_controls}</div>' if excluded_controls else ""}</section></details></div></header>
<nav class="main-nav" aria-label="Navigation principale"><a class="active" href="/">Accueil</a><a href="/#stations">Stations</a><a href="/fillups">Pleins</a><a href="/analytics">Analytics</a><a href="/#settings">Paramètres</a></nav>
{"".join(advice_banners)}
<div id="stations">{"".join(sections)}</div>
<footer><span>* Distance geographique, pas routiere.</span><span>Page generee {generated}</span></footer>
</main><div id="chart-tooltip" role="tooltip"></div><script>
for (const detail of document.querySelectorAll('[data-station]')) {{
  if (location.hash === `#${{detail.id}}`) detail.open = true;
  detail.addEventListener('toggle', () => {{
    if (detail.open) history.replaceState(null, '', `#${{detail.id}}`);
  }});
}}
for (const button of document.querySelectorAll('.station-actions button')) {{
  button.addEventListener('click', event => event.stopPropagation());
}}
for (const link of document.querySelectorAll('.station-address')) {{
  link.addEventListener('click', event => event.stopPropagation());
}}
const chartTooltip = document.querySelector('#chart-tooltip');
function showChartTooltip(point) {{
  const box = point.getBoundingClientRect();
  chartTooltip.textContent = point.dataset.tooltip;
  chartTooltip.style.left = `${{box.left + box.width / 2}}px`;
  chartTooltip.style.top = `${{box.top}}px`;
  chartTooltip.classList.add('visible');
}}
function hideChartTooltip(chart) {{
  chartTooltip.classList.remove('visible');
  chart.querySelector('.chart-point.active')?.classList.remove('active');
}}
for (const chart of document.querySelectorAll('[data-chart]')) {{
  const points = [...chart.querySelectorAll('.chart-point')];
  chart.addEventListener('pointermove', event => {{
    const nearest = points.reduce((best, point) => {{
      const pointX = point.getBoundingClientRect().left;
      const bestX = best.getBoundingClientRect().left;
      return Math.abs(pointX - event.clientX) < Math.abs(bestX - event.clientX) ? point : best;
    }});
    chart.querySelector('.chart-point.active')?.classList.remove('active');
    nearest.classList.add('active');
    showChartTooltip(nearest);
  }});
  chart.addEventListener('pointerleave', () => hideChartTooltip(chart));
}}
for (const point of document.querySelectorAll('.chart-point')) {{
  point.addEventListener('focus', () => showChartTooltip(point));
  point.addEventListener('blur', () => chartTooltip.classList.remove('visible'));
}}
</script></body></html>"""


def _render_fillups_legacy(repository: Repository, settings: Settings) -> str:
    snapshot = repository.dashboard_snapshot(settings.max_price_age_minutes)
    market_average = (
        statistics.fmean(float(row["price_cents"]) for row in snapshot) if snapshot else None
    )
    today = datetime.now(ZoneInfo(settings.tz)).date()
    month = repository.fillup_statistics(since_date=today.replace(day=1))
    year = repository.fillup_statistics(since_date=today.replace(month=1, day=1))
    lifetime = repository.fillup_statistics(None)
    rows = repository.fillups(None)

    def amount(value: float | None, suffix: str) -> str:
        return "—" if value is None else f"{value:.1f}{suffix}"

    vehicle_options = "".join(
        f'<option value="{html.escape(vehicle.key)}">{html.escape(vehicle.name)}</option>'
        for vehicle in settings.configured_vehicles
    )
    station_options = "".join(
        f'<option value="{html.escape(str(row["station_id"]))}" data-name="{html.escape(str(row["name"]), quote=True)}" data-price="{float(row["price_cents"]):.1f}">{html.escape(str(row["name"]))} — {float(row["price_cents"]):.1f} c/L</option>'
        for row in snapshot
    )
    history_rows = (
        "".join(
            f"<tr><td>{html.escape(str(row['filled_at']))}</td><td>{html.escape(str(row['vehicle_name']))}</td>"
            f"<td>{html.escape(str(row['station_name']))}</td><td>{float(row['price_cents']):.1f} c/L</td>"
            f"<td>{float(row['liters']):.1f} L</td><td>{float(row['total_cad']):.2f} $</td>"
            f"<td>{float(row['savings_cad']):.2f} $</td></tr>"
            for row in rows
        )
        or '<tr><td colspan="7">Aucun plein enregistré.</td></tr>'
    )
    notice = ""
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Journal des pleins · GasWatch</title>
    <style>:root{{--ink:#f4f1e8;--muted:#aaa69d;--panel:#171715;--panel2:#201f1c;--line:#393732;--accent:#e6c77a;--black:#0e0e0d;--green:#71d99b}}*{{box-sizing:border-box}}body{{margin:0;background:var(--black);color:var(--ink);font:16px/1.5 system-ui,sans-serif}}.shell{{width:min(1100px,calc(100% - 28px));margin:auto;padding:32px 0 60px}}h1{{font-size:clamp(2rem,5vw,4rem);margin:0}}h1 b,.eyebrow{{color:var(--accent)}}nav{{display:flex;gap:6px;flex-wrap:wrap;margin:18px 0}}a{{color:var(--muted);padding:8px 12px;text-decoration:none;border-radius:9px}}a.active,a:hover{{background:var(--accent);color:var(--black)}}.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:20px 0}}.stats div,section{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}}.stats span,label{{display:block;color:var(--muted);font-size:.8rem}}.stats strong{{font-size:1.3rem}}form{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}input,select,button{{width:100%;padding:11px;border:1px solid var(--line);border-radius:8px;background:var(--panel2);color:var(--ink)}}button{{background:var(--accent);color:var(--black);font-weight:800;cursor:pointer}}table{{width:100%;border-collapse:collapse;margin-top:12px}}th,td{{padding:10px;border-top:1px solid var(--line);text-align:left}}.table{{overflow:auto}}@media(max-width:720px){{.stats{{grid-template-columns:1fr 1fr}}form{{grid-template-columns:1fr}}}}</style></head>
    <body><main class="shell"><header><h1>Gas<b>Watch</b></h1><p>Journal des pleins</p></header><nav><a href="/">Accueil</a><a class="active" href="/fillups">Pleins</a><a href="/analytics">Analytics</a></nav>{notice}
    <div class="stats"><div><span>Essence ce mois-ci</span><strong>{amount(month["liters"], " L")}</strong></div><div><span>Dépenses ce mois-ci</span><strong>{month["spending_cad"]:.2f} $</strong></div><div><span>Prix moyen payé</span><strong>{amount(month["average_price_cents"], " c/L")}</strong></div><div><span>Consommation réelle</span><strong>{amount(lifetime["consumption_l_per_100km"], " L/100 km")}</strong></div><div><span>Coût moyen / 100 km</span><strong>{amount(lifetime["cost_per_100km"], " $")}</strong></div><div><span>Économies ce mois-ci</span><strong>{month["savings_cad"]:.2f} $</strong></div><div><span>Économies cette année</span><strong>{year["savings_cad"]:.2f} $</strong></div><div><span>Depuis l’installation</span><strong>{lifetime["savings_cad"]:.2f} $</strong></div></div>
    <section><h2>Enregistrer un plein</h2><form method="post" action="/fillups"><input type="hidden" name="csrf" value="{{csrf_token}}"><label>Date<input type="date" name="filled_at" required value="{datetime.now(ZoneInfo(settings.tz)).date().isoformat()}"></label><label>Véhicule<select name="vehicle_key" required>{vehicle_options}</select></label><label>Station<select name="station_id" id="station" required>{station_options}</select></label><label>Prix par litre (c/L)<input name="price_cents" id="price" type="number" min="1" step="0.1" required></label><label>Litres<input name="liters" type="number" min="0.1" step="0.1" required></label><label>Odomètre (km, optionnel)<input name="odometer_km" type="number" min="0" step="1"></label><button type="submit">Enregistrer le plein</button></form><p>Le prix moyen du secteur ({f"{market_average:.1f} c/L" if market_average is not None else "en attente de collecte"}) est enregistré automatiquement pour mesurer l’économie.</p></section>
    <section class="table"><h2>Historique</h2><table><thead><tr><th>Date</th><th>Véhicule</th><th>Station</th><th>Prix</th><th>Litres</th><th>Total</th><th>Économie</th></tr></thead><tbody>{history_rows}</tbody></table></section></main><script>const station=document.querySelector('#station'),price=document.querySelector('#price');function updatePrice(){{price.value=station.selectedOptions[0]?.dataset.price||''}}station?.addEventListener('change',updatePrice);updatePrice();</script></body></html>"""


def render_fillups(
    repository: Repository,
    settings: Settings,
    *,
    vehicle_filter: str = "",
    from_date: date | None = None,
    until_date: date | None = None,
) -> str:
    snapshot = repository.dashboard_snapshot(settings.max_price_age_minutes)
    today = datetime.now(ZoneInfo(settings.tz)).date()
    market_average = (
        statistics.fmean(float(row["price_cents"]) for row in snapshot) if snapshot else None
    )
    month = repository.fillup_statistics(since_date=today.replace(day=1))
    year = repository.fillup_statistics(since_date=today.replace(month=1, day=1))
    lifetime = repository.fillup_statistics()
    rows = repository.fillups(
        since_date=from_date, until_date=until_date, vehicle_key=vehicle_filter or None
    )

    def amount(value: float | None, suffix: str) -> str:
        return "—" if value is None else f"{value:.1f}{suffix}"

    vehicle_options = "".join(
        f'<option value="{html.escape(vehicle.key)}">{html.escape(vehicle.name)}</option>'
        for vehicle in settings.configured_vehicles
    )
    filter_vehicle_options = '<option value="">Tous les véhicules</option>' + "".join(
        f'<option value="{html.escape(vehicle.key)}" {"selected" if vehicle.key == vehicle_filter else ""}>{html.escape(vehicle.name)}</option>'
        for vehicle in settings.configured_vehicles
    )
    station_options = "".join(
        f'<option value="{html.escape(str(row["station_id"]))}" data-price="{float(row["price_cents"]):.1f}">{html.escape(str(row["name"]))} — {float(row["price_cents"]):.1f} c/L</option>'
        for row in snapshot
    )
    history_parts = []
    for row in rows:
        edit_vehicle_options = "".join(
            f'<option value="{html.escape(vehicle.key)}" {"selected" if vehicle.key == row["vehicle_key"] else ""}>{html.escape(vehicle.name)}</option>'
            for vehicle in settings.configured_vehicles
        )
        receipt = (
            f'<a href="/fillups/receipt?id={int(row["id"])}">Reçu</a>'
            if row["has_receipt"]
            else "—"
        )
        history_parts.append(
            f"<tr><td>{html.escape(str(row['filled_at']))}</td><td>{html.escape(str(row['vehicle_name']))}</td>"
            f"<td>{html.escape(str(row['station_name']))}</td><td>{float(row['price_cents']):.1f} c/L</td>"
            f"<td>{float(row['liters']):.1f} L</td><td>{float(row['total_cad']):.2f} $</td>"
            f"<td>{float(row['savings_cad']):.2f} $</td><td>{receipt}</td><td><details><summary>Modifier</summary>"
            f'<form method="post" action="/fillups/update" class="edit-form"><input type="hidden" name="csrf" value="{{csrf_token}}"><input type="hidden" name="fillup_id" value="{int(row["id"])}">'
            f'<input type="date" name="filled_at" required value="{html.escape(str(row["filled_at"]))}"><select name="vehicle_key">{edit_vehicle_options}</select>'
            f'<input name="station_name" required value="{html.escape(str(row["station_name"]), quote=True)}"><input type="number" name="price_cents" min="1" step="0.1" required value="{float(row["price_cents"]):.1f}">'
            f'<input type="number" name="liters" min="0.1" step="0.1" required value="{float(row["liters"]):.1f}"><input type="number" name="odometer_km" min="0" step="1" value="{row["odometer_km"] or ""}">'
            f'<input name="note" maxlength="500" placeholder="Note" value="{html.escape(str(row["note"] or ""), quote=True)}"><label><input type="checkbox" name="is_full_tank" value="1" {"checked" if row["is_full_tank"] else ""}> Réservoir rempli</label><button type="submit">Enregistrer</button></form>'
            f'<form method="post" action="/fillups/delete" onsubmit="return confirm(\'Supprimer ce plein?\')"><input type="hidden" name="csrf" value="{{csrf_token}}"><input type="hidden" name="fillup_id" value="{int(row["id"])}"><button class="danger" type="submit">Supprimer</button></form></details></td></tr>'
        )
    history_rows = "".join(history_parts) or '<tr><td colspan="9">Aucun plein enregistré.</td></tr>'
    comparison_rows = "".join(
        _vehicle_comparison_row(repository, vehicle, today)
        for vehicle in settings.configured_vehicles
    )
    filter_query = urlencode(
        {
            "vehicle": vehicle_filter,
            "from": from_date.isoformat() if from_date else "",
            "to": until_date.isoformat() if until_date else "",
        }
    )
    average_label = f"{market_average:.1f} c/L" if market_average is not None else "en attente"
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Journal des pleins · GasWatch</title>
    <style>:root{{--ink:#f4f1e8;--muted:#aaa69d;--panel:#171715;--panel2:#201f1c;--line:#393732;--accent:#e6c77a;--black:#0e0e0d;--green:#71d99b;--red:#ef7d72}}*{{box-sizing:border-box}}body{{margin:0;background:var(--black);color:var(--ink);font:16px/1.5 system-ui,sans-serif}}.shell{{width:min(1180px,calc(100% - 28px));margin:auto;padding:32px 0 60px}}h1{{font-size:clamp(2rem,5vw,4rem);margin:0}}h1 b{{color:var(--accent)}}nav{{display:flex;gap:6px;flex-wrap:wrap;margin:18px 0}}a{{color:var(--muted);padding:8px 12px;text-decoration:none;border-radius:9px}}a.active,a:hover{{background:var(--accent);color:var(--black)}}.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:20px 0}}.stats div,section{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}}.stats span,label{{display:block;color:var(--muted);font-size:.8rem}}.stats strong{{font-size:1.3rem}}form{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.edit-form{{grid-template-columns:1fr;min-width:260px}}input,select,button{{width:100%;padding:11px;border:1px solid var(--line);border-radius:8px;background:var(--panel2);color:var(--ink)}}input[type=checkbox]{{width:auto}}button{{background:var(--accent);color:var(--black);font-weight:800;cursor:pointer}}button.danger{{background:var(--red);margin-top:6px}}table{{width:100%;border-collapse:collapse;margin-top:12px}}th,td{{padding:10px;border-top:1px solid var(--line);text-align:left;white-space:nowrap}}.table{{overflow:auto}}.filters{{display:flex;align-items:end;gap:8px;margin-bottom:14px}}.filters>*{{flex:1}}@media(max-width:720px){{.stats{{grid-template-columns:1fr 1fr}}form,.filters{{display:grid;grid-template-columns:1fr}}}}</style></head>
    <body><main class="shell"><header><h1>Gas<b>Watch</b></h1><p>Journal des pleins</p></header><nav><a href="/">Accueil</a><a class="active" href="/fillups">Pleins</a><a href="/analytics">Analytics</a></nav>
    <div class="stats"><div><span>Essence ce mois-ci</span><strong>{amount(month["liters"], " L")}</strong></div><div><span>Dépenses ce mois-ci</span><strong>{month["spending_cad"]:.2f} $</strong></div><div><span>Prix moyen payé</span><strong>{amount(month["average_price_cents"], " c/L")}</strong></div><div><span>Consommation réelle</span><strong>{amount(lifetime["consumption_l_per_100km"], " L/100 km")}</strong></div><div><span>Coût moyen / 100 km</span><strong>{amount(lifetime["cost_per_100km"], " $")}</strong></div><div><span>Économies ce mois-ci</span><strong>{month["savings_cad"]:.2f} $</strong></div><div><span>Économies cette année</span><strong>{year["savings_cad"]:.2f} $</strong></div><div><span>Depuis l’installation</span><strong>{lifetime["savings_cad"]:.2f} $</strong></div></div>
    <section><h2>Enregistrer un plein</h2><form method="post" action="/fillups" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{{csrf_token}}"><label>Date<input type="date" name="filled_at" required value="{today.isoformat()}"></label><label>Véhicule<select name="vehicle_key" required>{vehicle_options}</select></label><label>Station<select name="station_id" id="station" required>{station_options}</select></label><label>Prix (c/L)<input name="price_cents" id="price" type="number" min="1" step="0.1" required></label><label>Litres<input name="liters" type="number" min="0.1" step="0.1" required></label><label>Odomètre (optionnel)<input name="odometer_km" type="number" min="0" step="1"></label><label>Note<input name="note" maxlength="500"></label><label>Reçu (JPG, PNG ou PDF, 5 Mo max.)<input name="receipt" type="file" accept="image/jpeg,image/png,application/pdf"></label><label><input type="checkbox" name="is_full_tank" value="1" checked> Réservoir rempli complètement</label><button type="submit">Enregistrer le plein</button></form><p>Prix moyen du secteur enregistré automatiquement : {average_label}.</p></section>
    <section><h2>Coût mensuel et consommation</h2><div class="table"><table><thead><tr><th>Véhicule</th><th>Coût ce mois</th><th>Réelle</th><th>Théorique</th><th>Écart</th></tr></thead><tbody>{comparison_rows}</tbody></table></div></section>
    <section class="table"><h2>Historique</h2><form method="get" action="/fillups" class="filters"><label>Véhicule<select name="vehicle">{filter_vehicle_options}</select></label><label>Du<input type="date" name="from" value="{from_date.isoformat() if from_date else ""}"></label><label>Au<input type="date" name="to" value="{until_date.isoformat() if until_date else ""}"></label><button type="submit">Filtrer</button><a href="/fillups/export.csv?{filter_query}">Exporter CSV</a></form><table><thead><tr><th>Date</th><th>Véhicule</th><th>Station</th><th>Prix</th><th>Litres</th><th>Total</th><th>Économie</th><th>Pièce</th><th>Actions</th></tr></thead><tbody>{history_rows}</tbody></table></section></main><script>const station=document.querySelector('#station'),price=document.querySelector('#price');function updatePrice(){{price.value=station.selectedOptions[0]?.dataset.price||''}}station?.addEventListener('change',updatePrice);updatePrice();</script></body></html>"""


def _vehicle_comparison_row(repository: Repository, vehicle: Vehicle, today: date) -> str:
    actual = repository.fillup_statistics(since_date=today.replace(day=1), vehicle_key=vehicle.key)
    real = actual["consumption_l_per_100km"]
    real_label = f"{real:.1f} L/100 km" if real is not None else "—"
    delta = f"{real - vehicle.consumption_l_per_100km:+.1f} L/100 km" if real is not None else "—"
    return f"<tr><td>{html.escape(vehicle.name)}</td><td>{actual['spending_cad']:.2f} $</td><td>{real_label}</td><td>{vehicle.consumption_l_per_100km:.1f} L/100 km</td><td>{delta}</td></tr>"


def _multi_series_chart(rows: list[dict[str, object]], series: tuple[tuple[str, str], ...]) -> str:
    if not rows:
        return '<div class="empty-chart">Historique en construction</div>'
    width, height, padding = 900, 250, 24
    values = [float(row[key]) for row in rows for key, _ in series if row.get(key) is not None]
    low, high = min(values), max(values)
    spread = max(high - low, 1)
    paths: list[str] = []
    points: list[str] = []
    for key, label in series:
        coords = []
        for index, row in enumerate(rows):
            if row.get(key) is None:
                continue
            value = float(row[key])
            x = padding + index * (width - 2 * padding) / max(len(rows) - 1, 1)
            y = height - padding - (value - low) * (height - 2 * padding) / spread
            coords.append(f"{x:.1f},{y:.1f}")
            tooltip = html.escape(
                f"{row.get('day', row.get('label', ''))} · {label}: {value:.1f} c/L", quote=True
            )
            points.append(
                f'<circle class="series-point series-{key}" cx="{x:.1f}" cy="{y:.1f}" r="4" '
                f'data-series="{key}" data-tooltip="{tooltip}"><title>{tooltip}</title></circle>'
            )
        paths.append(
            f'<polyline class="series-line series-{key}" data-series="{key}" points="{" ".join(coords)}" />'
        )
    toggles = "".join(
        f'<label><input type="checkbox" data-toggle-series="{key}" checked> {html.escape(label)}</label>'
        for key, label in series
    )
    return (
        f'<div class="series-toggles">{toggles}</div><svg class="analysis-chart" '
        f'viewBox="0 0 {width} {height}" role="img" data-chart>'
        f"{''.join(paths)}{''.join(points)}</svg>"
    )


def render_statistics(repository: Repository, settings: Settings, days: int | None = 30) -> str:
    snapshot = repository.dashboard_snapshot()
    groups = sorted({(str(row["location_key"]), str(row["fuel_type"])) for row in snapshot})
    history = repository.dashboard_history(days)
    station_history = repository.dashboard_station_history(days or 36500)
    period_options = (
        (1, "24 h"),
        (7, "7 j"),
        (30, "30 j"),
        (90, "90 j"),
        (183, "6 mois"),
        (365, "1 an"),
        (None, "Tout l’historique"),
    )
    period_links = "".join(
        f'<a class="{"active" if value == days else ""}" href="/analytics?period={"all" if value is None else value}">{label}</a>'
        for value, label in period_options
    )
    sections: list[str] = []
    for location_key, fuel_type in groups:
        rows = [
            row
            for row in history
            if row["location_key"] == location_key and row["fuel_type"] == fuel_type
        ]
        stats = repository.advanced_statistics(location_key, fuel_type, days)
        if not stats:
            continue
        changes = []
        for previous, current in zip(rows, rows[1:], strict=False):
            changes.append(
                {
                    "day": current["day"],
                    "change": float(current["average"]) - float(previous["average"]),
                }
            )
        temporal = repository.temporal_patterns(location_key, fuel_type, days or 36500, settings.tz)
        prediction = repository.prediction_accuracy(location_key, fuel_type)
        prediction_rows = repository.prediction_history(location_key, fuel_type)
        prediction_chart_rows = [
            {
                "day": str(row["created_at"]),
                "prediction": row["predicted_price_cents"],
                "actual": row["actual_price_cents"],
            }
            for row in prediction_rows
        ]
        station_rows = [
            row
            for row in station_history
            if row["location_key"] == location_key and row["fuel_type"] == fuel_type
        ]
        station_names = {str(row["station_id"]): str(row["name"]) for row in station_rows}
        cheapest = stats["cheapest_percentages"]
        market_by_day = {str(row["day"]): float(row["average"]) for row in rows}
        station_metrics = []
        for station_id, station_name in station_names.items():
            values = [
                float(row["price_cents"])
                for row in station_rows
                if str(row["station_id"]) == station_id
            ]
            gaps = [
                float(row["price_cents"]) - market_by_day[str(row["day"])]
                for row in station_rows
                if str(row["station_id"]) == station_id and str(row["day"]) in market_by_day
            ]
            station_metrics.append((station_id, station_name, values, gaps))
        cheapest_rows = "".join(
            f"<tr><td>{html.escape(name)}</td><td>{cheapest.get(station_id, 0):.1f} %</td><td>{round(cheapest.get(station_id, 0) * len(rows) / 100):d}</td><td>{statistics.fmean(values):.1f} c/L</td><td>{min(values):.1f} c/L</td><td>{max(values):.1f} c/L</td><td>{statistics.fmean(gaps):+.1f} c/L</td></tr>"
            for station_id, name, values, gaps in sorted(
                station_metrics, key=lambda item: cheapest.get(item[0], 0), reverse=True
            )
            if values and gaps
        )
        daily_values = [float(row["average"]) for row in rows]
        current = daily_values[-1]

        def change_for(
            day_count: int,
            current: float = current,
            daily_values: list[float] = daily_values,
        ) -> float | None:
            return current - daily_values[-day_count - 1] if len(daily_values) > day_count else None

        changes_cards = (
            ("Variation 24 h", change_for(1)),
            ("Variation 7 jours", change_for(7)),
            ("Variation 30 jours", change_for(30)),
        )
        cards = (
            ("Minimum historique", stats["minimum"], " c/L"),
            ("Moyenne", stats["average"], " c/L"),
            ("Médiane", stats["median"], " c/L"),
            ("Maximum historique", stats["maximum"], " c/L"),
            ("Volatilité", stats["volatility"], " c/L"),
            ("Plus forte hausse", stats["largest_increase"], " c/L"),
            ("Plus forte baisse", stats["largest_decrease"], " c/L"),
            ("Position actuelle", stats["current_percentile"], "e percentile"),
        )
        cards_html = "".join(
            f"<div><span>{label}</span><strong>{float(value):.1f}{suffix}</strong></div>"
            for label, value, suffix in cards
        )
        cards_html += "".join(
            f"<div><span>{label}</span><strong>{value:+.1f} c/L</strong></div>"
            if value is not None
            else f"<div><span>{label}</span><strong>—</strong></div>"
            for label, value in changes_cards
        )
        change_interval = stats["average_change_interval_hours"]
        stability = stats["average_stability_hours"]
        cards_html += (
            "<div><span>Fréquence des changements</span><strong>"
            + (f"Tous les {float(change_interval):.1f} h" if change_interval is not None else "—")
            + "</strong></div><div><span>Stabilité moyenne</span><strong>"
            + (f"{float(stability):.1f} h" if stability is not None else "—")
            + "</strong></div>"
        )
        station_charts = "".join(
            f"<details><summary>{html.escape(station_names.get(station_id, station_id))}</summary>{_sparkline([float(row['price_cents']) for row in station_rows if str(row['station_id']) == station_id], 'Historique station', [str(row['day']) for row in station_rows if str(row['station_id']) == station_id])}</details>"
            for station_id in sorted(station_names)
        )
        accuracy = (
            f"{prediction['mean_absolute_error']:.1f} c/L d’erreur moyenne · "
            f"{prediction['within_two_cents_percent']:.0f} % à ±2 c/L · "
            f"{prediction['evaluated_count']} prévisions évaluées"
            if prediction["mean_absolute_error"] is not None
            else "Pas encore assez de prévisions arrivées à échéance."
        )
        bins = (
            (float("-inf"), 150, "&lt; 150"),
            (150, 155, "150–155"),
            (155, 160, "155–160"),
            (160, 165, "160–165"),
            (165, float("inf"), "&gt; 165"),
        )
        distribution = "".join(
            f'<div class="distribution-row"><span>{label} c/L</span><i style="width:{100 * sum(low <= value < high for value in daily_values) / len(daily_values):.1f}%"></i><strong>{100 * sum(low <= value < high for value in daily_values) / len(daily_values):.0f} %</strong></div>'
            for low, high, label in bins
        )
        low, high = (
            min(float(row["minimum"]) for row in rows),
            max(float(row["minimum"]) for row in rows),
        )
        spread = max(high - low, 0.1)
        calendar = "".join(
            f'<span class="heat-cell" style="--heat:{(float(row["minimum"]) - low) / spread:.2f}" title="{html.escape(str(row["day"]))} · {float(row["minimum"]):.1f} c/L"><b>{str(row["day"])[-2:]}</b><small>{float(row["minimum"]):.1f}</small></span>'
            for row in rows
        )
        target = (
            settings.manual_target_price_cents
            if settings.target_price_mode == "MANUAL"
            else percentile(
                [float(row["minimum"]) for row in rows], settings.target_price_percentile
            )
        )
        increases = [
            (daily_values[index] - daily_values[index - 1], rows[index]["day"])
            for index in range(1, len(rows))
        ]
        records = (
            ("Plus bas prix enregistré", f"{low:.1f} c/L"),
            ("Plus haut prix enregistré", f"{high:.1f} c/L"),
            (
                "Plus forte hausse en 24 h",
                f"{max((value for value, _ in increases), default=0):+.1f} c/L",
            ),
            (
                "Plus forte baisse en 24 h",
                f"{min((value for value, _ in increases), default=0):+.1f} c/L",
            ),
            (
                "Jours sous la cible personnelle",
                str(sum(float(row["minimum"]) <= target for row in rows)),
            ),
            (
                "Jours au-dessus de la moyenne",
                str(sum(value > statistics.fmean(daily_values) for value in daily_values)),
            ),
        )
        records_html = "".join(
            f"<div><span>{label}</span><strong>{value}</strong></div>" for label, value in records
        )
        percentile_explanation = max(0, min(100, 100 - float(stats["current_percentile"])))
        sections.append(f"""
        <section class="analysis-section"><p class="eyebrow">{html.escape(fuel_type)}</p><h2>{html.escape(location_key)}</h2>
          <div class="stat-grid">{cards_html}</div>
          <p class="percentile-copy">Le prix actuel est inférieur à {percentile_explanation:.0f} % des journées de la période.</p>
          <div class="analysis-grid">
            <article><h3>Minimum, moyenne et maximum</h3>{_multi_series_chart(rows, (("minimum", "Minimum"), ("average", "Moyenne"), ("maximum", "Maximum")))}</article>
            <article><h3>Variations quotidiennes</h3>{_multi_series_chart(changes, (("change", "Variation"),))}</article>
            <article><h3>Prix moyen selon le jour</h3>{_sparkline([float(row["average"]) for row in temporal["weekdays"]], "Prix par jour", [str(row["weekday"]) for row in temporal["weekdays"]])}</article>
            <article><h3>Prix moyen et fréquence des changements selon l’heure locale</h3>{_sparkline([float(row["average"]) for row in temporal["hours"]], "Prix par heure", [f"{int(row['hour']):02d}:00 · {float(row['change_frequency_percent']):.1f} % de changements" for row in temporal["hours"]])}</article>
            <article><h3>Comparaison des stations</h3><div class="station-comparisons">{station_charts or "Historique en construction"}</div></article>
            <article><h3>Prédictions comparées au réel</h3>{_multi_series_chart(prediction_chart_rows, (("prediction", "Prévision"), ("actual", "Prix réel")))}<p>{accuracy}</p><p>{len(prediction_rows)} prévisions conservées. Les estimations ne sont affichées que lorsque le signal est suffisamment fiable.</p></article>
          </div>
          <div class="analysis-grid"><article><h3>Distribution des prix</h3>{distribution}</article><article><h3>Records historiques</h3><div class="records">{records_html}</div></article></div>
          <article class="cheapest"><h3>Classement des stations</h3><div class="table-scroll"><table><thead><tr><th>Station</th><th>Jours moins chère</th><th>Nombre</th><th>Moyenne</th><th>Minimum</th><th>Maximum</th><th>Écart marché</th></tr></thead><tbody>{cheapest_rows}</tbody></table></div></article>
          <article class="cheapest"><h3>Calendrier des prix minimums</h3><p>Du vert (très bas) au rouge (très élevé).</p><div class="heatmap">{calendar}</div></article>
        </section>""")
    empty = '<section class="empty"><h2>Historique en construction</h2></section>'
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Statistiques · GasWatch</title>
    <style>@media(prefers-color-scheme:light){{:root{{--ink:#24211d!important;--muted:#6b665d!important;--panel:#fffdf7!important;--panel2:#f3eee2!important;--line:#d8d0bf!important;--accent:#8a6513!important;--black:#f6f2e8!important}}}}</style>
    <style>:root{{--ink:#f4f1e8;--muted:#aaa69d;--panel:#171715;--panel2:#201f1c;--line:#393732;--accent:#e6c77a;--black:#0e0e0d;--green:#71d99b;--red:#ef7d72;--blue:#70b8d7}}*{{box-sizing:border-box}}body{{margin:0;background:var(--black);color:var(--ink);font:16px/1.5 system-ui,sans-serif}}.shell{{width:min(1180px,calc(100% - 28px));margin:auto;padding:32px 0 60px}}header{{display:flex;align-items:end;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:18px}}h1{{margin:0;font-size:clamp(2rem,5vw,4rem)}}h1 b,.eyebrow{{color:var(--accent)}}nav,.periods,.series-toggles{{display:flex;gap:6px;flex-wrap:wrap}}nav{{margin:18px 0}}a{{color:var(--muted);text-decoration:none;padding:8px 12px;border-radius:9px}}a.active,a:hover{{background:var(--accent);color:var(--black)}}.periods{{margin-bottom:20px}}.periods a{{border:1px solid var(--line)}}.analysis-section,.analysis-grid article,.cheapest{{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:20px}}.stat-grid,.records{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:16px 0}}.stat-grid div,.records div{{background:var(--panel2);padding:14px;border-radius:10px}}.stat-grid span,.records span{{display:block;color:var(--muted);font-size:.7rem;text-transform:uppercase}}.stat-grid strong{{font-size:1.1rem}}.analysis-grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}.analysis-grid article{{margin:0;min-width:0}}.analysis-chart,.chart{{width:100%;height:auto;max-height:260px}}.series-line{{fill:none;stroke-width:3}}.series-minimum{{stroke:var(--green);fill:var(--green)}}.series-average,.series-prediction{{stroke:var(--accent);fill:var(--accent)}}.series-maximum{{stroke:var(--red);fill:var(--red)}}.series-change,.series-actual{{stroke:var(--blue);fill:var(--blue)}}.series-point{{stroke-width:1}}.series-toggles label{{color:var(--muted);font-size:.8rem}}.chart polyline{{fill:none;stroke:var(--accent);stroke-width:3}}.chart-point{{fill:var(--panel);stroke:var(--accent);stroke-width:3}}table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border-top:1px solid var(--line);text-align:left;white-space:nowrap}}.table-scroll{{overflow:auto}}details summary{{cursor:pointer;color:var(--accent);padding:6px}}p{{color:var(--muted)}}.distribution-row{{display:grid;grid-template-columns:90px 1fr 48px;gap:8px;align-items:center;margin:10px 0}}.distribution-row i{{height:12px;background:var(--accent);border-radius:8px;min-width:2px}}.heatmap{{display:grid;grid-template-columns:repeat(auto-fill,minmax(58px,1fr));gap:5px}}.heat-cell{{display:grid;place-items:center;aspect-ratio:1;border-radius:8px;background:color-mix(in srgb,var(--green) calc((1 - var(--heat))*100%),var(--red));color:#111}}.heat-cell small{{font-size:.65rem}}@media(max-width:760px){{.stat-grid,.records{{grid-template-columns:1fr 1fr}}.analysis-grid{{grid-template-columns:1fr}}}}</style></head>
    <body><main class="shell"><header><div><h1>Gas<b>Watch</b></h1><p>Analytics</p></div></header><nav><a href="/">Accueil</a><a href="/fillups">Pleins</a><a class="active" href="/analytics">Analytics</a></nav><div class="periods">{period_links}</div>{"".join(sections) or empty}</main><script>for(const input of document.querySelectorAll('[data-toggle-series]')){{input.addEventListener('change',()=>{{for(const node of document.querySelectorAll(`.series-${{input.dataset.toggleSeries}}`))node.style.display=input.checked?'':'none'}})}});</script></body></html>"""


class DashboardServer:
    def __init__(self, repository: Repository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings
        self._csrf_token = secrets.token_urlsafe(24)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/favicon.svg":
                    self._send(
                        HTTPStatus.OK,
                        "image/svg+xml",
                        (STATIC_DIR / "app-icon.svg").read_bytes(),
                    )
                elif path == "/apple-touch-icon.png":
                    self._send(
                        HTTPStatus.OK,
                        "image/png",
                        (STATIC_DIR / "apple-touch-icon.png").read_bytes(),
                    )
                elif path == "/health":
                    self._send(
                        HTTPStatus.OK
                        if outer.repository.healthy()
                        else HTTPStatus.SERVICE_UNAVAILABLE,
                        "application/json",
                        json.dumps({"status": "ok"}).encode(),
                    )
                elif path == "/api/dashboard":
                    raw_period = parse_qs(parsed.query).get(
                        "period", [str(outer.settings.history_days)]
                    )[0]
                    period = _period_days(raw_period, outer.settings.history_days)
                    excluded = _station_ids(
                        outer.repository.runtime_settings().get("EXCLUDED_STATION_IDS", "")
                    )
                    stations = [
                        row
                        for row in outer.repository.dashboard_snapshot(
                            outer.settings.max_price_age_minutes
                        )
                        if str(row["station_id"]) not in excluded
                    ]
                    body = json.dumps(
                        {
                            "stations": stations,
                            "history": outer.repository.dashboard_history(period),
                            "station_history": [
                                row
                                for row in outer.repository.dashboard_station_history(
                                    period or 36500
                                )
                                if str(row["station_id"]) not in excluded
                            ],
                        },
                        ensure_ascii=False,
                    ).encode()
                    self._send(HTTPStatus.OK, "application/json; charset=utf-8", body)
                elif path == "/api/statistics":
                    query = parse_qs(parsed.query)
                    raw_period = query.get("period", ["30"])[0]
                    period = _period_days(raw_period)
                    location = query.get("location", [outer.settings.configured_locations[0].key])[
                        0
                    ]
                    fuel = query.get("fuel", [outer.settings.fuel_type.value])[0]
                    body = json.dumps(
                        {
                            "period_days": period,
                            "statistics": outer.repository.advanced_statistics(
                                location, fuel, period
                            ),
                            "history": [
                                row
                                for row in outer.repository.dashboard_history(period)
                                if row["location_key"] == location and row["fuel_type"] == fuel
                            ],
                            "temporal_patterns": outer.repository.temporal_patterns(
                                location, fuel, period or 36500, outer.settings.tz
                            ),
                            "prediction_accuracy": outer.repository.prediction_accuracy(
                                location, fuel
                            ),
                        },
                        ensure_ascii=False,
                    ).encode()
                    self._send(HTTPStatus.OK, "application/json; charset=utf-8", body)
                elif path == "/api/fillups":
                    local_today = datetime.now(ZoneInfo(outer.settings.tz)).date()
                    body = json.dumps(
                        {
                            "fillups": outer.repository.fillups(),
                            "month": outer.repository.fillup_statistics(
                                since_date=local_today.replace(day=1)
                            ),
                            "year": outer.repository.fillup_statistics(
                                since_date=local_today.replace(month=1, day=1)
                            ),
                            "lifetime": outer.repository.fillup_statistics(),
                        },
                        ensure_ascii=False,
                    ).encode()
                    self._send(HTTPStatus.OK, "application/json; charset=utf-8", body)
                elif path in {"/analytics", "/statistics"}:
                    raw_period = parse_qs(parsed.query).get("period", ["30"])[0]
                    period = _period_days(raw_period)
                    self._send(
                        HTTPStatus.OK,
                        "text/html; charset=utf-8",
                        render_statistics(outer.repository, outer.settings, period).encode(),
                    )
                elif path == "/fillups":
                    query = parse_qs(parsed.query)
                    try:
                        from_date = (
                            date.fromisoformat(query["from"][0])
                            if query.get("from", [""])[0]
                            else None
                        )
                        until_date = (
                            date.fromisoformat(query["to"][0]) if query.get("to", [""])[0] else None
                        )
                    except ValueError:
                        self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"Invalid date")
                        return
                    page = render_fillups(
                        outer.repository,
                        outer.settings,
                        vehicle_filter=query.get("vehicle", [""])[0],
                        from_date=from_date,
                        until_date=until_date,
                    ).replace("{csrf_token}", outer._csrf_token)
                    if parse_qs(parsed.query).get("saved") == ["1"]:
                        page = page.replace(
                            '<div class="stats">',
                            '<p role="status" style="color:var(--green)">Plein enregistré.</p><div class="stats">',
                            1,
                        )
                    self._send(HTTPStatus.OK, "text/html; charset=utf-8", page.encode())
                elif path == "/fillups/export.csv":
                    query = parse_qs(parsed.query)
                    try:
                        from_date = (
                            date.fromisoformat(query["from"][0])
                            if query.get("from", [""])[0]
                            else None
                        )
                        until_date = (
                            date.fromisoformat(query["to"][0]) if query.get("to", [""])[0] else None
                        )
                    except ValueError:
                        self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"Invalid date")
                        return
                    output = io.StringIO()
                    writer = csv.writer(output)
                    writer.writerow(
                        (
                            "date",
                            "vehicule",
                            "station",
                            "prix_cents",
                            "litres",
                            "total_cad",
                            "odometre_km",
                            "economie_cad",
                            "plein_complet",
                            "note",
                        )
                    )
                    for row in outer.repository.fillups(
                        since_date=from_date,
                        until_date=until_date,
                        vehicle_key=query.get("vehicle", [""])[0] or None,
                    ):
                        writer.writerow(
                            (
                                row["filled_at"],
                                row["vehicle_name"],
                                row["station_name"],
                                row["price_cents"],
                                row["liters"],
                                row["total_cad"],
                                row["odometer_km"],
                                row["savings_cad"],
                                row["is_full_tank"],
                                row["note"],
                            )
                        )
                    self._send(
                        HTTPStatus.OK,
                        "text/csv; charset=utf-8",
                        output.getvalue().encode("utf-8-sig"),
                    )
                elif path == "/fillups/receipt":
                    try:
                        fillup_id = int(parse_qs(parsed.query).get("id", [""])[0])
                    except ValueError:
                        self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"Invalid receipt")
                        return
                    receipt = outer.repository.fillup_receipt(fillup_id)
                    if receipt is None:
                        self._send(HTTPStatus.NOT_FOUND, "text/plain", b"Receipt not found")
                        return
                    name, content_type, data = receipt
                    safe_name = (
                        "".join(
                            character if character.isalnum() or character in "._-" else "_"
                            for character in Path(name).name
                        )
                        or "receipt"
                    )
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", content_type)
                    self.send_header(
                        "Content-Disposition",
                        f'attachment; filename="{safe_name.encode("ascii", "ignore").decode() or "receipt"}"',
                    )
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(data)
                elif path == "/":
                    page = render_dashboard(outer.repository, outer.settings).replace(
                        "{csrf_token}", outer._csrf_token
                    )
                    saved = parse_qs(urlparse(self.path).query).get("saved")
                    if saved in (["1"], ["station"]):
                        message = (
                            "Preference de station sauvegardee."
                            if saved == ["station"]
                            else "Reglages sauvegardes. Ils seront utilises a la prochaine collecte."
                        )
                        page = page.replace(
                            "</header>",
                            f'</header><div class="notice" role="status">{message}</div>',
                            1,
                        )
                    body = page.encode()
                    self._send(HTTPStatus.OK, "text/html; charset=utf-8", body)
                else:
                    self._send(HTTPStatus.NOT_FOUND, "text/plain", b"Not found")

            def do_POST(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path not in {
                    "/settings",
                    "/station-preference",
                    "/fillups",
                    "/fillups/update",
                    "/fillups/delete",
                }:
                    self._send(HTTPStatus.NOT_FOUND, "text/plain", b"Not found")
                    return
                length = int(self.headers.get("Content-Length", "0"))
                max_length = MAX_RECEIPT_BYTES + 16_384 if path == "/fillups" else 16_384
                if length <= 0 or length > max_length:
                    self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"Invalid request")
                    return
                try:
                    form, files = _decode_form(
                        self.rfile.read(length), self.headers.get("Content-Type", "")
                    )
                except (UnicodeDecodeError, ValueError):
                    self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"Invalid form")
                    return
                if form.get("csrf", [""])[0] != outer._csrf_token:
                    self._send(HTTPStatus.FORBIDDEN, "text/plain", b"Invalid CSRF token")
                    return
                if path == "/fillups/delete":
                    try:
                        deleted = outer.repository.delete_fillup(int(form["fillup_id"][0]))
                    except (KeyError, ValueError):
                        deleted = False
                    if not deleted:
                        self._send(HTTPStatus.NOT_FOUND, "text/plain", b"Fillup not found")
                        return
                    self._redirect("/fillups?saved=deleted")
                    return
                if path == "/fillups/update":
                    try:
                        vehicle = next(
                            item
                            for item in outer.settings.configured_vehicles
                            if item.key == form.get("vehicle_key", [""])[0]
                        )
                        price_cents = float(form.get("price_cents", [""])[0])
                        liters = float(form.get("liters", [""])[0])
                        if price_cents <= 0 or liters <= 0:
                            raise ValueError("Invalid fillup")
                        raw_odometer = form.get("odometer_km", [""])[0]
                        updated = outer.repository.update_fillup(
                            int(form["fillup_id"][0]),
                            filled_at=date.fromisoformat(form["filled_at"][0]),
                            vehicle_key=vehicle.key,
                            vehicle_name=vehicle.name,
                            station_name=form.get("station_name", [""])[0].strip(),
                            price_cents=price_cents,
                            liters=liters,
                            odometer_km=float(raw_odometer) if raw_odometer else None,
                            note=form.get("note", [""])[0][:500],
                            is_full_tank=form.get("is_full_tank") == ["1"],
                        )
                        if not updated:
                            raise ValueError("Invalid fillup")
                    except (KeyError, StopIteration, TypeError, ValueError):
                        self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"Invalid fillup")
                        return
                    self._redirect("/fillups?saved=updated")
                    return
                if path == "/fillups":
                    try:
                        station_id = form.get("station_id", [""])[0]
                        station = next(
                            row
                            for row in outer.repository.dashboard_snapshot()
                            if str(row["station_id"]) == station_id
                        )
                        vehicle_key = form.get("vehicle_key", [""])[0]
                        vehicle = next(
                            item
                            for item in outer.settings.configured_vehicles
                            if item.key == vehicle_key
                        )
                        filled_at = date.fromisoformat(form.get("filled_at", [""])[0])
                        price_cents = float(form.get("price_cents", [""])[0])
                        liters = float(form.get("liters", [""])[0])
                        raw_odometer = form.get("odometer_km", [""])[0]
                        odometer = float(raw_odometer) if raw_odometer else None
                        matching = [
                            float(row["price_cents"])
                            for row in outer.repository.dashboard_snapshot(
                                outer.settings.max_price_age_minutes
                            )
                            if row["location_key"] == station["location_key"]
                            and row["fuel_type"] == station["fuel_type"]
                        ]
                        local_average = statistics.fmean(matching) if matching else None
                        receipt_name = receipt_type = None
                        receipt_data = None
                        if "receipt" in files and files["receipt"][2]:
                            receipt_name, receipt_type, receipt_data = files["receipt"]
                            if (
                                receipt_type not in ALLOWED_RECEIPT_TYPES
                                or len(receipt_data) > MAX_RECEIPT_BYTES
                            ):
                                raise ValueError("Recu invalide")
                        if (
                            price_cents <= 0
                            or liters <= 0
                            or (odometer is not None and odometer < 0)
                        ):
                            raise ValueError("Valeur de plein invalide")
                        outer.repository.add_fillup(
                            filled_at=filled_at,
                            vehicle_key=vehicle.key,
                            vehicle_name=vehicle.name,
                            station_id=station_id,
                            station_name=str(station["name"]),
                            price_cents=price_cents,
                            liters=liters,
                            odometer_km=odometer,
                            local_average_cents=local_average,
                            note=form.get("note", [""])[0][:500],
                            is_full_tank=form.get("is_full_tank") == ["1"],
                            receipt_name=receipt_name,
                            receipt_type=receipt_type,
                            receipt_data=receipt_data,
                        )
                    except (StopIteration, TypeError, ValueError):
                        self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"Invalid fillup")
                        return
                    self._redirect("/fillups?saved=1")
                    return
                if path == "/station-preference":
                    try:
                        station_id = form.get("station_id", [""])[0]
                        action = form.get("action", [""])[0]
                        valid_ids = {
                            str(row["station_id"]) for row in outer.repository.dashboard_snapshot()
                        }
                        if not station_id or station_id not in valid_ids:
                            raise ValueError("Station invalide")
                        values = _updated_station_preferences(
                            outer.repository.runtime_settings(), station_id, action
                        )
                        outer.repository.save_runtime_settings(
                            values, outer.settings.runtime_env_path
                        )
                    except (TypeError, ValueError):
                        self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"Invalid station")
                        return
                    self.send_response(HTTPStatus.SEE_OTHER)
                    self.send_header("Location", "/?saved=station")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    latitude = float(form.get("latitude", [""])[0])
                    longitude = float(form.get("longitude", [""])[0])
                    radius = float(form.get("radius", [""])[0])
                    if not 44 <= latitude <= 63 or not -80 <= longitude <= -57:
                        raise ValueError("Coordonnees hors Quebec")
                    if not 1 <= radius <= 30:
                        raise ValueError("Rayon invalide")
                    outer.repository.save_runtime_settings(
                        {
                            "HOME_LATITUDE": str(latitude),
                            "HOME_LONGITUDE": str(longitude),
                            "SEARCH_RADIUS_KM": str(radius),
                        },
                        outer.settings.runtime_env_path,
                    )
                except (TypeError, ValueError):
                    self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"Invalid settings")
                    return
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/?saved=1")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _send(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(body)

            def _redirect(self, location: str) -> None:
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, message: str, *args: object) -> None:
                logger.debug("web %s", message, *args)

        self._server = ThreadingHTTPServer((settings.web_host, settings.web_port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()
        logger.info("Interface Web disponible sur le port %s", self.settings.web_port)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
