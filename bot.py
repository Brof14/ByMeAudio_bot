"""Telegram entry point for ByMeAudio.

Run with: python bot.py
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    AIORateLimiter,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import Settings
from app.formatting import format_duration, split_html_text
from app.queueing import JobGate
from app.storage import StatsRepository
from app.transcription import AudioTooLong, Transcriber

logger = logging.getLogger("bymeaudio")

AUDIO_EXTS = (".mp3", ".ogg", ".wav", ".m4a", ".flac", ".aac", ".oga", ".opus", ".amr", ".wma")
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".mpg", ".mpeg", ".3gp")
LONG_TRANSCRIPT_THRESHOLD = 9500


def setup_logging(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for name in ("httpx", "httpcore", "telegram", "telegram.ext", "apscheduler"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def detect_media(msg):
    if msg.voice:
        return msg.voice, "voice", "Голосовое сообщение", "🎙", msg.voice.duration or 0
    if msg.video_note:
        return msg.video_note, "video_note", "Видеокружок", "📹", msg.video_note.duration or 0
    if msg.audio:
        return msg.audio, "audio", "Аудиозапись", "🎵", msg.audio.duration or 0
    if msg.video:
        return msg.video, "video", "Видеофайл", "🎬", msg.video.duration or 0
    if msg.document:
        doc = msg.document
        mime = (doc.mime_type or "").lower()
        filename = (doc.file_name or "").lower()
        is_audio = mime.startswith("audio/") or filename.endswith(AUDIO_EXTS)
        is_video = mime.startswith("video/") or filename.endswith(VIDEO_EXTS)
        if is_audio:
            return doc, "audio", "Аудиофайл", "🎵", 0
        if is_video:
            return doc, "video", "Видеофайл", "🎬", 0
    return None


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Как пользоваться", callback_data="help"),
         InlineKeyboardButton("Моя статистика", callback_data="stats")],
        [InlineKeyboardButton("Конфиденциальность", callback_data="privacy")],
    ])


def back_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← Главное меню", callback_data="menu")]
    ])


WELCOME = (
    "<b>ByMeAudio — голос и видео в текст.</b>\n\n"
    "Отправьте голосовое сообщение, кружок, аудио или видео — "
    "бот распознает речь и пришлёт текст.\n\n"
    "Распознавание выполняется локально на сервере. Бесплатно."
)

HELP = (
    "<b>Как пользоваться</b>\n\n"
    "1. Отправьте или перешлите голосовое сообщение, кружок, аудио или видео.\n"
    "2. Дождитесь окончания обработки — при нагрузке запрос попадёт в очередь.\n"
    "3. Получите текст; длинную расшифровку бот отправит файлом.\n\n"
    "Ограничения размера и длительности указаны при отправке.\n"
    "Команды: /start, /help, /stats, /privacy, /delete_me."
)
PRIVACY = (
    "<b>Конфиденциальность</b>\n\n"
    "Аудио обрабатывается локально, без внешнего сервиса распознавания. "
    "Бот сохраняет Telegram ID, имя, username, дату активности и счётчики обработок. "
    "Само аудио и расшифровки в пользовательской базе не сохраняются.\n\n"
    "Передача файлов происходит через Telegram. В технических логах могут "
    "временно сохраняться идентификаторы запросов и сообщения об ошибках.\n"
    "Удалить свою статистику: /delete_me."
)


class AudioBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = StatsRepository(settings.db_path)
        self.db.initialize()
        self.gate = JobGate(settings.max_pending_jobs, settings.user_cooldown_seconds)
        self.transcriber = Transcriber(
            settings.whisper_model,
            settings.whisper_beam_size,
            settings.whisper_cpu_threads,
            settings.transcription_language,
            settings.max_duration_seconds,
        )

    def remember(self, user) -> None:
        self.db.ensure_user(user.id, user.username, user.first_name)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.remember(update.effective_user)
        await update.effective_message.reply_text(
            WELCOME, parse_mode=ParseMode.HTML, reply_markup=main_menu()
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            HELP, parse_mode=ParseMode.HTML, reply_markup=back_menu()
        )

    def stats_text(self, user_id: int, is_admin: bool) -> str:
        stats = self.db.user_stats(user_id)
        text = (
            "<b>Моя статистика</b>\n\n"
            f"Голосовых: <b>{stats.get('total_voice', 0)}</b>\n"
            f"Видео и кружков: <b>{stats.get('total_video', 0)}</b>\n"
            f"Аудиофайлов: <b>{stats.get('total_audio', 0)}</b>\n"
            f"Всего расшифровок: <b>{stats.get('total_conversions', 0)}</b>\n"
            f"Первое использование: <code>{stats.get('created_at', '—') or '—'}</code>"
        )
        if is_admin:
            global_stats = self.db.global_stats()
            text += (
                "\n\n<b>Статистика сервиса</b>\n"
                f"Пользователей: <b>{global_stats['users']}</b>\n"
                f"Расшифровок: <b>{global_stats['total']}</b>"
            )
        return text

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        self.remember(user)
        await update.effective_message.reply_text(
            self.stats_text(user.id, user.id in self.settings.admin_ids),
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu(),
        )

    async def privacy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            PRIVACY, parse_mode=ParseMode.HTML, reply_markup=back_menu()
        )

    async def delete_me(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Удалить статистику", callback_data="delete_confirm")],
            [InlineKeyboardButton("Отмена", callback_data="menu")],
        ])
        await update.effective_message.reply_text(
            "Удалить ваши имя, username и сохранённую статистику? "
            "Это действие невозможно отменить.",
            reply_markup=keyboard,
        )

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        user = query.from_user
        if query.data == "menu":
            await query.edit_message_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=main_menu())
        elif query.data == "help":
            await query.edit_message_text(HELP, parse_mode=ParseMode.HTML, reply_markup=back_menu())
        elif query.data == "stats":
            self.remember(user)
            await query.edit_message_text(
                self.stats_text(user.id, user.id in self.settings.admin_ids),
                parse_mode=ParseMode.HTML, reply_markup=back_menu(),
            )
        elif query.data == "privacy":
            await query.edit_message_text(PRIVACY, parse_mode=ParseMode.HTML, reply_markup=back_menu())
        elif query.data == "delete_confirm":
            self.db.delete_user(user.id)
            await query.edit_message_text(
                "Сохранённая статистика удалена. При следующем использовании бот создаст новую запись.",
                reply_markup=back_menu(),
            )

    async def handle_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        if not message or not user:
            return

        detected = detect_media(message)
        if not detected:
            if message.document:
                await message.reply_text(
                    "Неподдерживаемый формат. Пришлите аудио (MP3, OGG, WAV, M4A) "
                    "или видео (MP4, MOV, MKV)."
                )
            return
        file_obj, kind, title, icon, reported_duration = detected
        max_bytes = self.settings.max_file_size_mb * 1024 * 1024
        if (file_obj.file_size or 0) > max_bytes:
            await message.reply_text(
                f"Файл превышает лимит {self.settings.max_file_size_mb} МБ."
            )
            return
        if reported_duration > self.settings.max_duration_seconds:
            await message.reply_text(
                f"Запись слишком длинная. Максимум: "
                f"{format_duration(self.settings.max_duration_seconds)}."
            )
            return

        admission = await self.gate.admit(user.id)
        if not admission.accepted:
            await message.reply_text(admission.message)
            return

        status = None
        try:
            self.remember(user)
            status = await message.reply_text(
                "Запись в очереди. Ожидаю свободный слот..." if admission.position
                else "Файл получен. Подготавливаю звук..."
            )
            # A single processing slot covers BOTH downloading and decoding:
            # waiting requests cannot each hold a full file in memory.
            async with self.gate.processing_slot():
                if admission.position:
                    await status.edit_text("Подготавливаю звук...")
                await context.bot.send_chat_action(
                    chat_id=message.chat_id, action=ChatAction.TYPING
                )
                telegram_file = await file_obj.get_file()
                if (telegram_file.file_size or 0) > max_bytes:
                    await status.edit_text("Файл превышает допустимый размер.")
                    return
                data = await telegram_file.download_as_bytearray()
                if len(data) > max_bytes:
                    await status.edit_text("Файл превышает допустимый размер.")
                    return
                await status.edit_text("Распознаю речь. Это может занять некоторое время...")
                started = time.perf_counter()
                transcript = await asyncio.to_thread(
                    self.transcriber.transcribe_bytes, bytes(data)
                )
                del data
                elapsed = time.perf_counter() - started

            if not transcript.text:
                await status.edit_text(
                    "Не удалось распознать речь. Возможно, запись содержит тишину, "
                    "музыку или сильный шум."
                )
                return

            footer = (
                f"Длительность: {format_duration(transcript.duration_seconds)}"
                f" · Обработка: {elapsed:.1f} с"
            )
            chunks = split_html_text(transcript.text, 3300)
            if len(transcript.text) > LONG_TRANSCRIPT_THRESHOLD:
                await status.edit_text(
                    f"{icon} <b>{html.escape(title)}</b>\n\n"
                    f"{chunks[0]}\n\n"
                    "…\n\nПолный текст — в прикреплённом файле.\n"
                    f"<i>{html.escape(footer)}</i>",
                    parse_mode=ParseMode.HTML,
                )
                document = io.BytesIO(transcript.text.encode("utf-8"))
                document.name = f"bymeaudio_{int(time.time())}.txt"
                await message.reply_document(document, caption="Полная расшифровка")
            else:
                await status.edit_text(
                    f"{icon} <b>{html.escape(title)}</b>\n\n"
                    f"{chunks[0]}\n\n<i>{html.escape(footer)}</i>",
                    parse_mode=ParseMode.HTML,
                )
                for part in chunks[1:]:
                    await message.reply_text(part, parse_mode=ParseMode.HTML)

            # Count an operation only after all output was delivered.
            self.db.record_conversion(user.id, user.username, user.first_name, kind)
            logger.info("Transcription delivered uid=%s kind=%s seconds=%.1f",
                        user.id, kind, transcript.duration_seconds)
        except AudioTooLong:
            if status:
                await self._safe_status(status, "Аудио слишком длинное для обработки.")
        except TelegramError:
            logger.exception("Telegram delivery/download error uid=%s", user.id)
            if status:
                await self._safe_status(status, "Не удалось загрузить или отправить файл. Повторите попытку.")
        except Exception:
            logger.exception("Transcription error uid=%s", user.id)
            if status:
                await self._safe_status(status, "Ошибка обработки аудио. Повторите попытку позже.")
        finally:
            await self.gate.release(user.id)

    @staticmethod
    async def _safe_status(message, text: str) -> None:
        try:
            await message.edit_text(text)
        except TelegramError:
            logger.warning("Could not edit failed request status")

    async def broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user.id not in self.settings.admin_ids:
            return
        if not context.args:
            await update.effective_message.reply_text("Использование: /broadcast <текст>")
            return
        content = update.effective_message.text.split(maxsplit=1)[1]
        if len(content) > 4096:
            await update.effective_message.reply_text("Текст рассылки превышает лимит Telegram.")
            return
        recipients = self.db.user_ids()
        await update.effective_message.reply_text(
            f"Начинаю отправку {len(recipients)} пользователям."
        )
        success = failed = 0
        for recipient in recipients:
            try:
                # Plain text: arbitrary input must not be parsed as HTML.
                await context.bot.send_message(chat_id=recipient, text=content)
                success += 1
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 0.5)
                try:
                    await context.bot.send_message(chat_id=recipient, text=content)
                    success += 1
                except TelegramError:
                    failed += 1
            except TelegramError:
                failed += 1
            await asyncio.sleep(0.05)
        await update.effective_message.reply_text(
            f"Рассылка завершена. Отправлено: {success}. Ошибок: {failed}."
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if error:
        logger.error("Uncaught update error: %s", error,
                     exc_info=(type(error), error, error.__traceback__))


def main() -> None:
    load_dotenv()
    settings = Settings.from_env()
    setup_logging(settings.log_path)
    service = AudioBot(settings)
    threading.Thread(
        target=service.transcriber.warmup, daemon=True, name="whisper-warmup"
    ).start()

    app = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .rate_limiter(AIORateLimiter(overall_max_rate=30, overall_time_period=1, max_retries=3))
        .concurrent_updates(8)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", service.start, filters=private))
    app.add_handler(CommandHandler("help", service.help, filters=private))
    app.add_handler(CommandHandler("stats", service.stats, filters=private))
    app.add_handler(CommandHandler("privacy", service.privacy, filters=private))
    app.add_handler(CommandHandler("delete_me", service.delete_me, filters=private))
    app.add_handler(CommandHandler("broadcast", service.broadcast, filters=private))
    media = (
        filters.VOICE | filters.VIDEO_NOTE | filters.AUDIO
        | filters.VIDEO | filters.Document.ALL
    )
    app.add_handler(MessageHandler(media & private, service.handle_media))
    app.add_handler(CallbackQueryHandler(service.callback))
    app.add_error_handler(on_error)
    logger.info("ByMeAudio configured: model=%s, max_pending=%s",
                settings.whisper_model, settings.max_pending_jobs)
    try:
        # Do not silently discard messages received during a brief restart.
        app.run_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
        )
    except KeyboardInterrupt:
        logger.info("Stopped by user")
    except Exception:
        logger.exception("Fatal bot error")
        raise  # A non-zero exit status enables service supervisors to restart.
    finally:
        service.db.close()


if __name__ == "__main__":
    main()
