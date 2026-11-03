"""Local speech recognition. Audio never leaves the machine."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import gc
import sys
import time
import logging
from pathlib import Path
from typing import Protocol

import numpy as np

from core.config import ROOT

SAMPLE_RATE = 16000
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_SECONDS = 120


log = logging.getLogger("echo.stt")


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
        self.last_used = 0.0

    def _run(self, audio: np.ndarray) -> str:
        import mlx_whisper
        with self._lock:
            self.last_used = time.time()
            r = mlx_whisper.transcribe(audio, path_or_hf_repo=self.model, language=self.language,
                                       condition_on_previous_text=False, verbose=None)
            self.last_used = time.time()
        return str(r.get("text", "")).strip()

    def loaded(self) -> bool:
        t = sys.modules.get("mlx_whisper.transcribe")
        return bool(t and getattr(t.ModelHolder, "model", None) is not None)

    def unload(self) -> bool:
        """Free Whisper's ~2.3 GB (measured 2026-11-01: 2,298 → 167 MB a few seconds later). Reloads on next use."""
        t = sys.modules.get("mlx_whisper.transcribe")
        if not t or t.ModelHolder.model is None:
            return False
        with self._lock:   # never while it's transcribing
            t.ModelHolder.model, t.ModelHolder.model_path = None, None
            gc.collect()
            import mlx.core as mx
            mx.clear_cache()
        return True

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
        self.retried_ok = 0   # a failed request that worked on the one retry (no Whisper needed)

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
        data = audio.astype("<f4").tobytes()
        err = None
        for attempt in range(2):   # 2026-11-01: retry once before loading Whisper (2.3 GB that then stayed loaded)
            try:
                req = urllib.request.Request(self.url + "/transcribe", data=data, method="POST",
                                             headers={"Content-Type": "application/octet-stream"})
                text = json.loads(urllib.request.urlopen(req, timeout=60).read())["text"]
                if attempt:
                    self.retried_ok += 1
                return text
            except Exception as e:  # noqa: BLE001
                err = e
                time.sleep(0.3)
        self.fallbacks += 1
        log.warning("parakeet failed twice (%s), using Whisper", type(err).__name__)   # never the audio or text
        return self.fallback().transcribe_sync(audio)

    def release_idle_fallback(self, idle_s: float = 600) -> bool:
        """Unload the Whisper fallback after `idle_s` without use (called from the quiet loop). True if unloaded."""
        fb = self._fallback
        if isinstance(fb, MLXWhisperSTT) and fb.loaded() and time.time() - fb.last_used > idle_s:
            if fb.unload():
                log.info("whisper fallback unloaded after %d idle minutes", idle_s // 60)
                return True
        return False

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
