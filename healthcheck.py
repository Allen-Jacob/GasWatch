import os
from pathlib import Path
from urllib.request import urlopen

from app.database import Repository

database_path = Path(os.getenv("DATABASE_PATH", "/app/data/gaswatch.db"))
database_healthy = database_path.exists() and Repository(database_path).healthy()
web_enabled = os.getenv("WEB_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
web_healthy = True
if web_enabled:
    port = int(os.getenv("WEB_PORT", "8080"))
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:  # noqa: S310
            web_healthy = response.status == 200
    except OSError:
        web_healthy = False

raise SystemExit(0 if database_healthy and web_healthy else 1)
