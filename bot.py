import os
import sys
import time
import html
import logging
import sqlite3
import tempfile
import asyncio
import threading
import glob
import wave
import io
from contextlib import contextmanager

from dotenv import load_dotenv
import requests

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    AIORateLimiter,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import Conflict, TelegramError

# ══════════════════════════════════════════════════════════
#  ЛОГИРОВАНИЕ
# ══════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("audio_bot")

# ══════════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════════

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в файле .env")

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip() or None

MAX_FILE_SIZE_MB = 20  # Ограничение Telegram Bot API на getFile
DB_PATH = "audio_bot.db"

# ══════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        # Создание таблицы пользователей
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id                 INTEGER PRIMARY KEY,
                username                TEXT,
                first_name              TEXT,
                total_conversions       INTEGER DEFAULT 0,
                total_voice             INTEGER DEFAULT 0,
                total_video             INTEGER DEFAULT 0,
                total_audio             INTEGER DEFAULT 0,
                created_at              TEXT    DEFAULT CURRENT_TIMESTAMP,
                last_active             TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Миграция колонок, если таблица была создана ранее в старой версии
        cursor = conn.execute("PRAGMA table_info(users)")
        existing_cols = {row["name"] for row in cursor.fetchall()}

        needed_cols = {
            "first_name": "TEXT",
            "total_conversions": "INTEGER DEFAULT 0",
            "total_voice": "INTEGER DEFAULT 0",
            "total_video": "INTEGER DEFAULT 0",
            "total_audio": "INTEGER DEFAULT 0",
            "last_active": "TEXT",
        }

        for col_name, col_def in needed_cols.items():
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
                logger.info("Миграция БД: добавлена колонка %s", col_name)

        # Удаление неиспользуемой таблицы транзакций от оплат
        conn.execute("DROP TABLE IF EXISTS transactions")

    logger.info("База данных инициализирована успешно.")


def ensure_user(user_id: int, username: str | None, first_name: str | None) -> dict:
    username = username or ""
    first_name = first_name or ""
    with get_db() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name, last_active)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_active = CURRENT_TIMESTAMP
        """, (user_id, username, first_name))
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else {}


def record_conversion(user_id: int, username: str | None, first_name: str | None, media_type: str):
    v_voice = 1 if media_type == "voice" else 0
    v_video = 1 if media_type == "video" else 0
    v_audio = 1 if media_type == "audio" else 0
    username = username or ""
    first_name = first_name or ""

    with get_db() as conn:
        conn.execute("""
            INSERT INTO users (
                user_id, username, first_name,
                total_conversions, total_voice, total_video, total_audio,
                last_active
            )
            VALUES (?, ?, ?, 1, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                total_conversions = total_conversions + 1,
                total_voice = total_voice + excluded.total_voice,
                total_video = total_video + excluded.total_video,
                total_audio = total_audio + excluded.total_audio,
                last_active = CURRENT_TIMESTAMP
        """, (user_id, username, first_name, v_voice, v_video, v_audio))


def get_user_stats(user_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else {}


def get_global_stats() -> dict:
    with get_db() as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total = conn.execute("SELECT COALESCE(SUM(total_conversions), 0) FROM users").fetchone()[0]
        voice = conn.execute("SELECT COALESCE(SUM(total_voice), 0) FROM users").fetchone()[0]
        video = conn.execute("SELECT COALESCE(SUM(total_video), 0) FROM users").fetchone()[0]
        audio = conn.execute("SELECT COALESCE(SUM(total_audio), 0) FROM users").fetchone()[0]
        return {
            "users": users,
            "total": total,
            "voice": voice,
            "video": video,
            "audio": audio,
        }


def get_all_user_ids() -> list[int]:
    with get_db() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
        return [row["user_id"] for row in rows]


# ══════════════════════════════════════════════════════════
#  КОНВЕРТАЦИЯ МЕДИА (FFmpeg)
# ══════════════════════════════════════════════════════════

async def extract_audio_to_wav(input_path: str, output_wav_path: str) -> bool:
    """
    Быстрое неблокирующее извлечение звука через FFmpeg.
    Конвертирует любой входящий формат (.ogg, .mp4, .mp3, .m4a, .mov и т.д.)
    в 16kHz mono 16-bit PCM WAV — идеальный формат для распознавания речи.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vn",                   # отключаем видеопоток
        "-acodec", "pcm_s16le",  # PCM 16-bit
        "-ar", "16000",          # 16 kHz
        "-ac", "1",              # моно
        output_wav_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("Ошибка FFmpeg (код %d): %s", proc.returncode, stderr.decode(errors="ignore"))
            return False
        return True
    except Exception as e:
        logger.error("Исключение при вызове FFmpeg: %s", e)
        return False


def get_wav_duration(wav_path: str) -> float:
    """Быстро получает длительность WAV из заголовка."""
    try:
        with wave.open(wav_path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return frames / float(rate) if rate > 0 else 0.0
    except Exception:
        return 0.0


def format_duration(seconds: float | int) -> str:
    """Форматирует длительность в формат MM:SS или HH:MM:SS."""
    seconds = int(round(seconds))
    if seconds < 0:
        seconds = 0
    mins = seconds // 60
    secs = seconds % 60
    if mins >= 60:
        hours = mins // 60
        mins = mins % 60
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


# ══════════════════════════════════════════════════════════
#  ДВИЖОК РАСПОЗНАВАНИЯ РЕЧИ (Transcriber)
# ══════════════════════════════════════════════════════════

class SpeechTranscriber:
    """
    Интеллектуальный транскрибер:
    1. Groq Cloud API (whisper-large-v3-turbo, ~0.3-0.8с, пунктуация) — если задан GROQ_API_KEY
    2. Faster-Whisper Base (локально на CPU int8, ~1-2с, оффлайн, пунктуация, без ограничений длины)
    3. Google Speech API (бесплатный сетевой резерв с нарезкой длинных фрагментов)
    """

    def __init__(self, groq_api_key: str | None = None):
        self.groq_api_key = groq_api_key
        self.groq_client = None
        if self.groq_api_key:
            try:
                from groq import Groq
                self.groq_client = Groq(api_key=self.groq_api_key)
                logger.info("⚡ Groq API успешно инициализирован (Whisper Large v3 Turbo)")
            except Exception as e:
                logger.warning("Не удалось инициализировать Groq client: %s", e)

        self._local_model = None
        self._model_lock = threading.Lock()

    def get_local_model(self):
        """Ленивая загрузка локальной модели Faster-Whisper."""
        with self._model_lock:
            if self._local_model is not None:
                return self._local_model

            from faster_whisper import WhisperModel

            snapshots = glob.glob(
                os.path.expanduser("~/.cache/huggingface/hub/models--Systran--faster-whisper-base/snapshots/*")
            )
            if snapshots:
                cached_path = snapshots[0]
                logger.info("Загрузка Faster-Whisper из локального кэша: %s", cached_path)
                self._local_model = WhisperModel(
                    cached_path,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=4,
                    local_files_only=True,
                )
            else:
                logger.info("Загрузка Faster-Whisper base модели...")
                self._local_model = WhisperModel(
                    "base",
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=4,
                )
            return self._local_model

    def transcribe(self, wav_path: str) -> tuple[str, str]:
        """
        Выполняет распознавание аудио.
        Возвращает (текст, название_движка).
        """
        # ── 1. Groq Cloud Whisper (быстрейший и самый точный) ──
        if self.groq_client:
            try:
                with open(wav_path, "rb") as f:
                    transcription = self.groq_client.audio.transcriptions.create(
                        file=f,
                        model="whisper-large-v3-turbo",
                        response_format="json",
                        temperature=0.0,
                    )
                text = transcription.text.strip()
                if text:
                    return text, "Groq Whisper Large"
            except Exception as e:
                logger.warning("Groq API вернул ошибку, переключаюсь на локальный Whisper: %s", e)

        # ── 2. Faster-Whisper (локально, бесплатно, качественно) ──
        try:
            model = self.get_local_model()
            segments, info = model.transcribe(
                wav_path,
                beam_size=5,
                condition_on_previous_text=False,
                vad_filter=False,  # отключено для избежания сетевых запросов к silero
                no_speech_threshold=0.6,
            )
            parts = []
            for s in segments:
                if s.no_speech_prob < 0.65:
                    clean = s.text.strip()
                    if clean.lower() not in {"you", "you.", "thank you", "thank you.", "thanks.", "thanks", "[blank_audio]"}:
                        parts.append(clean)

            text = " ".join(parts).strip()
            if text:
                lang_tag = f"Faster-Whisper ({info.language})"
                return text, lang_tag
        except Exception as e:
            logger.warning("Локальный Whisper вернул ошибку: %s. Переключаюсь на Google Speech.", e)

        # ── 3. Google Speech API (резервный) ──
        try:
            import speech_recognition as sr
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_path) as source:
                audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data, language="ru-RU")
            if text:
                return text.strip(), "Google Speech"
        except Exception as e:
            logger.info("Google Speech не распознал речь (тишина или ошибка): %s", e)

        return "", "None"


transcriber = SpeechTranscriber(groq_api_key=GROQ_API_KEY)


# ══════════════════════════════════════════════════════════
#  ФОРМАТИРОВАНИЕ И РАЗБИВКА СООБЩЕНИЙ
# ══════════════════════════════════════════════════════════

def split_message_text(text: str, max_chunk_size: int = 3800) -> list[str]:
    """
    Разбивает длинный текст на части не более max_chunk_size,
    сохраняя целостность абзацев и предложений.
    """
    if len(text) <= max_chunk_size:
        return [text]

    chunks = []
    current_chunk = []
    current_length = 0

    paragraphs = text.split("\n\n")
    for para in paragraphs:
        para_len = len(para) + 2
        if current_length + para_len <= max_chunk_size:
            current_chunk.append(para)
            current_length += para_len
        else:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_length = 0

            if len(para) > max_chunk_size:
                lines = para.split("\n")
                for line in lines:
                    if len(line) <= max_chunk_size:
                        if current_length + len(line) + 1 <= max_chunk_size:
                            current_chunk.append(line)
                            current_length += len(line) + 1
                        else:
                            if current_chunk:
                                chunks.append("\n".join(current_chunk))
                                current_chunk = [line]
                                current_length = len(line) + 1
                            else:
                                chunks.append(line)
                    else:
                        words = line.split(" ")
                        temp = ""
                        for word in words:
                            if len(word) > max_chunk_size:
                                if temp:
                                    chunks.append(temp)
                                    temp = ""
                                for i in range(0, len(word), max_chunk_size):
                                    chunks.append(word[i:i + max_chunk_size])
                            elif len(temp) + len(word) + (1 if temp else 0) <= max_chunk_size:
                                temp = f"{temp} {word}" if temp else word
                            else:
                                if temp:
                                    chunks.append(temp)
                                temp = word
                        if temp:
                            chunks.append(temp)
            else:
                current_chunk.append(para)
                current_length += para_len

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


# ══════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════

def kb_main_menu() -> InlineKeyboardMarkup:
    """Главное меню."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("ℹ️ Как пользоваться", callback_data="help"),
            InlineKeyboardButton("📊 Моя статистика", callback_data="my_stats"),
        ],
        [
            InlineKeyboardButton("⚡ О возможностях бота", callback_data="about"),
        ],
    ])


def kb_back_to_menu() -> InlineKeyboardMarkup:
    """Кнопка возврата в меню."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← Назад в меню", callback_data="back_to_menu")]
    ])


# ══════════════════════════════════════════════════════════
#  ХЕНДЛЕРЫ — КОМАНДЫ
# ══════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)

    name = user.first_name or "друг"
    text = (
        f"👋 <b>Привет, {html.escape(name)}!</b>\n\n"
        f"Я быстрый и бесплатный бот для мгновенного перевода голоса и видео в текст.\n\n"
        f"🚀 <b>Что я умею:</b>\n"
        f"• 🎙 <b>Голосовые сообщения</b> (.ogg)\n"
        f"• 📹 <b>Видео-кружки</b> (.mp4)\n"
        f"• 🎵 <b>Аудиофайлы</b> (MP3, M4A, WAV, FLAC, AAC…)\n"
        f"• 🎬 <b>Видеофайлы</b> (извлечение звука)\n"
        f"• ✍️ Точная расстановка знаков препинания и заглавных букв\n"
        f"• 🆓 <b>100% бесплатно и без ограничений!</b>\n\n"
        f"👉 <i>Просто отправь или перешли мне голосовое или кружок:</i>"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=kb_main_menu(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"📖 <b>Инструкция по использованию:</b>\n\n"
        f"1️⃣ <b>Голосовые сообщения:</b>\n"
        f"Запишите голосовое или перешлите сообщение из любого чата — бот сразу выдаст текст.\n\n"
        f"2️⃣ <b>Кружки (видео-сообщения):</b>\n"
        f"Отправьте кружок — бот извлечёт аудиодорожку и расшифрует её без потери качества.\n\n"
        f"3️⃣ <b>Аудио и видео файлы:</b>\n"
        f"Поддерживаются файлы MP3, M4A, OGG, WAV, MP4 и др. размером до 20 МБ.\n\n"
        f"💡 <b>Совет:</b> старайтесь говорить без сильного ветра или постороннего фонового шума для идеальной точности.\n\n"
        f"⚡ <b>Команды:</b>\n"
        f"/start — главное меню\n"
        f"/help — эта инструкция\n"
        f"/stats — ваша статистика"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back_to_menu())


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user_stats(uid)
    is_admin = uid in set(ADMIN_IDS)

    user_name = update.effective_user.first_name or "Пользователь"
    total_conv = u.get("total_conversions", 0)
    total_voice = u.get("total_voice", 0)
    total_video = u.get("total_video", 0)
    total_audio = u.get("total_audio", 0)
    created_at = u.get("created_at", "—")

    text = (
        f"📊 <b>Статистика: {html.escape(user_name)}</b>\n\n"
        f"🎙 Голосовых расшифровано: <b>{total_voice}</b>\n"
        f"📹 Кружков расшифровано: <b>{total_video}</b>\n"
        f"🎵 Аудиофайлов: <b>{total_audio}</b>\n"
        f"📝 Всего расшифровано: <b>{total_conv}</b>\n"
        f"📅 С нами с: <code>{created_at}</code>\n"
        f"💎 Статус: <b>Бесплатно навсегда 🚀</b>"
    )

    if is_admin:
        g = get_global_stats()
        text += (
            f"\n\n👑 <b>Админ-статистика:</b>\n"
            f"👥 Всего пользователей: <b>{g['users']}</b>\n"
            f"📈 Всего обработано файлов: <b>{g['total']}</b>\n"
            f"🎙 Всего голосовых: <b>{g['voice']}</b>\n"
            f"📹 Всего кружков: <b>{g['video']}</b>\n"
            f"🎵 Всего аудиофайлов: <b>{g['audio']}</b>"
        )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back_to_menu())


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Рассылка сообщения всем пользователям бота (только для админов)."""
    uid = update.effective_user.id
    if uid not in set(ADMIN_IDS):
        return

    if not context.args:
        await update.message.reply_text("Использование: /broadcast <текст рассылки>")
        return

    broadcast_text = update.message.text.split(maxsplit=1)[1]
    user_ids = get_all_user_ids()
    await update.message.reply_text(f"📢 Начинаю рассылку для {len(user_ids)} пользователей...")

    success = 0
    failed = 0

    for target_id in user_ids:
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=broadcast_text,
                parse_mode=ParseMode.HTML,
            )
            success += 1
            await asyncio.sleep(0.05)  # Защита от лимитов Telegram
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"✅ <b>Рассылка завершена!</b>\n"
        f"Успешно: <b>{success}</b>\n"
        f"Не доставлено: <b>{failed}</b>",
        parse_mode=ParseMode.HTML,
    )


# ══════════════════════════════════════════════════════════
#  ХЕНДЛЕРЫ — ОБРАБОТКА МЕДИА (ГОЛОС / КРУЖКИ / АУДИО)
# ══════════════════════════════════════════════════════════

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    ensure_user(user.id, user.username, user.first_name)

    # Определение типа входящего медиа
    file_obj = None
    media_type = "voice"
    icon = "🎙"
    title = "Голосовое сообщение"
    status_prompt = "🎧 Слушаю и перевожу голосовое в текст…"
    duration = 0
    file_size = 0

    if msg.voice:
        file_obj = msg.voice
        media_type = "voice"
        icon = "🎙"
        title = "Голосовое сообщение"
        status_prompt = "🎧 Слушаю и перевожу голосовое в текст…"
        duration = msg.voice.duration or 0
        file_size = msg.voice.file_size or 0

    elif msg.video_note:
        file_obj = msg.video_note
        media_type = "video"
        icon = "📹"
        title = "Видео-сообщение (кружок)"
        status_prompt = "📹 Извлекаю звук из кружка и перевожу в текст…"
        duration = msg.video_note.duration or 0
        file_size = msg.video_note.file_size or 0

    elif msg.audio:
        file_obj = msg.audio
        media_type = "audio"
        icon = "🎵"
        fname = msg.audio.file_name or "Аудиозапись"
        title = f"Аудио: {fname}"
        status_prompt = "🎵 Обрабатываю аудиофайл…"
        duration = msg.audio.duration or 0
        file_size = msg.audio.file_size or 0

    elif msg.video:
        file_obj = msg.video
        media_type = "video"
        icon = "🎬"
        title = "Видеофайл"
        status_prompt = "🎬 Извлекаю звук из видео и перевожу в текст…"
        duration = msg.video.duration or 0
        file_size = msg.video.file_size or 0

    elif msg.document:
        doc = msg.document
        mime = (doc.mime_type or "").lower()
        fname = (doc.file_name or "").lower()
        is_audio = mime.startswith("audio/") or any(fname.endswith(ext) for ext in [".mp3", ".ogg", ".wav", ".m4a", ".flac", ".aac", ".oga"])
        is_video = mime.startswith("video/") or any(fname.endswith(ext) for ext in [".mp4", ".mov", ".mkv", ".avi", ".webm"])

        if is_audio or is_video:
            file_obj = doc
            media_type = "audio" if is_audio else "video"
            icon = "🎵" if is_audio else "🎬"
            title = f"Файл: {doc.file_name or 'Аудио'}"
            status_prompt = "📁 Извлекаю аудиодорожку из файла…"
            file_size = doc.file_size or 0
        else:
            return
    else:
        return

    # Проверка размера файла (Telegram Bot API limit = 20MB)
    if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await msg.reply_text(
            f"⚠️ <b>Файл слишком большой</b> ({file_size / (1024 * 1024):.1f} МБ).\n"
            f"Telegram Bot API разрешает ботам загружать файлы до {MAX_FILE_SIZE_MB} МБ.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Отправка статуса и действия
    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
    status_msg = await msg.reply_text(status_prompt)

    try:
        tg_file = await file_obj.get_file()

        with tempfile.TemporaryDirectory() as tmp_dir:
            input_ext = ".tmp"
            if media_type == "voice":
                input_ext = ".ogg"
            elif media_type == "video":
                input_ext = ".mp4"
            elif msg.audio and msg.audio.file_name:
                input_ext = os.path.splitext(msg.audio.file_name)[1] or ".mp3"

            input_path = os.path.join(tmp_dir, f"input{input_ext}")
            wav_path = os.path.join(tmp_dir, "extracted.wav")

            # Скачивание файла
            await tg_file.download_to_drive(input_path)

            # Извлечение звука через FFmpeg
            extracted_ok = await extract_audio_to_wav(input_path, wav_path)
            if not extracted_ok or not os.path.exists(wav_path) or os.path.getsize(wav_path) == 0:
                await status_msg.edit_text(
                    "❌ <b>Не удалось извлечь звук из файла.</b>\n"
                    "Проверьте, что в файле присутствует корректная звуковая дорожка.",
                    parse_mode=ParseMode.HTML,
                )
                return

            # Вычисление фактической длительности звука
            calc_dur = get_wav_duration(wav_path)
            actual_dur = duration if duration > 0 else calc_dur
            dur_formatted = format_duration(actual_dur)

            # Распознавание в отдельном потоке (без блокировки event loop)
            t_start = time.time()
            text, engine_name = await asyncio.to_thread(transcriber.transcribe, wav_path)
            elapsed = time.time() - t_start

            if not text:
                await status_msg.edit_text(
                    "❌ <b>Не удалось разобрать речь.</b>\n\n"
                    "Возможные причины:\n"
                    "• В записи тишина или играет только музыка\n"
                    "• Слишком сильные посторонние шумы\n"
                    "• Слишком тихий голос\n\n"
                    "Попробуйте записать ещё раз ближе к микрофону.",
                    parse_mode=ParseMode.HTML,
                )
                return

            # Учёт конвертации в БД
            record_conversion(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                media_type=media_type,
            )

            # Форматирование и экранирование
            escaped_text = html.escape(text)
            stats_footer = f"⏱ {elapsed:.1f} сек | ⏳ {dur_formatted} | ⚡ {engine_name}"

            # Разбивка текста, если превышает лимит Telegram (4096 символов)
            chunks = split_message_text(escaped_text, max_chunk_size=3800)

            first_text = (
                f"{icon} <b>{html.escape(title)}</b>\n\n"
                f"{chunks[0]}\n\n"
                f"📊 <i>{stats_footer}</i>"
            )

            await status_msg.edit_text(first_text, parse_mode=ParseMode.HTML)

            # Если текст очень длинный — досылаем оставшиеся части
            for chunk in chunks[1:]:
                await msg.reply_text(chunk, parse_mode=ParseMode.HTML)

            # Если текст огромный (> 10 000 символов), дополнительно прикрепляем текстовый файл
            if len(text) > 10000:
                bio = io.BytesIO(text.encode("utf-8"))
                bio.name = f"transcript_{int(time.time())}.txt"
                await msg.reply_document(
                    document=bio,
                    caption="📄 Полный текст расшифровки в файле",
                )

    except TelegramError as e:
        logger.error("Telegram API ошибка при обработке медиа uid=%s: %s", user.id, e)
        try:
            await status_msg.edit_text("❌ Ошибка отправки результата. Попробуйте ещё раз.")
        except Exception:
            pass
    except Exception as e:
        logger.exception("Непредвиденная ошибка при обработке медиа uid=%s", user.id)
        try:
            await status_msg.edit_text("❌ Произошла ошибка при обработке файла. Попробуйте ещё раз.")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════
#  ХЕНДЛЕРЫ — CALLBACK (КНОПКИ МЕНЮ)
# ══════════════════════════════════════════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    user = q.from_user

    if data == "back_to_menu":
        name = user.first_name or "друг"
        text = (
            f"👋 <b>Главное меню</b>\n\n"
            f"Отправьте мне любое голосовое сообщение, видео-кружок или аудиофайл — "
            f"и я мгновенно переведу его в текст.\n\n"
            f"Бот полностью бесплатный и без ограничений! 🚀"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_main_menu())
        return

    elif data == "help":
        text = (
            f"📖 <b>Как пользоваться ботом:</b>\n\n"
            f"1️⃣ <b>Голосовые:</b> запишите или перешлите аудиосообщение.\n"
            f"2️⃣ <b>Кружки:</b> отправьте видео-сообщение (кружок) — звук автоматически извлечётся.\n"
            f"3️⃣ <b>Аудиофайлы:</b> отправьте MP3, WAV, M4A, OGG и др. (до 20 МБ).\n\n"
            f"✨ Текст формируется со знаками препинания и заглавными буквами.\n"
            f"🆓 Без подписок, счетов и ограничений."
        )
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back_to_menu())
        return

    elif data == "my_stats":
        u = get_user_stats(user.id)
        total_conv = u.get("total_conversions", 0)
        total_voice = u.get("total_voice", 0)
        total_video = u.get("total_video", 0)
        total_audio = u.get("total_audio", 0)
        created_at = u.get("created_at", "—")

        text = (
            f"📊 <b>Ваша статистика:</b>\n\n"
            f"🎙 Голосовых: <b>{total_voice}</b>\n"
            f"📹 Кружков: <b>{total_video}</b>\n"
            f"🎵 Аудиофайлов: <b>{total_audio}</b>\n"
            f"📝 Всего переведено: <b>{total_conv}</b>\n"
            f"📅 Дата первого запуска: <code>{created_at}</code>\n\n"
            f"💎 Тариф: <b>Бесплатный навсегда 🚀</b>"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back_to_menu())
        return

    elif data == "about":
        engine_desc = "Whisper AI (локально)"
        if GROQ_API_KEY:
            engine_desc = "Groq Whisper Large v3 Turbo (ультрабыстрый)"

        text = (
            f"⚡ <b>О возможностях бота:</b>\n\n"
            f"• 🧠 <b>Движок распознавания:</b> {engine_desc}\n"
            f"• 🎙 <b>Поддержка голоса:</b> OGG, OPUS, MP3, WAV, M4A, AAC, FLAC\n"
            f"• 📹 <b>Поддержка видео:</b> Telegram видео-сообщения (кружки), MP4, MOV\n"
            f"• ✍️ <b>Качество:</b> автоматическая расстановка запятых, точек и заглавных букв\n"
            f"• ⚡ <b>Скорость:</b> извлечение звука через FFmpeg за доли секунды\n"
            f"• 🆓 <b>Полная свобода:</b> никаких подписок, оплат и лимитов!"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back_to_menu())
        return


# ══════════════════════════════════════════════════════════
#  ОБРАБОТКА ОШИБОК И ЗАВЕРШЕНИЕ
# ══════════════════════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        logger.warning("Конфликт getUpdates: возможно запущен другой экземпляр бота.")
        return
    logger.error("Необработанное исключение:", exc_info=context.error)


def force_cleanup_webhook():
    """Сбрасывает webhook и очищает очередь старых апдейтов перед стартом polling."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook"
        resp = requests.post(url, json={"drop_pending_updates": True}, timeout=10)
        if resp.status_code == 200:
            logger.info("✅ Webhook успешно сброшен, старые апдейты очищены")
        else:
            logger.warning("Ответ Telegram при удалении webhook: %s", resp.text)
    except Exception as e:
        logger.warning("Не удалось выполнить запрос сброса webhook: %s", e)


# ══════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА (MAIN)
# ══════════════════════════════════════════════════════════

def main():
    init_db()
    force_cleanup_webhook()

    # Предзагрузка модели Faster-Whisper в фоновом потоке для мгновенного первого ответа
    threading.Thread(target=transcriber.get_local_model, daemon=True, name="whisper_warmup").start()

    # Построение Telegram приложения с адекватным rate-limiter
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(
            AIORateLimiter(
                overall_max_rate=30,
                overall_time_period=1,
                max_retries=3,
            )
        )
        .build()
    )

    # Регистрация команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # Обработка всех типов аудио и видео: голосовые, видео-сообщения (кружки), аудиофайлы, видеофайлы
    media_filter = (
        filters.VOICE
        | filters.VIDEO_NOTE
        | filters.AUDIO
        | filters.VIDEO
        | filters.Document.AUDIO
        | filters.Document.VIDEO
    )
    application.add_handler(MessageHandler(media_filter, handle_media))

    # Обработка кнопок
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Обработчик ошибок
    application.add_error_handler(error_handler)

    logger.info("🚀 Бот успешно настроен и готов к работе!")
    logger.info("Администраторы: %s | Groq: %s", ADMIN_IDS, "Включён" if GROQ_API_KEY else "Отключён (локальный Whisper)")

    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        err_msg = str(e)
        if "Unauthorized" in err_msg or "InvalidToken" in err_msg:
            logger.error("❌ Ошибка авторизации: проверьте BOT_TOKEN в файле .env (получите токен у @BotFather)")
        else:
            logger.exception("Критическая ошибка работы бота: %s", e)


if __name__ == "__main__":
    main()
