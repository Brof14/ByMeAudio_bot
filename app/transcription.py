"""CPU-only local Faster-Whisper speech recognition."""
from __future__ import annotations

import io
import logging
import re
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class AudioTooLong(ValueError):
    pass


@dataclass(frozen=True)
class Transcript:
    text: str
    engine: str
    duration_seconds: float


class Transcriber:
    def __init__(self, model_size: str, beam_size: int, cpu_threads: int,
                 language: str | None, max_duration_seconds: int) -> None:
        self.model_size = model_size
        self.beam_size = beam_size
        self.cpu_threads = cpu_threads
        self.language = language
        self.max_duration_seconds = max_duration_seconds
        self._model = None
        self._model_lock = threading.Lock()

    def get_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                self._model = WhisperModel(
                    self.model_size, device="cpu", compute_type="int8",
                    cpu_threads=self.cpu_threads,
                )
            return self._model

    def warmup(self) -> None:
        try:
            self.get_model()
            from faster_whisper.vad import get_vad_model
            get_vad_model()
            logger.info("Whisper model and VAD loaded")
        except Exception:
            logger.exception("Could not warm up the speech model")

    def transcribe_bytes(self, data: bytes) -> Transcript:
        from faster_whisper.audio import decode_audio

        # Validate decoded length: compressed file size alone is insufficient.
        audio = decode_audio(io.BytesIO(data))
        duration = len(audio) / 16000
        if duration > self.max_duration_seconds:
            raise AudioTooLong(
                f"Audio is {duration:.0f}s; maximum is {self.max_duration_seconds}s"
            )
        if audio.size == 0:
            return Transcript("", "Faster-Whisper", duration)

        model = self.get_model()
        segments, info = model.transcribe(
            audio,
            beam_size=self.beam_size,
            best_of=2,
            temperature=[0.0, 0.1, 0.2],
            vad_filter=True,
            vad_parameters={
                "min_silence_duration_ms": 300,
                "max_speech_duration_s": 30,
                "speech_pad_ms": 100,
            },
            condition_on_previous_text=False,
            no_speech_threshold=0.55,
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            language=self.language,
            task="transcribe",
        )
        # Do not delete repeated words or segments based on additional heuristics:
        # doing so can erase real speech.
        text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
        text = re.sub(r"\s+", " ", text).strip()
        return Transcript(text, f"Faster-Whisper {self.model_size} ({info.language})", duration)
