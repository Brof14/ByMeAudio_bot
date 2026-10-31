from types import SimpleNamespace

import numpy as np
import pytest
from faster_whisper.vad import VadOptions

from app.transcription import AudioTooLong, Transcriber


class StubModel:
    def __init__(self):
        self.last = None

    def transcribe(self, audio, **options):
        self.last = options
        # Regression test: reject unrecognized VAD option names.
        VadOptions(**options["vad_parameters"])
        return (
            [SimpleNamespace(text=" Нет, нет, нет. "), SimpleNamespace(text="Запись готова!")],
            SimpleNamespace(language="ru"),
        )


def test_vad_parameters_and_exact_repetitions(monkeypatch):
    model = StubModel()
    transcriber = Transcriber("base", 2, 0, "ru", 300)
    monkeypatch.setattr(transcriber, "get_model", lambda: model)
    monkeypatch.setattr(transcriber, "_validate_container_duration", lambda data: None)
    monkeypatch.setattr("faster_whisper.audio.decode_audio", lambda data: np.zeros(16000))
    result = transcriber.transcribe_bytes(b"audio")
    assert result.text == "Нет, нет, нет. Запись готова!"
    assert model.last["vad_parameters"]["speech_pad_ms"] == 100
    assert "padding_duration_ms" not in model.last["vad_parameters"]
    assert model.last["language"] == "ru"
    assert result.duration_seconds == 1


def test_rejects_long_decoded_audio_before_model_loading(monkeypatch):
    transcriber = Transcriber("base", 2, 0, None, 1)
    monkeypatch.setattr(transcriber, "_validate_container_duration", lambda data: None)
    monkeypatch.setattr("faster_whisper.audio.decode_audio", lambda data: np.zeros(32000))
    monkeypatch.setattr(transcriber, "get_model", lambda: pytest.fail("model must not load"))
    with pytest.raises(AudioTooLong):
        transcriber.transcribe_bytes(b"audio")

def test_container_metadata_rejects_overlong_recording():
    import io
    import wave

    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 32000)

    recognizer = Transcriber("base", 2, 0, "ru", 1)
    with pytest.raises(AudioTooLong):
        recognizer._validate_container_duration(audio.getvalue())
