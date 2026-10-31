"""Handler bootstrap smoke test: no network or model download required."""
from telegram.ext import Application


def test_main_builds_real_ptb_handlers_without_network(tmp_path, monkeypatch):
    import bot

    monkeypatch.setenv(
        "BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789"
    )
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "bot.log"))
    monkeypatch.setenv("ADMIN_IDS", "123456")
    monkeypatch.setattr(bot.threading.Thread, "start", lambda self: None)

    seen = []

    def fake_poll(self, **kwargs):
        seen.append(self)
        assert kwargs["drop_pending_updates"] is False
        assert kwargs["allowed_updates"] == ["message", "callback_query"]

    monkeypatch.setattr(Application, "run_polling", fake_poll)
    bot.main()
    assert len(seen) == 1
    assert len(seen[0].handlers[0]) >= 8
