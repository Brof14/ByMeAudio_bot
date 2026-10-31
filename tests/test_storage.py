import sqlite3

from app.storage import StatsRepository


def test_legacy_tables_survive_startup(tmp_path):
    path = tmp_path / "audio_bot.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, reference TEXT)")
    old.execute("INSERT INTO transactions (reference) VALUES ('must-remain')")
    old.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT)")
    old.execute("INSERT INTO users (user_id, username) VALUES (42, 'legacy')")
    old.commit()
    old.close()

    db = StatsRepository(path)
    db.initialize()
    db.initialize()
    db.ensure_user(42, "name", "Name")
    db.record_conversion(42, "name", "Name", "voice")
    db.record_conversion(42, "name", "Name", "video_note")
    db.record_conversion(42, "name", "Name", "audio")
    assert db.user_stats(42)["total_conversions"] == 3
    assert db.user_stats(42)["total_voice"] == 1
    assert db.user_stats(42)["total_video"] == 1
    assert db.user_stats(42)["total_audio"] == 1
    assert db.global_stats()["users"] == 1
    assert db.conn.execute("SELECT reference FROM transactions").fetchone()[0] == "must-remain"
    db.delete_user(42)
    assert db.user_stats(42) == {}
    assert db.conn.execute("SELECT reference FROM transactions").fetchone()[0] == "must-remain"
    db.close()


def test_database_creation(tmp_path):
    path = tmp_path / "nested" / "audio.db"
    db = StatsRepository(path)
    db.initialize()
    db.ensure_user(99, None, "new")
    assert db.user_stats(99)["first_name"] == "new"
    db.close()
