import asyncio
import io
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore
from voice.stt import MAX_AUDIO_BYTES, FakeSTT, MLXWhisperSTT, STTError, decode_audio


def synthetic_wav(seconds=1.0, rate=48000, freq=220.0) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    pcm = (0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm.tobytes())
    return buf.getvalue()


def test_decode_resamples_to_16k_mono():
    a = decode_audio(synthetic_wav(1.0, 48000))
    assert a.dtype == np.float32 and abs(len(a) - 16000) < 200 and 0.1 < np.abs(a).max() <= 1.0


@pytest.mark.parametrize("bad", [b"", b"not audio at all" * 100])
def test_decode_rejects_bad_input(bad):
    with pytest.raises(STTError):
        decode_audio(bad)


def test_silence_skips_whisper():
    # must not even load the model for silent input
    assert asyncio.run(MLXWhisperSTT(model="does/not-exist").transcribe(np.zeros(16000, np.float32))) == ""


def app(tmp_path, stt):
    return TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite"), stt))


def test_stt_endpoint(tmp_path):
    r = app(tmp_path, FakeSTT("remember that I like trains")).post("/api/stt", content=synthetic_wav())
    assert r.status_code == 200 and r.json() == {"text": "remember that I like trains"}


def test_stt_endpoint_rejects_garbage_and_oversize(tmp_path):
    c = app(tmp_path, FakeSTT())
    assert c.post("/api/stt", content=b"garbage" * 50).status_code == 400
    assert c.post("/api/stt", content=b"\0" * (MAX_AUDIO_BYTES + 1)).status_code == 413


def test_health_reports_stt(tmp_path):
    assert app(tmp_path, FakeSTT()).get("/health").json()["stt"]["backend"] == "fake"


def test_tts_proxy_reports_service_down(tmp_path):
    cfg = PersonaConfig()
    cfg.voice.tts_url = "http://127.0.0.1:9"  # nothing listens here
    c = TestClient(create_app(cfg, EchoBackend(), MemoryStore(tmp_path / "m.sqlite"), FakeSTT()))
    assert c.get("/api/tts/health").json()["ok"] is False
    assert c.post("/api/tts", json={"text": "hello"}).status_code == 503
    assert c.post("/api/tts", json={"text": ""}).status_code == 400


def test_parakeet_falls_back_to_whisper_when_service_down():
    from voice.stt import ParakeetSTT
    stt = ParakeetSTT(url="http://127.0.0.1:9", fallback=FakeSTT("from whisper"))
    audio = np.random.default_rng(0).normal(0, 0.1, 16000).astype(np.float32)
    assert stt.transcribe_sync(audio) == "from whisper" and stt.fallbacks == 1
    assert stt.transcribe_sync(np.zeros(16000, np.float32)) == ""     # silence never reaches either
    assert not stt.healthy()


def test_make_stt_choice():
    from voice.stt import make_stt
    assert make_stt("whisper", "m", "en").name == "mlx-whisper"
    p = make_stt("parakeet", "m", "en")
    assert p.name == "parakeet" and p.fallback().name == "mlx-whisper"
