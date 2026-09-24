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
from urllib.parse import parse_qs, urlparse

from app.config import Settings
from app.database import Repository
from app.domain import PriceStats, RecommendationCode
from app.services.analysis import percentile
from app.services.recommendations import recommend

logger = logging.getLogger(__name__)


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
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
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
    return f"""
    <section class="buy-advice {tone}" aria-labelledby="{html.escape(banner_id)}">
      <div class="advice-icon" aria-hidden="true">↗</div>
      <div class="advice-copy"><p class="eyebrow">Verdict du jour · {html.escape(location_name)}</p>
        <h2 id="{html.escape(banner_id)}">{html.escape(heading)}</h2>
        <p>{html.escape(result.reason)}</p></div>
      <span class="advice-badge">{html.escape(badge)}</span>
      <div class="advice-metrics">
        <div><span>Meilleur prix</span><strong>{stats.minimum:.1f} c/L</strong></div>
        <div><span>Comparaison</span><strong>{html.escape(comparison)}</strong></div>
        <div><span>Cible personnelle</span><strong>{html.escape(target_label)}</strong></div>
        <div><span>Historique</span><strong>{len(daily_minimums)} jour{"s" if len(daily_minimums) != 1 else ""}</strong></div>
      </div>
    </section>
    """


def render_dashboard(repository: Repository, settings: Settings) -> str:
    snapshot = repository.dashboard_snapshot(settings.max_price_age_minutes)
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

    runtime = repository.runtime_settings()
    primary_location = settings.configured_locations[0]
    latitude = runtime.get("HOME_LATITUDE", str(primary_location.latitude))
    longitude = runtime.get("HOME_LONGITUDE", str(primary_location.longitude))
    radius = runtime.get("SEARCH_RADIUS_KM", str(primary_location.radius_km))
    favorite_id = runtime.get("FAVORITE_STATION_IDS", "")
    location_names = {location.key: location.name for location in settings.configured_locations}
    station_options = "".join(
        f'<option value="{html.escape(str(row["station_id"]))}" '
        f"{'selected' if str(row['station_id']) == favorite_id else ''}>"
        f"{html.escape(str(row['name']))} — {float(row['price_cents']):.1f} c/L</option>"
        for row in snapshot
    )

    def metric(value: float | None, suffix: str = " c/L") -> str:
        return f"{value:+.1f}{suffix}" if value is not None else "—"

    sections: list[str] = []
    advice_banners: list[str] = []
    for (location_key, fuel_type), stations in groups.items():
        best = stations[0]
        average = sum(float(item["price_cents"]) for item in stations) / len(stations)
        age, freshness = _age_label(str(best["fetched_at"]))
        station_cards: list[str] = []
        for item in stations:
            station_id = str(item["station_id"])
            station_history_rows = grouped_station_history[(location_key, fuel_type, station_id)]
            station_points = [float(point["price_cents"]) for point in station_history_rows]
            station_days = [str(point["day"]) for point in station_history_rows]
            station_min = min(station_points) if station_points else None
            station_max = max(station_points) if station_points else None
            station_change = (
                station_points[-1] - station_points[0] if len(station_points) > 1 else None
            )
            station_key = "station-" + "".join(
                character if character.isalnum() else "-"
                for character in f"{location_key}-{fuel_type}-{station_id}"
            )
            station_cards.append(
                f"""
                <details class="station-card" id="{html.escape(station_key)}" data-station>
                  <summary>
                    <span class="station-name"><strong>{html.escape(str(item["name"]))}</strong>
                      <small>{html.escape(str(item["address"]))}</small></span>
                    <span class="station-price">{float(item["price_cents"]):.1f}<small> c/L</small></span>
                    <span>{float(item["distance_km"]):.1f} km</span>
                    <span>{html.escape(_age_label(str(item["fetched_at"]))[0])}</span>
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
                """
            )
        station_list = "".join(station_cards)
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
                  <strong>{float(best["price_cents"]):.1f}<small> c/L</small></strong>
                  <p>{html.escape(str(best["name"]))}</p></article>
                <article><span>Moyenne locale</span><strong>{average:.1f}<small> c/L</small></strong>
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
<meta http-equiv="refresh" content="60"><title>GasWatch</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23111110'/%3E%3Cpath d='M18 12h25v43H18z' fill='%23d7c7ad'/%3E%3Cpath d='M23 18h15v13H23z' fill='%23111110'/%3E%3Cpath d='M43 22c8 1 6 14 6 21 0 5 6 5 6 0V28' fill='none' stroke='%23d7c7ad' stroke-width='5'/%3E%3C/svg%3E">
<style>
:root{{--ink:#f4f1e8;--muted:#aaa69d;--panel:#171715;--panel2:#201f1c;--line:#393732;
--accent:#e6c77a;--soft:#fff3d3;--dim:#7d7971;--black:#0e0e0d;--green:#71d99b;
--amber:#f0b45f;--red:#ef7d72;--blue:#70b8d7}}*{{box-sizing:border-box}}
body{{margin:0;background:var(--black);color:var(--ink);font:16px/1.5 ui-sans-serif,system-ui,sans-serif}}
.shell{{width:min(1180px,calc(100% - 32px));margin:auto;padding:34px 0 64px}}
header{{display:flex;align-items:end;justify-content:space-between;margin-bottom:28px;border-bottom:1px solid var(--line);padding-bottom:20px}}
h1{{font-size:clamp(2rem,5vw,4rem);letter-spacing:-.06em;line-height:.9;margin:0}}h1 b{{color:var(--accent)}}
header p,.meta,article p{{color:var(--muted);margin:.35rem 0 0;font-size:.875rem}}
.buy-advice{{position:relative;display:grid;grid-template-columns:auto 1fr auto;gap:14px 18px;align-items:center;
padding:22px 24px;margin-bottom:24px;border:1px solid color-mix(in srgb,var(--verdict) 52%,var(--line));
border-radius:18px;background:linear-gradient(120deg,color-mix(in srgb,var(--verdict) 14%,var(--panel)),var(--panel) 62%);overflow:hidden}}
.buy-advice::after{{content:'';position:absolute;width:180px;height:180px;right:-70px;top:-100px;border-radius:50%;background:var(--verdict);opacity:.09}}
.buy-advice.excellent,.buy-advice.good{{--verdict:var(--green)}}.buy-advice.normal{{--verdict:var(--blue)}}
.buy-advice.wait,.buy-advice.learning{{--verdict:var(--amber)}}.buy-advice.high{{--verdict:var(--red)}}
.advice-icon{{display:grid;place-items:center;width:42px;height:42px;border-radius:12px;background:var(--verdict);color:#10110f;font-size:1.4rem;font-weight:900}}
.advice-copy h2{{font-size:1.65rem;margin:2px 0}}.advice-copy>p:last-child{{color:var(--muted);margin:4px 0 0}}
.advice-badge{{position:relative;z-index:1;color:var(--verdict);border:1px solid color-mix(in srgb,var(--verdict) 55%,transparent);background:#111;
padding:7px 11px;border-radius:99px;font-size:.78rem;font-weight:800}}
.advice-metrics{{grid-column:1/-1;display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--line);padding-top:16px;margin-top:2px}}
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
.hero-price{{background:var(--panel2)}}.hero-price strong{{color:var(--accent)}}.chart{{display:block;width:100%;height:90px;margin-top:8px}}
.chart polyline{{fill:none;stroke:var(--accent);stroke-width:4;stroke-linejoin:round;stroke-linecap:round}}
.chart-point{{fill:var(--panel);stroke:var(--accent);stroke-width:3;cursor:crosshair;transition:r .15s ease,fill .15s ease}}
.chart-point:hover,.chart-point:focus{{r:7;fill:var(--accent);outline:none}}
.chart text{{fill:var(--muted);font-size:12px}}.empty-chart{{color:var(--muted);padding-top:35px}}
#chart-tooltip{{position:fixed;z-index:20;pointer-events:none;opacity:0;transform:translate(-50%,-115%);padding:7px 10px;
border-radius:8px;background:var(--soft);color:#171512;font-size:.78rem;font-weight:800;box-shadow:0 8px 30px #0009;transition:opacity .12s ease;white-space:nowrap}}
#chart-tooltip.visible{{opacity:1}}
.settings{{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:22px 26px;margin-bottom:24px}}
.settings h2{{margin-bottom:14px}}form{{display:grid;grid-template-columns:1fr 1fr .7fr 1.5fr auto;gap:12px;align-items:end}}
label{{display:grid;gap:6px;color:var(--muted);font-size:.8rem}}input,select,button{{font:inherit;border-radius:9px;border:1px solid var(--line);padding:10px 12px}}
input,select{{background:#10100f;color:var(--ink);min-width:0}}button{{background:var(--accent);color:#171512;font-weight:800;cursor:pointer}}
button:hover{{background:var(--soft)}}.settings>p{{color:var(--muted);font-size:.8rem;margin:12px 0 0}}
.notice{{background:var(--panel2);color:var(--soft);border:1px solid var(--accent);padding:10px 14px;border-radius:9px;margin-bottom:14px}}
.history-stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:8px}}.history-stats div{{background:#111110;padding:8px;border-radius:8px}}
.history-stats span{{display:block;color:var(--muted);font-size:.65rem;text-transform:uppercase}}.history-stats strong{{font-size:.9rem;margin:2px 0 0;letter-spacing:0}}
.station-list-head,.station-card summary{{display:grid;grid-template-columns:minmax(260px,1fr) 130px 120px 130px;align-items:center;gap:16px;padding:13px 26px}}
.station-list-head{{color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;border-bottom:1px solid var(--line)}}
.station-card{{border-bottom:1px solid var(--line)}}.station-card:last-child{{border-bottom:0}}
.station-card summary{{cursor:pointer;list-style:none;transition:background .18s ease}}.station-card summary::-webkit-details-marker{{display:none}}
.station-card summary:hover,.station-card[open] summary{{background:var(--panel2)}}
.station-card summary::after{{content:'+';color:var(--accent);font-size:1.25rem;position:absolute;right:10px}}
.station-card[open] summary::after{{content:'−'}}.station-card summary{{position:relative}}
.station-card summary>span{{color:var(--muted);font-size:.88rem}}.station-name strong{{display:block;color:var(--ink);font-size:1rem}}
.station-name small{{display:block;color:var(--muted);font-size:.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.station-price{{color:var(--accent)!important;font-size:1.2rem!important;font-weight:800}}.station-price small{{font-size:.75rem}}
.station-detail{{display:grid;grid-template-columns:1fr 2fr;gap:16px 28px;padding:20px 26px 26px;background:#111110;border-top:1px solid var(--line)}}
.station-detail .chart{{height:118px;margin:0}}.station-stats{{grid-column:1/-1;display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
.station-stats div{{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:var(--panel)}}
.station-stats span{{display:block;color:var(--muted);font-size:.68rem;text-transform:uppercase}}.station-stats strong{{font-size:1rem}}
.empty{{text-align:center;padding:80px 24px;background:var(--panel);border:1px solid var(--line);border-radius:18px}}
.pump{{font-size:3rem}}footer{{display:flex;justify-content:space-between;gap:20px;color:var(--muted);font-size:.8rem;margin-top:24px}}
@media(max-width:760px){{.shell{{width:min(100% - 20px,1180px);padding-top:22px}}header{{align-items:start}}header>p{{text-align:right;max-width:160px}}
.buy-advice{{grid-template-columns:auto 1fr;padding:18px}}.advice-badge{{grid-column:1/-1;width:max-content}}
.advice-metrics{{grid-template-columns:1fr 1fr;gap:14px 0}}.advice-metrics div:nth-child(2){{border:0}}.advice-metrics div:nth-child(3){{padding-left:0}}
.settings{{padding:18px}}form{{grid-template-columns:1fr 1fr}}form label:nth-child(4),form button{{grid-column:1/-1}}
.summary-grid{{grid-template-columns:1fr 1fr}}article{{padding:18px;min-height:130px}}article.trend{{grid-column:1/-1;border-top:1px solid var(--line)}}
.station-list-head{{display:none}}.station-card summary{{grid-template-columns:1fr auto;padding:15px 18px 15px 18px}}
.station-card summary>span:nth-child(3),.station-card summary>span:nth-child(4){{display:none}}.station-card summary::after{{right:8px}}
.station-price{{padding-right:22px}}.station-detail{{grid-template-columns:1fr;padding:18px}}.station-stats{{grid-template-columns:1fr 1fr}}
footer{{display:block}}}}
</style></head><body><main class="shell"><header><div><h1>Gas<b>Watch</b></h1>
<p>Prix recents autour de vos emplacements</p></div><p>Actualisation automatique<br>toutes les 60 secondes</p></header>
{"".join(advice_banners)}
<section class="settings"><h2>Mes reglages</h2>
<form method="post" action="/settings">
<input type="hidden" name="csrf" value="{{csrf_token}}">
<label>Latitude<input name="latitude" inputmode="decimal" required value="{html.escape(latitude)}"></label>
<label>Longitude<input name="longitude" inputmode="decimal" required value="{html.escape(longitude)}"></label>
<label>Rayon (km)<input name="radius" inputmode="decimal" required value="{html.escape(radius)}"></label>
<label>Station favorite<select name="favorite_station_id"><option value="">Aucune</option>{station_options}</select></label>
<button type="submit">Enregistrer</button></form>
<p>La position est envoyee uniquement a Gas Quebec pour la recherche. Une copie .env est conservee dans le volume GasWatch.</p></section>
{"".join(sections)}
<footer><span>* Distance geographique, pas routiere.</span><span>Page generee {generated}</span></footer>
</main><div id="chart-tooltip" role="tooltip"></div><script>
for (const detail of document.querySelectorAll('[data-station]')) {{
  if (location.hash === `#${{detail.id}}`) detail.open = true;
  detail.addEventListener('toggle', () => {{
    if (detail.open) history.replaceState(null, '', `#${{detail.id}}`);
  }});
}}
const chartTooltip = document.querySelector('#chart-tooltip');
function showChartTooltip(point) {{
  const box = point.getBoundingClientRect();
  chartTooltip.textContent = point.dataset.tooltip;
  chartTooltip.style.left = `${{box.left + box.width / 2}}px`;
  chartTooltip.style.top = `${{box.top}}px`;
  chartTooltip.classList.add('visible');
}}
for (const point of document.querySelectorAll('.chart-point')) {{
  point.addEventListener('mouseenter', () => showChartTooltip(point));
  point.addEventListener('focus', () => showChartTooltip(point));
  point.addEventListener('mouseleave', () => chartTooltip.classList.remove('visible'));
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
                if path == "/health":
                    self._send(
                        HTTPStatus.OK
                        if outer.repository.healthy()
                        else HTTPStatus.SERVICE_UNAVAILABLE,
                        "application/json",
                        json.dumps({"status": "ok"}).encode(),
                    )
                elif path == "/api/dashboard":
                    body = json.dumps(
                        {
                            "stations": outer.repository.dashboard_snapshot(
                                outer.settings.max_price_age_minutes
                            ),
                            "history": outer.repository.dashboard_history(
                                outer.settings.history_days
                            ),
                            "station_history": outer.repository.dashboard_station_history(
                                outer.settings.history_days
                            ),
                        },
                        ensure_ascii=False,
                    ).encode()
                    self._send(HTTPStatus.OK, "application/json; charset=utf-8", body)
                elif path == "/":
                    page = render_dashboard(outer.repository, outer.settings).replace(
                        "{csrf_token}", outer._csrf_token
                    )
                    if parse_qs(urlparse(self.path).query).get("saved") == ["1"]:
                        page = page.replace(
                            "</header>",
                            '</header><div class="notice" role="status">Reglages sauvegardes. '
                            "Ils seront utilises a la prochaine collecte.</div>",
                            1,
                        )
                    body = page.encode()
                    self._send(HTTPStatus.OK, "text/html; charset=utf-8", body)
                else:
                    self._send(HTTPStatus.NOT_FOUND, "text/plain", b"Not found")

            def do_POST(self) -> None:  # noqa: N802
                if urlparse(self.path).path != "/settings":
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
                try:
                    latitude = float(form.get("latitude", [""])[0])
                    longitude = float(form.get("longitude", [""])[0])
                    radius = float(form.get("radius", [""])[0])
                    if not 44 <= latitude <= 63 or not -80 <= longitude <= -57:
                        raise ValueError("Coordonnees hors Quebec")
                    if not 1 <= radius <= 30:
                        raise ValueError("Rayon invalide")
                    favorite = form.get("favorite_station_id", [""])[0]
                    valid_ids = {
                        str(row["station_id"])
                        for row in outer.repository.dashboard_snapshot(
                            outer.settings.max_price_age_minutes
                        )
                    }
                    if favorite and favorite not in valid_ids:
                        raise ValueError("Station favorite invalide")
                    outer.repository.save_runtime_settings(
                        {
                            "HOME_LATITUDE": str(latitude),
                            "HOME_LONGITUDE": str(longitude),
                            "SEARCH_RADIUS_KM": str(radius),
                            "FAVORITE_STATION_IDS": favorite,
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
