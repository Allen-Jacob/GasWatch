import os
from pathlib import Path

from app.database import Repository

database_path = Path(os.getenv("DATABASE_PATH", "/app/data/gaswatch.db"))
raise SystemExit(0 if database_path.exists() and Repository(database_path).healthy() else 1)
