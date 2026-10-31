"""Validated application settings (no side effects on import)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    db_path: Path
    whisper_model: str
    whisper_beam_size: int
    whisper_cpu_threads: int
    transcription_language: str | None
    max_file_size_mb: int
    max_duration_seconds: int
    max_pending_jobs: int
    user_cooldown_seconds: int
    log_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("BOT_TOKEN is required. Set it in .env.")

        raw_admin_ids = os.getenv("ADMIN_IDS", "")
        try:
            admin_ids = frozenset(
                int(part.strip()) for part in raw_admin_ids.split(",") if part.strip()
            )
        except ValueError as exc:
            raise ValueError("ADMIN_IDS must contain comma-separated numeric IDs") from exc

        def integer(name: str, default: int, low: int, high: int) -> int:
            try:
                value = int(os.getenv(name, str(default)).strip())
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer") from exc
            if not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
            return value

        lang = os.getenv("TRANSCRIPTION_LANGUAGE", "ru").strip().lower()
        if lang in ("", "auto"):
            lang = None
        elif not (len(lang) in (2, 3) and lang.isalpha()):
            raise ValueError("TRANSCRIPTION_LANGUAGE must be auto or a language code")

        return cls(
            bot_token=token,
            admin_ids=admin_ids,
            db_path=Path(os.getenv("DB_PATH", "data/audio_bot.db")),
            whisper_model=os.getenv("WHISPER_MODEL", "base").strip() or "base",
            whisper_beam_size=integer("WHISPER_BEAM_SIZE", 2, 1, 10),
            whisper_cpu_threads=integer("WHISPER_CPU_THREADS", 0, 0, 128),
            transcription_language=lang,
            max_file_size_mb=integer("MAX_FILE_SIZE_MB", 20, 1, 20),
            max_duration_seconds=integer("MAX_DURATION_SECONDS", 900, 30, 3600),
            max_pending_jobs=integer("MAX_PENDING_JOBS", 4, 1, 30),
            user_cooldown_seconds=integer("USER_COOLDOWN_SECONDS", 3, 0, 600),
            log_path=Path(os.getenv("LOG_PATH", "bot.log")),
        )
