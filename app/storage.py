"""SQLite statistics, with non-destructive migrations."""
from __future__ import annotations

import sqlite3
from pathlib import Path


class StatsRepository:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=20)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=20000")
        self.conn.execute("PRAGMA synchronous=NORMAL")

    def initialize(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                total_conversions INTEGER NOT NULL DEFAULT 0,
                total_voice INTEGER NOT NULL DEFAULT 0,
                total_video INTEGER NOT NULL DEFAULT 0,
                total_audio INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_active TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(users)")}
        migrations = {
            "username": "TEXT",
            "first_name": "TEXT",
            "total_conversions": "INTEGER NOT NULL DEFAULT 0",
            "total_voice": "INTEGER NOT NULL DEFAULT 0",
            "total_video": "INTEGER NOT NULL DEFAULT 0",
            "total_audio": "INTEGER NOT NULL DEFAULT 0",
            "created_at": "TEXT",
            "last_active": "TEXT",
        }
        for name, declaration in migrations.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE users ADD COLUMN {name} {declaration}")
        self.conn.commit()
        # Deliberately do NOT drop legacy 'transactions' or other tables.

    def ensure_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        self.conn.execute("""
            INSERT INTO users (user_id, username, first_name, last_active)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_active = CURRENT_TIMESTAMP
        """, (user_id, username or "", first_name or ""))
        self.conn.commit()

    def record_conversion(self, user_id: int, username: str | None, first_name: str | None,
                          media_type: str) -> None:
        voice = int(media_type == "voice")
        video = int(media_type in ("video", "video_note"))
        audio = int(media_type == "audio")
        self.conn.execute("""
            INSERT INTO users (
                user_id, username, first_name, total_conversions,
                total_voice, total_video, total_audio, last_active
            ) VALUES (?, ?, ?, 1, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                total_conversions = users.total_conversions + 1,
                total_voice = users.total_voice + excluded.total_voice,
                total_video = users.total_video + excluded.total_video,
                total_audio = users.total_audio + excluded.total_audio,
                last_active = CURRENT_TIMESTAMP
        """, (user_id, username or "", first_name or "", voice, video, audio))
        self.conn.commit()

    def user_stats(self, user_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else {}

    def global_stats(self) -> dict:
        row = self.conn.execute("""
            SELECT COUNT(*) AS users,
                   COALESCE(SUM(total_conversions), 0) AS total,
                   COALESCE(SUM(total_voice), 0) AS voice,
                   COALESCE(SUM(total_video), 0) AS video,
                   COALESCE(SUM(total_audio), 0) AS audio
            FROM users
        """).fetchone()
        return dict(row)

    def user_ids(self) -> list[int]:
        return [row["user_id"] for row in self.conn.execute("SELECT user_id FROM users")]

    def delete_user(self, user_id: int) -> None:
        self.conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
