"""Echo speech recognition service: NVIDIA Parakeet TDT 0.6B v2 (English) via mlx-audio, local only.

Chosen 2026-10-27 after a test on 110 of the owner's recordings: same accuracy as Whisper large-v3-turbo (1.71% word
error rate each) and ~7× faster (0.13 s vs 0.94 s per clip); wake phrases 8/8 in the owner's cloned voice.
  POST /transcribe   body: 16 kHz mono float32 PCM (little-endian)  -> {"text": "..."}
  GET  /health                                                       -> {"ok": bool, "model": ...}
Binds to 127.0.0.1 only. Echo Persona Core falls back to Whisper if this service is unavailable.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("HF_HOME", str(Path(__file__).resolve().parents[1] / "models" / "hf"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import numpy as np  # noqa: E402

MODEL = os.environ.get("ECHO_STT_MODEL", "mlx-community/parakeet-tdt-0.6b-v2")
PORT = int(os.environ.get("ECHO_STT_PORT", "8767"))
MAX_BYTES = 16000 * 4 * 120   # two minutes of audio
# MLX keeps freed GPU memory in a cache for reuse, and every different clip length leaves more behind. In wake mode
# every sound gets transcribed, so without a limit this grew to 10 GB in 8 hours and pushed the Mac into swap.
CACHE_LIMIT = 256 * 1024 * 1024

state = {"model": None, "error": None}
lock = threading.Lock()


def load() -> None:
    while state["model"] is None:
        try:
            import mlx.core as mx
            from mlx_audio.stt import load as load_stt
            mx.set_cache_limit(CACHE_LIMIT)
            m = load_stt(MODEL)
            m.generate(mx.array(np.zeros(16000, np.float32)))   # warm up
            state["model"], state["error"] = m, None
            print(f"speech recognition ready: {MODEL}", flush=True)
        except Exception as e:
            state["error"] = f"{type(e).__name__}: {e}"
            print("speech recognition failed to load:", state["error"], flush=True)
            time.sleep(30)


def transcribe(audio: np.ndarray) -> str:
    import mlx.core as mx
    with lock:
        try:
            return state["model"].generate(mx.array(audio)).text.strip()
        finally:
            mx.clear_cache()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):   # no request logging (and transcripts are never logged)
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": state["model"] is not None, "model": MODEL, "error": state["error"]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/transcribe":
            return self._json(404, {"error": "not found"})
        if state["model"] is None:
            return self._json(503, {"error": "loading"})
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0 or n > MAX_BYTES or n % 4:
            return self._json(400, {"error": "bad audio"})
        audio = np.frombuffer(self.rfile.read(n), dtype="<f4").astype(np.float32)
        try:
            return self._json(200, {"text": transcribe(audio)})
        except Exception as e:
            print("transcription failed:", type(e).__name__, flush=True)
            return self._json(500, {"error": "transcription failed"})


if __name__ == "__main__":
    threading.Thread(target=load, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
