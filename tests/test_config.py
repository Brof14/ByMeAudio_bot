import pytest

from app.config import Settings


def test_required_token(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="BOT_TOKEN"):
        Settings.from_env()


def test_configuration_validation(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:test")
    monkeypatch.setenv("ADMIN_IDS", "123, 456")
    monkeypatch.setenv("TRANSCRIPTION_LANGUAGE", "auto")
    monkeypatch.setenv("MAX_PENDING_JOBS", "3")
    config = Settings.from_env()
    assert config.admin_ids == frozenset({123, 456})
    assert config.transcription_language is None
    assert config.max_pending_jobs == 3
    monkeypatch.setenv("MAX_FILE_SIZE_MB", "21")
    with pytest.raises(ValueError, match="MAX_FILE_SIZE_MB"):
        Settings.from_env()
