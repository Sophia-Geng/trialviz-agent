"""Settings read from environment variables (and a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1")
    ctgov_base_url: str = os.getenv("CTGOV_BASE_URL", "https://clinicaltrials.gov/api/v2")
    ctgov_min_interval_s: float = float(os.getenv("CTGOV_MIN_INTERVAL_S", "1.0"))
    ctgov_timeout_s: float = float(os.getenv("CTGOV_TIMEOUT_S", "30"))
    cache_enabled: bool = _bool("CACHE_ENABLED", True)
    cache_dir: Path = Path(os.getenv("CACHE_DIR", ROOT / ".cache"))


settings = Settings()
