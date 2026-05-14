from __future__ import annotations

import base64
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
    youtube_cookies_file: Path | None = None

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN environment variable is required.")

        config = cls(bot_token=token, youtube_cookies_file=cls._load_youtube_cookies())
        config.downloads_dir.mkdir(parents=True, exist_ok=True)
        return config

    @staticmethod
    def _load_youtube_cookies() -> Path | None:
        cookie_path = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
        if cookie_path:
            path = Path(cookie_path)
            if not path.exists():
                raise RuntimeError(f"YOUTUBE_COOKIES_FILE does not exist: {path}")
            return path

        encoded_cookies = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
        if not encoded_cookies:
            return None

        cookies_path = BASE_DIR / "downloads" / "youtube_cookies.txt"
        cookies_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            cookies_text = base64.b64decode(encoded_cookies).decode("utf-8")
        except Exception as exc:
            raise RuntimeError("YOUTUBE_COOKIES_B64 is not valid base64 text.") from exc

        if not cookies_text.startswith(("# HTTP Cookie File", "# Netscape HTTP Cookie File")):
            raise RuntimeError("YouTube cookies must be in Netscape cookies.txt format.")

        cookies_path.write_text(cookies_text, encoding="utf-8", newline="\n")
        return cookies_path


CONFIG = Config.from_env()
