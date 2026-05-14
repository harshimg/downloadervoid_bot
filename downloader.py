from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp

from config import CONFIG
from utils import (
    SUPPORTED_RESOLUTIONS,
    ProgressState,
    best_thumbnail,
    estimate_format_size,
    percent_from_hook,
    remove_tree,
    sanitize_filename,
)


class DownloadError(Exception):
    """Raised when metadata extraction or media download fails."""


@dataclass(frozen=True)
class FormatOption:
    key: str
    label: str
    format_selector: str
    output_kind: str
    estimated_size: int | None = None

    @property
    def requires_ffmpeg(self) -> bool:
        return "+" in self.format_selector or self.output_kind == "audio"


@dataclass(frozen=True)
class MediaInfo:
    url: str
    title: str
    thumbnail: str | None
    webpage_url: str
    duration: int | None
    options: dict[str, FormatOption]
    raw_info: dict[str, Any]


@dataclass(frozen=True)
class DownloadResult:
    file_path: Path
    title: str
    output_kind: str


class Downloader:
    """Async wrapper around yt-dlp with Railway-friendly defaults."""

    def __init__(self, downloads_dir: Path = CONFIG.downloads_dir) -> None:
        self.downloads_dir = downloads_dir
        self._download_semaphore = asyncio.Semaphore(CONFIG.max_concurrent_downloads)
        self._ffmpeg_path = self._find_ffmpeg()

    async def extract_info(self, url: str) -> MediaInfo:
        return await asyncio.to_thread(self._extract_info_sync, url)

    def _extract_info_sync(self, url: str) -> MediaInfo:
        ydl_opts = self._base_ydl_opts()
        ydl_opts.update(
            {
                "skip_download": True,
                "extract_flat": False,
                "noplaylist": True,
            }
        )

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # yt-dlp raises many extractor-specific errors.
            raise DownloadError(f"Could not fetch media information: {exc}") from exc

        if not info:
            raise DownloadError("No media information was returned.")

        formats = info.get("formats") or []
        options = self._build_format_options(formats)
        if not options:
            raise DownloadError("No supported 360p, 480p, 720p, 1080p, or audio format was found.")

        title = info.get("title") or "Untitled"
        return MediaInfo(
            url=url,
            title=title,
            thumbnail=best_thumbnail(info),
            webpage_url=info.get("webpage_url") or url,
            duration=info.get("duration"),
            options=options,
            raw_info=info,
        )

    async def download(self, media: MediaInfo, option: FormatOption, progress: ProgressState) -> DownloadResult:
        async with self._download_semaphore:
            work_dir = self.downloads_dir / uuid.uuid4().hex
            work_dir.mkdir(parents=True, exist_ok=True)
            try:
                file_path = await asyncio.to_thread(
                    self._download_sync,
                    media.url,
                    media.title,
                    option,
                    work_dir,
                    progress,
                )
                return DownloadResult(file_path=file_path, title=media.title, output_kind=option.output_kind)
            except Exception as exc:
                remove_tree(work_dir)
                if isinstance(exc, DownloadError):
                    raise
                raise DownloadError(f"Download failed: {exc}") from exc

    def _download_sync(
        self,
        url: str,
        title: str,
        option: FormatOption,
        work_dir: Path,
        progress: ProgressState,
    ) -> Path:
        if option.requires_ffmpeg and not self._ffmpeg_path:
            raise DownloadError(
                "ffmpeg is not installed or not available in PATH. "
                "Run pip install -r requirements.txt, then restart the bot. "
                "480p, 720p, 1080p, and MP3 downloads require ffmpeg to merge/convert media."
            )

        safe_title = sanitize_filename(title)
        extension = "mp3" if option.output_kind == "audio" else "mp4"
        output_template = str(work_dir / f"{safe_title}.%(ext)s")

        def hook(data: dict) -> None:
            status = data.get("status")
            if status == "downloading":
                progress.update_from_thread(
                    status="downloading",
                    percent=percent_from_hook(data),
                    downloaded_bytes=int(data.get("downloaded_bytes") or 0),
                    total_bytes=data.get("total_bytes") or data.get("total_bytes_estimate"),
                    speed=data.get("speed"),
                    eta=data.get("eta"),
                )
            elif status == "finished":
                progress.update_from_thread(status="processing", percent=100.0)

        ydl_opts = self._base_ydl_opts()
        if self._ffmpeg_path:
            ydl_opts["ffmpeg_location"] = self._ffmpeg_path

        ydl_opts.update(
            {
                "format": option.format_selector,
                "outtmpl": output_template,
                "noplaylist": True,
                "progress_hooks": [hook],
                "merge_output_format": "mp4",
                "postprocessors": self._postprocessors(option),
                "postprocessor_args": self._postprocessor_args(option),
            }
        )

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as exc:
            raise DownloadError(str(exc)) from exc

        output_files = sorted(work_dir.glob(f"*.{extension}"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not output_files:
            raise DownloadError("The download finished, but the output file was not created.")

        final_path = output_files[0]
        progress.update_from_thread(status="downloaded", percent=100.0, finished_path=final_path)
        return final_path

    def _build_format_options(self, formats: list[dict]) -> dict[str, FormatOption]:
        options: dict[str, FormatOption] = {}
        available_video_heights = {
            height
            for fmt in formats
            if fmt.get("vcodec") not in (None, "none")
            for height in [self._format_height(fmt)]
            if height
        }
        max_video_height = max(available_video_heights, default=0)

        for resolution in SUPPORTED_RESOLUTIONS:
            progressive = self._best_progressive_mp4(formats, resolution)
            if progressive:
                fmt_id = str(progressive["format_id"])
                options[str(resolution)] = FormatOption(
                    key=str(resolution),
                    label=f"{resolution}p MP4",
                    format_selector=fmt_id,
                    output_kind="video",
                    estimated_size=estimate_format_size(formats, [fmt_id]),
                )
                continue

            video = self._best_video_mp4(formats, resolution) or self._best_video_any(formats, resolution)
            if not video and max_video_height >= resolution:
                options[str(resolution)] = FormatOption(
                    key=str(resolution),
                    label=f"{resolution}p MP4",
                    format_selector=self._adaptive_selector(resolution),
                    output_kind="video",
                )
                continue

            if video:
                video_id = str(video["format_id"])
                options[str(resolution)] = FormatOption(
                    key=str(resolution),
                    label=f"{resolution}p MP4",
                    format_selector=self._adaptive_selector(resolution, video_id),
                    output_kind="video",
                    estimated_size=estimate_format_size(formats, [video_id]),
                )

        if self._has_audio(formats):
            options["audio"] = FormatOption(
                key="audio",
                label="Audio MP3",
                format_selector="bestaudio/best",
                output_kind="audio",
            )

        return options

    @staticmethod
    def _best_progressive_mp4(formats: list[dict], height: int) -> dict | None:
        candidates = [
            fmt
            for fmt in formats
            if Downloader._format_height(fmt) == height
            and fmt.get("ext") == "mp4"
            and fmt.get("vcodec") not in (None, "none")
            and fmt.get("acodec") not in (None, "none")
        ]
        return Downloader._best_by_quality(candidates)

    @staticmethod
    def _best_video_mp4(formats: list[dict], height: int) -> dict | None:
        candidates = [
            fmt
            for fmt in formats
            if Downloader._format_height(fmt) == height
            and fmt.get("ext") == "mp4"
            and fmt.get("vcodec") not in (None, "none")
        ]
        return Downloader._best_by_quality(candidates)

    @staticmethod
    def _best_video_any(formats: list[dict], height: int) -> dict | None:
        candidates = [
            fmt
            for fmt in formats
            if Downloader._format_height(fmt) == height and fmt.get("vcodec") not in (None, "none")
        ]
        return Downloader._best_by_quality(candidates)

    @staticmethod
    def _adaptive_selector(height: int, video_id: str | None = None) -> str:
        exact_mp4 = f"bestvideo[height={height}][ext=mp4]+bestaudio[ext=m4a]"
        exact_any_audio = f"bestvideo[height={height}][ext=mp4]+bestaudio"
        exact_any_video = f"bestvideo[height={height}]+bestaudio"
        progressive = f"best[height={height}][ext=mp4]/best[height={height}]"

        if video_id:
            return (
                f"{video_id}+bestaudio[ext=m4a]/"
                f"{video_id}+bestaudio/"
                f"{exact_mp4}/"
                f"{exact_any_audio}/"
                f"{exact_any_video}/"
                f"{progressive}"
            )

        return f"{exact_mp4}/{exact_any_audio}/{exact_any_video}/{progressive}"

    @staticmethod
    def _format_height(fmt: dict) -> int | None:
        height = fmt.get("height")
        if isinstance(height, int):
            return height

        resolution = fmt.get("resolution")
        if isinstance(resolution, str) and "x" in resolution:
            maybe_height = resolution.rsplit("x", 1)[-1]
            if maybe_height.isdigit():
                return int(maybe_height)

        format_note = fmt.get("format_note")
        if isinstance(format_note, str):
            match = re.search(r"(\d{3,4})p", format_note)
            if match:
                return int(match.group(1))

        return None

    @staticmethod
    def _best_by_quality(candidates: list[dict]) -> dict | None:
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda fmt: (
                fmt.get("quality") or 0,
                fmt.get("tbr") or 0,
                fmt.get("vbr") or 0,
                fmt.get("filesize") or fmt.get("filesize_approx") or 0,
            ),
        )

    @staticmethod
    def _has_audio(formats: list[dict]) -> bool:
        return any(fmt.get("acodec") not in (None, "none") for fmt in formats)

    @staticmethod
    def _base_ydl_opts() -> dict:
        return {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 4,
            "http_chunk_size": 10 * 1024 * 1024,
            "prefer_ffmpeg": True,
            "geo_bypass": True,
        }

    @staticmethod
    def _find_ffmpeg() -> str | None:
        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg:
            return system_ffmpeg

        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None

    @staticmethod
    def _postprocessors(option: FormatOption) -> list[dict]:
        if option.output_kind == "audio":
            return [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]

        return [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ]

    @staticmethod
    def _postprocessor_args(option: FormatOption) -> dict:
        if option.output_kind == "audio":
            return {"ffmpeg": ["-vn"]}
        return {"ffmpeg": ["-movflags", "+faststart"]}
