"""Local speech recognition. Audio never leaves the machine."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Protocol

import numpy as np

from core.config import ROOT

SAMPLE_RATE = 16000
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_SECONDS = 120


class STTError(Exception):
    pass


def decode_audio(data: bytes) -> np.ndarray:
    """Any browser format (webm/opus, mp4, wav) -> 16 kHz mono float32, via ffmpeg on stdin."""
    if not data:
        raise STTError("empty audio")
    if len(data) > MAX_AUDIO_BYTES:
        raise STTError("audio too large")
    try:
        out = subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", "pipe:0", "-t", str(MAX_SECONDS),
             "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
            input=data, capture_output=True, timeout=30, check=True).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise STTError("could not decode audio") from e
    if not out:
        raise STTError("no audio samples")
    return np.frombuffer(out, np.int16).astype(np.float32) / 32768.0


class STTBackend(Protocol):
    name: str

    async def transcribe(self, audio: np.ndarray) -> str: ...
    def transcribe_sync(self, audio: np.ndarray) -> str: ...


class MLXWhisperSTT:
    name = "mlx-whisper"

    def __init__(self, model: str = "mlx-community/whisper-large-v3-turbo", language: str | None = "en"):
        os.environ.setdefault("HF_HOME", str(ROOT / "models" / "hf"))
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")  # never fetch at runtime; models are downloaded by setup
        self.model, self.language = model, language
        self._lock = threading.Lock()  # one transcription at a time on the GPU (shared by live speech and background jobs)

    def _run(self, audio: np.ndarray) -> str:
        import mlx_whisper
        with self._lock:
            r = mlx_whisper.transcribe(audio, path_or_hf_repo=self.model, language=self.language,
                                       condition_on_previous_text=False, verbose=None)
        return str(r.get("text", "")).strip()

    def transcribe_sync(self, audio: np.ndarray) -> str:
        if float(np.sqrt(np.mean(audio ** 2))) < 0.003:  # silence: skip Whisper's hallucinated "Thank you."
            return ""
        return self._run(audio)

    async def transcribe(self, audio: np.ndarray) -> str:
        return await asyncio.to_thread(self.transcribe_sync, audio)


class ParakeetSTT:
    """Parakeet via the local speech recognition service (tts/stt_service.py, 127.0.0.1:8767).
    If that service is down or fails, falls back to Whisper (loaded only when first needed), so listening never breaks."""
    name = "parakeet"

    def __init__(self, url: str = "http://127.0.0.1:8767", fallback: "STTBackend | None" = None):
        self.url = url.rstrip("/")
        self._fallback = fallback
        self.fallbacks = 0

    def fallback(self) -> "STTBackend":
        if self._fallback is None:
            self._fallback = MLXWhisperSTT()
        return self._fallback

    def healthy(self) -> bool:
        import urllib.request
        try:
            return json.loads(urllib.request.urlopen(self.url + "/health", timeout=2).read()).get("ok", False)
        except Exception:
            return False

    def transcribe_sync(self, audio: np.ndarray) -> str:
        if float(np.sqrt(np.mean(audio ** 2))) < 0.003:  # silence
            return ""
        import urllib.request
        try:
            req = urllib.request.Request(self.url + "/transcribe", data=audio.astype("<f4").tobytes(), method="POST",
                                         headers={"Content-Type": "application/octet-stream"})
            return json.loads(urllib.request.urlopen(req, timeout=60).read())["text"]
        except Exception:
            self.fallbacks += 1
            return self.fallback().transcribe_sync(audio)

    async def transcribe(self, audio: np.ndarray) -> str:
        return await asyncio.to_thread(self.transcribe_sync, audio)


def make_stt(choice: str, whisper_model: str, language: str | None) -> "STTBackend":
    """choice comes from config/voice.json "stt": "parakeet" (default) or "whisper"."""
    whisper = MLXWhisperSTT(whisper_model, language)
    return whisper if choice == "whisper" else ParakeetSTT(fallback=whisper)


class FakeSTT:
    """For tests."""
    name = "fake"

    def __init__(self, text: str = "hello echo"):
        self.text = text

    async def transcribe(self, audio: np.ndarray) -> str:
        return self.text

    def transcribe_sync(self, audio: np.ndarray) -> str:
        return self.text
