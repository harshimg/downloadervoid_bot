from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Config:
    """Runtime configuration loaded from environment variables."""

    bot_token: str
    downloads_dir: Path = BASE_DIR / "downloads"
    queue_workers: int = int(os.getenv("QUEUE_WORKERS", "2"))
    max_concurrent_downloads: int = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
    progress_interval_seconds: float = float(os.getenv("PROGRESS_INTERVAL_SECONDS", "2"))
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "1900"))
    url_cache_ttl_seconds: int = int(os.getenv("URL_CACHE_TTL_SECONDS", "1800"))

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN environment variable is required.")

        config = cls(bot_token=token)
        config.downloads_dir.mkdir(parents=True, exist_ok=True)
        return config


CONFIG = Config.from_env()
