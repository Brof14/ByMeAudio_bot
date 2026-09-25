import os
import sys
import time
import html
import logging
import sqlite3
import io
import asyncio
import threading
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

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

# ВАЖНО: httpx/telegram на уровне INFO печатали каждый getUpdates вместе
# с токеном бота в открытом виде и раздували bot.log без ограничения.
for _noisy in ("httpx", "httpcore", "telegram", "telegram.ext", "apscheduler", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        # Ротация: лог не растёт бесконечно и не съедает диск VPS.
        RotatingFileHandler("bot.log", maxBytes=512 * 1024, backupCount=1, encoding="utf-8"),
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
ADMIN_SET = set(ADMIN_IDS)

MAX_FILE_SIZE_MB = 20  # Ограничение Telegram Bot API на getFile
DB_PATH = "audio_bot.db"

# ── Параметры распознавания (тюнинг скорости) ──────────────────
# WHISPER_MODEL: tiny (~75 МБ, быстрее) | base (~145 МБ, точнее, по умолчанию)
# WHISPER_BEAM_SIZE: 1 = greedy (самый быстрый), 3/5 = точнее и медленнее
# WHISPER_CPU_THREADS: 0 = авто (число ядер VPS), ручное значение переопределяет
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base").strip() or "base"
WHISPER_BEAM_SIZE = max(1, int(os.getenv("WHISPER_BEAM_SIZE", "1") or 1))
WHISPER_CPU_THREADS = int(os.getenv("WHISPER_CPU_THREADS", "0") or 0)

# ══════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════

# Одно долгоживущее соединение вместо connect()/close() на каждый вызов:
# меньше системных вызовов, блокировок файла и повторных PRAGMA на каждый апдейт.
_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA cache_size=-2000")  # ограничить кэш страниц БД
    return _conn


def init_db():
    conn = get_conn()
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
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}

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
    conn.commit()

    logger.info("База данных инициализирована успешно.")


def ensure_user(user_id: int, username: str | None, first_name: str | None) -> None:
    conn = get_conn()
    conn.execute("""
        INSERT INTO users (user_id, username, first_name, last_active)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_active = CURRENT_TIMESTAMP
    """, (user_id, username or "", first_name or ""))
    conn.commit()


def record_conversion(user_id: int, username: str | None, first_name: str | None, media_type: str):
    v_voice = 1 if media_type == "voice" else 0
    v_video = 1 if media_type == "video" else 0
    v_audio = 1 if media_type == "audio" else 0

    conn = get_conn()
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
    """, (user_id, username or "", first_name or "", v_voice, v_video, v_audio))
    conn.commit()


def get_user_stats(user_id: int) -> dict:
    row = get_conn().execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else {}


def get_global_stats() -> dict:
    conn = get_conn()
    row = conn.execute("""
        SELECT COUNT(*)                                        AS users,
               COALESCE(SUM(total_conversions), 0)              AS total,
               COALESCE(SUM(total_voice), 0)                    AS voice,
               COALESCE(SUM(total_video), 0)                    AS video,
               COALESCE(SUM(total_audio), 0)                    AS audio
        FROM users
    """).fetchone()
    return dict(row)


def get_all_user_ids() -> list[int]:
    rows = get_conn().execute("SELECT user_id FROM users").fetchall()
    return [row["user_id"] for row in rows]


# ══════════════════════════════════════════════════════════
#  ДВИЖОК РАСПОЗНАВАНИЯ РЕЧИ (Faster-Whisper, локально)
# ══════════════════════════════════════════════════════════

class Transcriber:
    """
    Локальный офлайн-транскрайбер на faster-whisper (CTranslate2, int8, CPU).

    Аудио НЕ конвертируется заранее: PyAV (встроенный в faster-whisper)
    декодирует ogg/opus, mp4, mp3, m4a, wav, flaс и т.д. прямо в память
    и сам ресемплит в 16 кГц моно. Никаких временных файлов, никакого
    внешнего ffmpeg, никаких сетевых запросов.
    """

    def __init__(self, model_size: str, beam_size: int, cpu_threads: int):
        self.model_size = model_size
        self.beam_size = beam_size
        self.cpu_threads = cpu_threads
        self._model = None
        self._model_lock = threading.Lock()

    def get_model(self):
        """Ленивая загрузка модели (один раз за процесс)."""
        if self._model is not None:
            return self._model

        with self._model_lock:
            if self._model is not None:
                return self._model

            from faster_whisper import WhisperModel

            t = time.perf_counter()
            self._model = WhisperModel(
                self.model_size,
                device="cpu",
                compute_type="int8",
                cpu_threads=self.cpu_threads,
            )
            logger.info(
                "Whisper '%s' (int8, cpu_threads=%d) загружена за %.2f с",
                self.model_size, self.cpu_threads, time.perf_counter() - t,
            )
            return self._model

    def warmup(self):
        """
        Фоновая прогревка при старте: загрузка модели + прогон через VAD,
        чтобы silero-VAD не подгружался во время первого реального сообщения.
        """
        try:
            self.get_model()
            from faster_whisper.vad import get_vad_model
            get_vad_model()
            logger.info("Прогрев распознавания завершён")
        except Exception as e:
            logger.warning("Не удалось прогреть модель: %s", e)

    @staticmethod
    def _is_hallucination(text: str) -> bool:
        """
        Отсекает вырожденные сегменты-зацикливания вида «о, о, о, о, о…»,
        которые Whisper генерирует на тишине/шуме. Проверка обобщённая:
        один и тот же короткий токен, повторённый много раз подряд.
        """
        words = text.split()
        if len(words) < 8:
            return False
        first = words[0].lower()
        short = len(first) <= 4
        return short and all(w.lower() == first for w in words)

    def transcribe_bytes(self, data: bytes) -> tuple[str, str]:
        """data — исходные байты файла из Telegram. Возвращает (текст, движок)."""
        from faster_whisper.audio import decode_audio

        model = self.get_model()

        # Декодирование + ресемплинг 16 кГц моно в оперативную память.
        audio = decode_audio(io.BytesIO(data))
        if audio.size == 0:
            return "", "Faster-Whisper"

        segments, info = model.transcribe(
            audio,
            beam_size=self.beam_size,               # 1 = greedy: в разы быстрее beam=5
            temperature=0.0,                        # ровно один проход декодирования
            vad_filter=True,                        # локальный silero: режет тишину
            vad_parameters={
                # threshold НЕ трогаем: занижение пропускает в декодер шум,
                # no_speech_prob взлетает до ~0.95 и весь текст теряется.
                "min_silence_duration_ms": 300,
                "max_speech_duration_s": 30,        # ограничивает пик памяти на длинных файлах
            },
            condition_on_previous_text=False,       # без «залипания» на предыдущем тексте
            no_speech_threshold=0.6,
        )

        parts = []
        for seg in segments:
            if seg.no_speech_prob >= 0.65:
                continue
            clean = seg.text.strip()
            if clean and not self._is_hallucination(clean):
                parts.append(clean)

        text = " ".join(parts).strip()
        return text, f"Faster-Whisper {self.model_size} ({info.language})"


transcriber = Transcriber(WHISPER_MODEL, WHISPER_BEAM_SIZE, WHISPER_CPU_THREADS)

# Транскрипция CPU-bound: выполняем строго по одному файлу, чтобы не
# перегружать ядра VPS, но при этом event loop остаётся свободным
# (бот мгновенно отвечает на /stats, кнопки и т.п. во время расшифровки).
_asr_semaphore: asyncio.Semaphore | None = None


def asr_slot() -> asyncio.Semaphore:
    global _asr_semaphore
    if _asr_semaphore is None:
        _asr_semaphore = asyncio.Semaphore(1)
    return _asr_semaphore


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
    is_admin = uid in ADMIN_SET

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
    if update.effective_user.id not in ADMIN_SET:
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
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # Защита от лимитов Telegram

    await update.message.reply_text(
        f"✅ <b>Рассылка завершена!</b>\n"
        f"Успешно: <b>{success}</b>\n"
        f"Не доставлено: <b>{failed}</b>",
        parse_mode=ParseMode.HTML,
    )


# ══════════════════════════════════════════════════════════
#  ХЕНДЛЕРЫ — ОБРАБОТКА МЕДИА (ГОЛОС / КРУЖКИ / АУДИО)
# ══════════════════════════════════════════════════════════

AUDIO_EXTS = (".mp3", ".ogg", ".wav", ".m4a", ".flac", ".aac", ".oga", ".opus", ".amr", ".wma")
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".mpg", ".mpeg", ".3gp")


def detect_media(msg) -> dict | None:
    """Определяет тип входящего медиа. Возвращает описание или None."""
    if msg.voice:
        return dict(obj=msg.voice, kind="voice", media="voice", icon="🎙",
                    title="Голосовое сообщение",
                    prompt="🎧 Слушаю и перевожу голосовое в текст…",
                    duration=msg.voice.duration or 0, size=msg.voice.file_size or 0)
    if msg.video_note:
        return dict(obj=msg.video_note, kind="video", media="video", icon="📹",
                    title="Видео-сообщение (кружок)",
                    prompt="📹 Извлекаю звук из кружка и перевожу в текст…",
                    duration=msg.video_note.duration or 0, size=msg.video_note.file_size or 0)
    if msg.audio:
        return dict(obj=msg.audio, kind="audio", media="audio", icon="🎵",
                    title=f"Аудио: {msg.audio.file_name or 'Аудиозапись'}",
                    prompt="🎵 Обрабатываю аудиофайл…",
                    duration=msg.audio.duration or 0, size=msg.audio.file_size or 0)
    if msg.video:
        return dict(obj=msg.video, kind="video", media="video", icon="🎬",
                    title="Видеофайл",
                    prompt="🎬 Извлекаю звук из видео и перевожу в текст…",
                    duration=msg.video.duration or 0, size=msg.video.file_size or 0)
    if msg.document:
        doc = msg.document
        mime = (doc.mime_type or "").lower()
        name = (doc.file_name or "").lower()
        is_audio = mime.startswith("audio/") or name.endswith(AUDIO_EXTS)
        is_video = mime.startswith("video/") or name.endswith(VIDEO_EXTS)
        if not (is_audio or is_video):
            return None
        return dict(obj=doc, kind="doc", media="audio" if is_audio else "video",
                    icon="🎵" if is_audio else "🎬",
                    title=f"Файл: {doc.file_name or 'Аудио'}",
                    prompt="📁 Извлекаю аудиодорожку из файла…",
                    duration=getattr(doc, "duration", None) or 0, size=doc.file_size or 0)
    return None


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


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    # Регистрируем пользователя сразу (как и раньше), чтобы /stats
    # показывал дату первого запуска даже до первой успешной расшифровки.
    ensure_user(user.id, user.username, user.first_name)

    info = detect_media(msg)
    if info is None:
        return
    file_obj = info["obj"]

    # Проверка размера файла (Telegram Bot API limit = 20MB)
    if info["size"] > MAX_FILE_SIZE_MB * 1024 * 1024:
        await msg.reply_text(
            f"⚠️ <b>Файл слишком большой</b> ({info['size'] / (1024 * 1024):.1f} МБ).\n"
            f"Telegram Bot API разрешает ботам загружать файлы до {MAX_FILE_SIZE_MB} МБ.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Отправка статуса и действия
    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
    status_msg = await msg.reply_text(info["prompt"])

    try:
        tg_file = await file_obj.get_file()

        # Файл скачивается сразу в память. На диск он не попадает вообще.
        data = await tg_file.download_as_bytearray()
        del tg_file

        t_start = time.perf_counter()
        async with asr_slot():
            text, engine_name = await asyncio.to_thread(transcriber.transcribe_bytes, bytes(data))
            data = None  # освобождаем память сразу после обработки
        elapsed = time.perf_counter() - t_start

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
        record_conversion(user.id, user.username, user.first_name, info["media"])

        # Форматирование и экранирование
        dur_formatted = format_duration(info["duration"])
        stats_footer = f"⏱ {elapsed:.1f} сек | ⏳ {dur_formatted} | ⚡ {engine_name}"

        # Разбивка текста, если превышает лимит Telegram (4096 символов)
        chunks = split_message_text(html.escape(text), max_chunk_size=3800)

        first_text = (
            f"{info['icon']} <b>{html.escape(info['title'])}</b>\n\n"
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
    except Exception:
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
        text = (
            f"📊 <b>Ваша статистика:</b>\n\n"
            f"🎙 Голосовых: <b>{u.get('total_voice', 0)}</b>\n"
            f"📹 Кружков: <b>{u.get('total_video', 0)}</b>\n"
            f"🎵 Аудиофайлов: <b>{u.get('total_audio', 0)}</b>\n"
            f"📝 Всего переведено: <b>{u.get('total_conversions', 0)}</b>\n"
            f"📅 Дата первого запуска: <code>{u.get('created_at', '—')}</code>\n\n"
            f"💎 Тариф: <b>Бесплатный навсегда 🚀</b>"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back_to_menu())
        return

    elif data == "about":
        text = (
            f"⚡ <b>О возможностях бота:</b>\n\n"
            f"• 🧠 <b>Движок распознавания:</b> Faster-Whisper {WHISPER_MODEL} (локально, офлайн)\n"
            f"• 🎙 <b>Поддержка голоса:</b> OGG, OPUS, MP3, WAV, M4A, AAC, FLAC\n"
            f"• 📹 <b>Поддержка видео:</b> Telegram видео-сообщения (кружки), MP4, MOV\n"
            f"• ✍️ <b>Качество:</b> автоматическая расстановка запятых, точек и заглавных букв\n"
            f"• ⚡ <b>Скорость:</b> звук декодируется прямо из файла, без конвертаций\n"
            f"• 🆓 <b>Полная свобода:</b> никаких подписок, оплат и лимитов!"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back_to_menu())
        return


# ══════════════════════════════════════════════════════════
#  ОБРАБОТКА ОШИБОК
# ══════════════════════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        logger.warning("Конфликт getUpdates: возможно запущен другой экземпляр бота.")
        return
    logger.error("Необработанное исключение:", exc_info=context.error)


# ══════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА (MAIN)
# ══════════════════════════════════════════════════════════

def main():
    init_db()

    # Предзагрузка модели в фоне: первый апдейт не ждёт инициализацию.
    threading.Thread(target=transcriber.warmup, daemon=True, name="whisper_warmup").start()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter(overall_max_rate=30, overall_time_period=1, max_retries=3))
        # Апдейты обрабатываются параллельно: во время расшифровки аудио бот
        # продолжает отвечать на команды, кнопки и показывает «печатает…».
        # 8 — верхняя граница одновременных задач, чтобы при флуде не удерживать
        # в памяти сотни скачанных файлов (True превратился бы в 256).
        .concurrent_updates(8)
        # Скачивание файлов: увеличенные таймауты убирают бесполезные
        # ретраи и обрывы на медленном канале к Telegram.
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )

    # Регистрация команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # Голосовые, видео-кружки, аудио- и видеофайлы, а также документы
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
    logger.info("Администраторы: %s | Модель: %s (beam=%d, cpu_threads=%d)",
                ADMIN_IDS, WHISPER_MODEL, WHISPER_BEAM_SIZE, WHISPER_CPU_THREADS)

    # Только те типы апдейтов, которые бот реально обрабатывает:
    # меньше трафика и JSON-парсинга на каждый long-poll.
    allowed_updates = [
        "message", "callback_query", "my_chat_member", "chat_member",
        "chat_join_request", "poll", "poll_answer",
    ]

    try:
        application.run_polling(allowed_updates=allowed_updates, drop_pending_updates=True)
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
