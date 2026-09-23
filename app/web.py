from __future__ import annotations

import html
import json
import logging
import secrets
import threading
from collections import defaultdict
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app.config import Settings
from app.database import Repository

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


def _sparkline(points: list[float]) -> str:
    if not points:
        return '<div class="empty-chart">Historique en construction</div>'
    width, height, padding = 560, 118, 8
    low, high = min(points), max(points)
    spread = max(high - low, 1)
    coords: list[str] = []
    for index, value in enumerate(points):
        x = padding + index * (width - 2 * padding) / max(len(points) - 1, 1)
        y = height - padding - (value - low) * (height - 2 * padding) / spread
        coords.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Evolution du prix minimum"><polyline points="{" ".join(coords)}" />'
        f'<text x="8" y="16">{high:.1f}</text><text x="8" y="110">{low:.1f}</text></svg>'
    )


def render_dashboard(repository: Repository, settings: Settings) -> str:
    snapshot = repository.dashboard_snapshot(settings.max_price_age_minutes)
    history = repository.dashboard_history(settings.history_days)
    grouped_history: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in history:
        grouped_history[(row["location_key"], row["fuel_type"])].append(row)

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
    sections: list[str] = []
    for (location_key, fuel_type), stations in groups.items():
        best = stations[0]
        average = sum(float(item["price_cents"]) for item in stations) / len(stations)
        age, freshness = _age_label(str(best["fetched_at"]))
        rows = "".join(
            "<tr>"
            f"<td><strong>{html.escape(str(item['name']))}</strong>"
            f"<span>{html.escape(str(item['address']))}</span></td>"
            f'<td class="price">{float(item["price_cents"]):.1f}<small> c/L</small></td>'
            f"<td>{float(item['distance_km']):.1f} km</td>"
            f"<td>{html.escape(_age_label(str(item['fetched_at']))[0])}</td>"
            "</tr>"
            for item in stations[:12]
        )
        market_history = grouped_history[(location_key, fuel_type)]
        minimums = [float(item["minimum"]) for item in market_history]
        chart = _sparkline(minimums)
        history_min = min(minimums) if minimums else None
        history_max = max(minimums) if minimums else None
        history_avg = sum(minimums) / len(minimums) if minimums else None
        change = minimums[-1] - minimums[0] if len(minimums) > 1 else None

        def metric(value: float | None, suffix: str = " c/L") -> str:
            return f"{value:+.1f}{suffix}" if value is not None else "—"

        stats_cards = (
            f"<div><span>Minimum</span><strong>{metric(history_min).lstrip('+')}</strong></div>"
            f"<div><span>Moyenne</span><strong>{metric(history_avg).lstrip('+')}</strong></div>"
            f"<div><span>Maximum</span><strong>{metric(history_max).lstrip('+')}</strong></div>"
            f"<div><span>Variation</span><strong>{metric(change)}</strong></div>"
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
                  <p>{len(stations)} stations conservees</p></article>
                <article class="trend"><span>Minimums — {settings.history_days} jours</span>
                  {chart}<div class="history-stats">{stats_cards}</div></article>
              </div>
              <div class="table-wrap"><table><thead><tr><th>Station</th><th>Prix</th>
                <th>Distance*</th><th>Releve</th></tr></thead><tbody>{rows}</tbody></table></div>
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
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%230d1714'/%3E%3Cpath d='M18 12h25v43H18z' fill='%23f4b942'/%3E%3Cpath d='M23 18h15v13H23z' fill='%230d1714'/%3E%3Cpath d='M43 22c8 1 6 14 6 21 0 5 6 5 6 0V28' fill='none' stroke='%23f4b942' stroke-width='5'/%3E%3C/svg%3E">
<style>
:root{{--ink:#ecf4f0;--muted:#8fa49b;--panel:#13231e;--panel2:#192d26;--line:#29433a;
--accent:#f4b942;--green:#65d69e;--orange:#ef9564;--red:#e66f6f}}*{{box-sizing:border-box}}
body{{margin:0;background:#09120f;color:var(--ink);font:16px/1.5 ui-sans-serif,system-ui,sans-serif}}
.shell{{width:min(1180px,calc(100% - 32px));margin:auto;padding:34px 0 64px}}
header{{display:flex;align-items:end;justify-content:space-between;margin-bottom:28px;border-bottom:1px solid var(--line);padding-bottom:20px}}
h1{{font-size:clamp(2rem,5vw,4rem);letter-spacing:-.06em;line-height:.9;margin:0}}h1 b{{color:var(--accent)}}
header p,.meta,td span,article p{{color:var(--muted);margin:.35rem 0 0;font-size:.875rem}}
.market{{background:var(--panel);border:1px solid var(--line);border-radius:18px;overflow:hidden;margin-bottom:24px}}
.market-head{{display:flex;align-items:center;justify-content:space-between;padding:24px 26px 18px}}
.eyebrow{{color:var(--accent);font-size:.75rem;font-weight:800;letter-spacing:.15em;margin:0;text-transform:uppercase}}
h2{{margin:2px 0 0;font-size:1.5rem}}.status{{font-size:.78rem;padding:6px 10px;border-radius:99px;background:#203d33}}
.status.fresh{{color:var(--green)}}.status.aging{{color:var(--orange)}}.status.stale{{color:var(--red)}}
.summary-grid{{display:grid;grid-template-columns:1fr 1fr 2fr;border-block:1px solid var(--line)}}
article{{min-height:150px;padding:22px 26px;border-right:1px solid var(--line)}}article:last-child{{border:0}}
article>span{{color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.08em}}
article strong{{display:block;font-size:2.3rem;margin-top:12px;letter-spacing:-.04em}}article small{{font-size:.9rem;color:var(--muted)}}
.hero-price{{background:var(--panel2)}}.hero-price strong{{color:var(--accent)}}.chart{{display:block;width:100%;height:90px;margin-top:8px}}
.chart polyline{{fill:none;stroke:var(--green);stroke-width:4;stroke-linejoin:round;stroke-linecap:round}}
.chart text{{fill:var(--muted);font-size:12px}}.empty-chart{{color:var(--muted);padding-top:35px}}
.settings{{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:22px 26px;margin-bottom:24px}}
.settings h2{{margin-bottom:14px}}form{{display:grid;grid-template-columns:1fr 1fr .7fr 1.5fr auto;gap:12px;align-items:end}}
label{{display:grid;gap:6px;color:var(--muted);font-size:.8rem}}input,select,button{{font:inherit;border-radius:9px;border:1px solid var(--line);padding:10px 12px}}
input,select{{background:#0d1915;color:var(--ink);min-width:0}}button{{background:var(--accent);color:#171306;font-weight:800;cursor:pointer}}
.settings>p{{color:var(--muted);font-size:.8rem;margin:12px 0 0}}.notice{{background:#173a2c;color:var(--green);padding:10px 14px;border-radius:9px;margin-bottom:14px}}
.history-stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:8px}}.history-stats div{{background:#0e1b17;padding:8px;border-radius:8px}}
.history-stats span{{display:block;color:var(--muted);font-size:.65rem;text-transform:uppercase}}.history-stats strong{{font-size:.9rem;margin:2px 0 0;letter-spacing:0}}
.table-wrap{{overflow-x:auto}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:14px 26px;border-bottom:1px solid var(--line)}}
th{{color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.08em}}td span{{display:block;white-space:nowrap}}td.price{{color:var(--accent);font-size:1.2rem;font-weight:800}}
tbody tr:last-child td{{border:0}}.empty{{text-align:center;padding:80px 24px;background:var(--panel);border:1px solid var(--line);border-radius:18px}}
.pump{{font-size:3rem}}footer{{display:flex;justify-content:space-between;gap:20px;color:var(--muted);font-size:.8rem;margin-top:24px}}
@media(max-width:760px){{.shell{{width:min(100% - 20px,1180px);padding-top:22px}}header{{align-items:start}}header>p{{text-align:right;max-width:160px}}
.settings{{padding:18px}}form{{grid-template-columns:1fr 1fr}}form label:nth-child(4),form button{{grid-column:1/-1}}
.summary-grid{{grid-template-columns:1fr 1fr}}article{{padding:18px;min-height:130px}}article.trend{{grid-column:1/-1;border-top:1px solid var(--line)}}
th,td{{padding:12px 18px}}th:nth-child(4),td:nth-child(4){{display:none}}footer{{display:block}}}}
</style></head><body><main class="shell"><header><div><h1>Gas<b>Watch</b></h1>
<p>Prix recents autour de vos emplacements</p></div><p>Actualisation automatique<br>toutes les 60 secondes</p></header>
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
</main></body></html>"""


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
