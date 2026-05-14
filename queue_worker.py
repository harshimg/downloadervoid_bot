from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from telegram import InputFile
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application

from config import CONFIG
from downloader import Downloader, FormatOption, MediaInfo
from utils import ProgressState, human_size, progress_bar, remove_tree


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadJob:
    chat_id: int
    message_id: int
    media: MediaInfo
    option: FormatOption
    use_caption: bool = False


class DownloadQueue:
    def __init__(self, downloader: Downloader) -> None:
        self.downloader = downloader
        self.queue: asyncio.Queue[DownloadJob] = asyncio.Queue()
        self.workers: list[asyncio.Task] = []

    def start(self, application: Application, worker_count: int = CONFIG.queue_workers) -> None:
        for index in range(worker_count):
            task = asyncio.create_task(self._worker(application, index + 1), name=f"download-worker-{index + 1}")
            self.workers.append(task)

    async def stop(self) -> None:
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()

    async def enqueue(self, job: DownloadJob) -> int:
        await self.queue.put(job)
        return self.queue.qsize()

    async def _worker(self, application: Application, worker_id: int) -> None:
        logger.info("Download worker %s started", worker_id)
        while True:
            job = await self.queue.get()
            try:
                await self._run_job(application, job)
            except Exception:
                logger.exception("Unhandled download job failure")
            finally:
                self.queue.task_done()

    async def _run_job(self, application: Application, job: DownloadJob) -> None:
        progress = ProgressState(loop=asyncio.get_running_loop())
        reporter = asyncio.create_task(self._progress_reporter(application, job, progress))
        result_path: Path | None = None

        try:
            result = await self.downloader.download(job.media, job.option, progress)
            result_path = result.file_path
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)

            await self._safe_edit(application, job, "📤 Uploading...")
            await self._send_document(application, job, result.file_path, result.title)
            await self._safe_edit(application, job, "✅ Done.")
        except Exception as exc:
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)
            logger.exception("Download failed")
            await self._safe_edit(application, job, f"❌ Failed:\n{str(exc)[:350]}")
        finally:
            if result_path:
                remove_tree(result_path.parent)

    async def _progress_reporter(self, application: Application, job: DownloadJob, progress: ProgressState) -> None:
        last_text = ""
        while True:
            snapshot = await progress.snapshot()
            percent = float(snapshot["percent"])
            text = f"⏳ Downloading...\n\n{progress_bar(percent)}"
            if snapshot["total_bytes"]:
                text += f"\n{human_size(snapshot['downloaded_bytes'])} / {human_size(snapshot['total_bytes'])}"

            if text != last_text:
                await self._safe_edit(application, job, text)
                last_text = text

            await asyncio.sleep(CONFIG.progress_interval_seconds)

    async def _send_document(self, application: Application, job: DownloadJob, file_path: Path, title: str) -> None:
        file_size = file_path.stat().st_size
        if file_size > CONFIG.max_upload_mb * 1024 * 1024:
            raise RuntimeError(
                f"File is {human_size(file_size)}, which is above the configured {CONFIG.max_upload_mb} MB upload limit."
            )

        caption = title[:1024]
        for attempt in range(1, 4):
            try:
                with file_path.open("rb") as file_obj:
                    await application.bot.send_document(
                        chat_id=job.chat_id,
                        document=InputFile(file_obj, filename=file_path.name),
                        caption=caption,
                        read_timeout=900,
                        write_timeout=900,
                        connect_timeout=120,
                        pool_timeout=120,
                    )
                return
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 1)
            except (TimedOut, NetworkError) as exc:
                if attempt == 3:
                    raise RuntimeError(f"Telegram upload failed after retries: {exc}") from exc
                await asyncio.sleep(3 * attempt)
            except (BadRequest, Forbidden):
                raise

    async def _safe_edit(self, application: Application, job: DownloadJob, text: str) -> None:
        try:
            if job.use_caption:
                await application.bot.edit_message_caption(
                    chat_id=job.chat_id,
                    message_id=job.message_id,
                    caption=text,
                    read_timeout=60,
                    write_timeout=60,
                    connect_timeout=30,
                    pool_timeout=30,
                )
            else:
                await application.bot.edit_message_text(
                    chat_id=job.chat_id,
                    message_id=job.message_id,
                    text=text,
                    read_timeout=60,
                    write_timeout=60,
                    connect_timeout=30,
                    pool_timeout=30,
                )
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                logger.warning("Could not edit progress message: %s", exc)
        except (TimedOut, NetworkError) as exc:
            logger.warning("Transient progress edit failure: %s", exc)
        except Forbidden:
            logger.warning("Bot cannot edit message in chat %s", job.chat_id)
