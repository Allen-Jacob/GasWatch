from __future__ import annotations

import html
import json
import logging
import secrets
import statistics
import threading
from collections import defaultdict
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from app.config import Settings
from app.database import Repository
from app.domain import PriceStats, RecommendationCode
from app.services.analysis import percentile, predict_price_direction
from app.services.recommendations import recommend

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("static")


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
    if direction == "unknown":
        return "unknown", "·", "Tendance à venir : en calcul", "Il faut au moins 2 jours"
    if direction == "stable":
        return (
            "stable",
            "→",
            "Tendance à venir : plutôt stable",
            f"Signal {confidence} · {len(prices[-7:])} jours de vos données",
        )
    label = "hausse" if direction == "up" else "baisse"
    arrow = "↑" if direction == "up" else "↓"
    return (
        direction,
        arrow,
        f"Tendance à venir : {label} probable",
        f"Signal {confidence} · {slope:+.1f} c/L par jour · vos {len(prices[-7:])} derniers jours",
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

    def metric(value: float | None, suffix: str = " c/L") -> str:
        return f"{value:+.1f}{suffix}" if value is not None else "—"

    sections: list[str] = []
    advice_banners: list[str] = []
    for (location_key, fuel_type), stations in groups.items():
        best = stations[0]
        average = sum(float(item["price_cents"]) for item in stations) / len(stations)
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
                      <div><span>Variation</span><strong>{metric(station_change)}</strong></div>
                      <div><span>Jours suivis</span><strong>{len(station_points)}</strong></div>
                    </div>
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
        best_logo = _brand_logo(best.get("brand"), best.get("name"))
        market_price_trend = _price_trend(averages)

        stats_cards = (
            f"<div><span>Minimum</span><strong>{metric(history_min).lstrip('+')}</strong></div>"
            f"<div><span>Moyenne</span><strong>{metric(history_avg).lstrip('+')}</strong></div>"
            f"<div><span>Maximum</span><strong>{metric(history_max).lstrip('+')}</strong></div>"
            f"<div><span>Variation</span><strong>{metric(change)}</strong></div>"
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
                <article class="hero-price"><span>Meilleur prix</span>
                  <strong>{float(best["price_cents"]):.1f}<small> c/L</small>{_price_trend([float(point["price_cents"]) for point in grouped_station_history[(location_key, fuel_type, str(best["station_id"]))]])}</strong>
                  <p class="hero-station">{best_logo}{html.escape(str(best["name"]))}</p></article>
                <article><span>Moyenne locale</span><strong>{average:.1f}<small> c/L</small>{market_price_trend}</strong>
                  <p>{len(stations)} stations disponibles</p></article>
                <article class="trend"><span>Moyenne des stations suivies — {settings.history_days} jours</span>
                  {chart}<div class="history-stats">{stats_cards}</div></article>
              </div>
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

    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
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
body{{margin:0;background:var(--black);color:var(--ink);font:16px/1.5 ui-sans-serif,system-ui,sans-serif}}
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
.station-list-head{{display:none}}.station-card summary{{grid-template-columns:minmax(0,1fr) auto auto 16px;padding:13px 12px 13px 18px;gap:8px}}
.station-card summary>span:nth-child(3),.station-card summary>span:nth-child(4){{display:none}}
.station-price{{padding-right:3px}}.station-actions{{gap:3px}}.icon-button{{width:32px;height:32px}}.station-detail{{grid-template-columns:1fr;padding:18px}}.station-stats{{grid-template-columns:1fr 1fr}}
footer{{display:block}}}}
</style></head><body><main class="shell"><header><div><h1>Gas<b>Watch</b></h1>
<p>Prix recents autour de vos emplacements</p></div><div class="header-tools">
<p>Actualisation automatique<br>toutes les 60 secondes</p><details class="settings-menu">
<summary aria-label="Ouvrir mes réglages" title="Mes réglages">⚙</summary>
<section class="settings"><h2>Mes réglages</h2>
<form method="post" action="/settings" class="settings-form">
<input type="hidden" name="csrf" value="{{csrf_token}}">
<label>Latitude<input name="latitude" inputmode="decimal" required value="{html.escape(latitude)}"></label>
<label>Longitude<input name="longitude" inputmode="decimal" required value="{html.escape(longitude)}"></label>
<label>Rayon (km)<input name="radius" inputmode="decimal" required value="{html.escape(radius)}"></label>
<button type="submit">Enregistrer</button></form>
<p>Utilisez l'etoile a cote d'une station pour la garder en haut. La position est envoyee uniquement a Gas Quebec pour la recherche.</p>
{f'<div class="excluded-list"><h3>Stations exclues</h3>{excluded_controls}</div>' if excluded_controls else ""}</section></details></div></header>
{"".join(advice_banners)}
{"".join(sections)}
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


class DashboardServer:
    def __init__(self, repository: Repository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings
        self._csrf_token = secrets.token_urlsafe(24)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
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
                            "history": outer.repository.dashboard_history(
                                outer.settings.history_days
                            ),
                            "station_history": [
                                row
                                for row in outer.repository.dashboard_station_history(
                                    outer.settings.history_days
                                )
                                if str(row["station_id"]) not in excluded
                            ],
                        },
                        ensure_ascii=False,
                    ).encode()
                    self._send(HTTPStatus.OK, "application/json; charset=utf-8", body)
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
                if path not in {"/settings", "/station-preference"}:
                    self._send(HTTPStatus.NOT_FOUND, "text/plain", b"Not found")
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 8192:
                    self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"Invalid request")
                    return
                form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
                if form.get("csrf", [""])[0] != outer._csrf_token:
                    self._send(HTTPStatus.FORBIDDEN, "text/plain", b"Invalid CSRF token")
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
