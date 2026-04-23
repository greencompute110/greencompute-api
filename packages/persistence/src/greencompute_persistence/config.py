from __future__ import annotations

import os
from pathlib import Path


DEFAULT_SQLITE_PATH = Path(__file__).resolve().parents[4] / "greencompute-api.db"


def get_database_url() -> str:
    return os.getenv("GREENCOMPUTE_DATABASE_URL", f"sqlite+pysqlite:///{DEFAULT_SQLITE_PATH}")


def should_bootstrap_schema() -> bool:
    return os.getenv("GREENCOMPUTE_DB_BOOTSTRAP", "").lower() in {"1", "true", "yes"}
