from __future__ import annotations

import asyncio
import contextlib
import math
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


SUPPORTED_RESOLUTIONS = (360, 480, 720, 1080)
URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


def extract_url(text: str | None) -> str | None:
    if not text:
        return None
    match = URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,)")


def is_probable_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def sanitize_filename(value: str, max_len: int = 120) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" ._")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or "download")[:max_len]


def human_size(num_bytes: int | float | None) -> str:
    if not num_bytes or num_bytes < 0:
        return "unknown size"
    units = ("B", "KB", "MB", "GB")
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def progress_bar(percent: float) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(percent // 10)
    empty = 10 - filled
    return f"{'🟩' * filled}{'⬜' * empty}\n{percent:.0f}%"


def best_thumbnail(info: dict) -> str | None:
    thumbnails = info.get("thumbnails") or []
    if thumbnails:
        sorted_thumbs = sorted(
            thumbnails,
            key=lambda item: (item.get("width") or 0) * (item.get("height") or 0),
            reverse=True,
        )
        return sorted_thumbs[0].get("url")
    return info.get("thumbnail")


def estimate_format_size(formats: Iterable[dict], format_ids: Iterable[str]) -> int | None:
    sizes: list[int] = []
    wanted = set(format_ids)
    for fmt in formats:
        if str(fmt.get("format_id")) not in wanted:
            continue
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        if size:
            sizes.append(int(size))
    return sum(sizes) if sizes else None


def remove_tree(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


@dataclass
class ProgressState:
    """Thread-safe bridge between yt-dlp hooks and async Telegram updates."""

    loop: asyncio.AbstractEventLoop
    percent: float = 0.0
    status: str = "queued"
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed: float | None = None
    eta: float | None = None
    last_error: str | None = None
    finished_path: Path | None = None
    updated_at: float = field(default_factory=time.monotonic)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def update_from_thread(self, **values: object) -> None:
        self.loop.call_soon_threadsafe(asyncio.create_task, self._update(**values))

    async def _update(self, **values: object) -> None:
        async with self._lock:
            for key, value in values.items():
                setattr(self, key, value)
            self.updated_at = time.monotonic()

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                "percent": self.percent,
                "status": self.status,
                "downloaded_bytes": self.downloaded_bytes,
                "total_bytes": self.total_bytes,
                "speed": self.speed,
                "eta": self.eta,
                "last_error": self.last_error,
                "finished_path": self.finished_path,
            }


def percent_from_hook(data: dict) -> float:
    downloaded = data.get("downloaded_bytes") or 0
    total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
    if not total:
        return 0.0
    return min(100.0, math.floor((downloaded / total) * 1000) / 10)
