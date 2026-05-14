from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import CONFIG
from downloader import Downloader, FormatOption, MediaInfo
from queue_worker import DownloadJob, DownloadQueue
from utils import extract_url, human_size, is_probable_url


logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


@dataclass
class CachedMedia:
    media: MediaInfo
    created_at: float


downloader = Downloader()
download_queue = DownloadQueue(downloader)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "Send me a video URL from YouTube, Instagram, TikTok, X, Facebook, or any yt-dlp supported site."
        )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    url = extract_url(update.message.text)
    if not url or not is_probable_url(url):
        await update.message.reply_text("Please send a valid video URL.")
        return

    status_message = await update.message.reply_text("🔎 Fetching formats...")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        media = await downloader.extract_info(url)
    except Exception as exc:
        logger.exception("Metadata extraction failed")
        await status_message.edit_text(f"❌ Could not fetch formats:\n{str(exc)[:350]}")
        return

    cache_id = uuid.uuid4().hex[:12]
    media_cache: dict[str, CachedMedia] = context.application.bot_data.setdefault("media_cache", {})
    media_cache[cache_id] = CachedMedia(media=media, created_at=time.monotonic())
    prune_cache(media_cache)

    keyboard = build_format_keyboard(cache_id, media.options)
    caption = build_preview_caption(media)

    try:
        if media.thumbnail:
            await status_message.delete()
            await update.message.reply_photo(
                photo=media.thumbnail,
                caption=caption,
                reply_markup=keyboard,
                read_timeout=60,
                write_timeout=60,
                connect_timeout=30,
                pool_timeout=30,
            )
        else:
            await status_message.edit_text(caption, reply_markup=keyboard)
    except (BadRequest, TimedOut, NetworkError):
        logger.exception("Thumbnail preview failed; falling back to text preview")
        await status_message.edit_text(caption, reply_markup=keyboard)


async def handle_format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return

    await query.answer()
    payload = query.data or ""
    parts = payload.split(":")
    is_photo_preview = bool(query.message.photo)
    if len(parts) != 3 or parts[0] != "fmt":
        await edit_callback_message(query, is_photo_preview, "This selection is invalid. Please send the URL again.")
        return

    _, cache_id, option_key = parts
    media_cache: dict[str, CachedMedia] = context.application.bot_data.setdefault("media_cache", {})
    cached = media_cache.get(cache_id)
    if not cached or time.monotonic() - cached.created_at > CONFIG.url_cache_ttl_seconds:
        await edit_callback_message(query, is_photo_preview, "This selection expired. Please send the URL again.")
        return

    option = cached.media.options.get(option_key)
    if not option:
        await edit_callback_message(query, is_photo_preview, "That format is no longer available. Please send the URL again.")
        return

    initial_progress = "⏳ Downloading...\n\n🟩⬜⬜⬜⬜⬜⬜⬜⬜⬜\n0%"
    await edit_callback_message(query, is_photo_preview, initial_progress)

    position = await download_queue.enqueue(
        DownloadJob(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            media=cached.media,
            option=option,
            use_caption=is_photo_preview,
        )
    )

    if position > CONFIG.queue_workers:
        queued_text = f"⏳ Queued at position {position - CONFIG.queue_workers}..."
        if is_photo_preview:
            await context.bot.edit_message_caption(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                caption=queued_text,
            )
        else:
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                text=queued_text,
            )


async def edit_callback_message(query, use_caption: bool, text: str) -> None:
    if use_caption:
        await query.edit_message_caption(caption=text)
    else:
        await query.edit_message_text(text)


def build_format_keyboard(cache_id: str, options: dict[str, FormatOption]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []

    for key in ("360", "480", "720", "1080"):
        option = options.get(key)
        if not option:
            continue
        label = option.label
        if option.estimated_size:
            label = f"{label} · {human_size(option.estimated_size)}"
        current_row.append(InlineKeyboardButton(label, callback_data=f"fmt:{cache_id}:{option.key}"))
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

    audio = options.get("audio")
    if audio:
        rows.append([InlineKeyboardButton(audio.label, callback_data=f"fmt:{cache_id}:{audio.key}")])

    return InlineKeyboardMarkup(rows)


def build_preview_caption(media: MediaInfo) -> str:
    title = media.title[:180]
    lines = [f"🎬 {title}", "", "Choose a format:"]
    return "\n".join(lines)


def prune_cache(media_cache: dict[str, CachedMedia]) -> None:
    now = time.monotonic()
    expired = [
        cache_id
        for cache_id, cached in media_cache.items()
        if now - cached.created_at > CONFIG.url_cache_ttl_seconds
    ]
    for cache_id in expired:
        media_cache.pop(cache_id, None)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Telegram update failed", exc_info=context.error)


async def post_init(application: Application) -> None:
    download_queue.start(application)
    logger.info("DownloaderVoid_bot started with %s worker(s)", CONFIG.queue_workers)


async def post_shutdown(application: Application) -> None:
    await download_queue.stop()


def build_application() -> Application:
    return (
        ApplicationBuilder()
        .token(CONFIG.bot_token)
        .connect_timeout(60)
        .read_timeout(900)
        .write_timeout(900)
        .pool_timeout(120)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )


def main() -> None:
    application = build_application()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    application.add_handler(CallbackQueryHandler(handle_format_callback, pattern=r"^fmt:"))
    application.add_error_handler(error_handler)
    application.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)


if __name__ == "__main__":
    main()
