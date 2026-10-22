"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the project has no extra dependency for it."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/procurement"
        )
    )
    fx_api_url: str = field(
        default_factory=lambda: os.getenv("FX_API_URL", "https://api.frankfurter.dev/v1")
    )
    fx_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("FX_TIMEOUT_SECONDS", "10"))
    )
    raw_dir: Path = PROJECT_ROOT / "data" / "raw"
    sql_dir: Path = PROJECT_ROOT / "sql"
    base_currency: str = "USD"
    supported_currencies: tuple[str, ...] = ("USD", "EUR", "MXN", "CNY")
