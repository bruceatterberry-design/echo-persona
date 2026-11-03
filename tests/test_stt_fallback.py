"""2026-11-01: Parakeet failures retry once before Whisper; the Whisper fallback is unloaded after idle time."""
import time

import numpy as np

from voice import stt

LOUD = np.random.default_rng(0).normal(0, 0.05, 16000).astype(np.float32)


class FakeWhisper(stt.MLXWhisperSTT):
    def __init__(self):
        super().__init__()
        self.calls, self._loaded = 0, False

    def _run(self, audio):
        self.calls += 1
        self._loaded = True
        self.last_used = time.time()
        return "whisper text"

    def loaded(self):
        return self._loaded

    def unload(self):
        self._loaded = False
        return True


def fake_urlopen(answers):
    import io
    import json

    def urlopen(req, timeout=0):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return io.BytesIO(json.dumps({"text": a}).encode())
    return urlopen


def test_retry_once_then_success(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen([RuntimeError("busy"), "hello"]))
    monkeypatch.setattr(stt.time, "sleep", lambda s: None)
    w = FakeWhisper()
    p = stt.ParakeetSTT(fallback=w)
    assert p.transcribe_sync(LOUD) == "hello" and p.retried_ok == 1 and p.fallbacks == 0 and w.calls == 0


def test_two_failures_use_whisper_then_unload_when_idle(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen([RuntimeError("a"), RuntimeError("b")]))
    monkeypatch.setattr(stt.time, "sleep", lambda s: None)
    w = FakeWhisper()
    p = stt.ParakeetSTT(fallback=w)
    assert p.transcribe_sync(LOUD) == "whisper text" and p.fallbacks == 1
    assert not p.release_idle_fallback(idle_s=600)            # just used: stays
    w.last_used = time.time() - 601
    assert p.release_idle_fallback(idle_s=600) and not w.loaded()
    assert not p.release_idle_fallback(idle_s=600)            # nothing left to unload


def test_silence_never_calls_anything():
    p = stt.ParakeetSTT(url="http://127.0.0.1:9", fallback=FakeWhisper())
    assert p.transcribe_sync(np.zeros(16000, dtype=np.float32)) == ""
