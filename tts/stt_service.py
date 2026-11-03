"""Echo speech recognition service: NVIDIA Parakeet TDT 0.6B v2 (English), local only.

Engine (2026-11-02): the SAME model as Core ML on the Apple Neural Engine (voice/ane_stt, FluidAudio) is used first:
205 of the owner's recordings → 1.78% word errors, 44 ms per clip, GPU 0.8% busy (it no longer competes with the LLM and
the voice). If it fails or stalls, that request goes to the mlx-audio version on the GPU (loaded only when needed).
ECHO_STT_ENGINE=mlx uses mlx-audio only (the previous setup).

Chosen 2026-10-27 after a test on 110 of the owner's recordings: same accuracy as Whisper large-v3-turbo (1.71% word
error rate each) and ~7× faster (0.13 s vs 0.94 s per clip); wake phrases 8/8 in the owner's cloned voice.
  POST /transcribe   body: 16 kHz mono float32 PCM (little-endian)  -> {"text": "..."}
  GET  /health                                                       -> {"ok": bool, "model": ...}
Binds to 127.0.0.1 only. Echo Persona Core falls back to Whisper if this service is unavailable.
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import tempfile
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
# 2026-11-02: ALL MLX work (loading and every transcription) runs on this one thread. MLX keeps its GPU stream per
# thread; the HTTP server answers each request on a new thread, so requests failed with "There is no Stream(gpu, 0)
# in current thread" (205/205 in the test, 241 in the log) and Echo fell back to Whisper (+2.3 GB).
from concurrent.futures import ThreadPoolExecutor
MLX = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")


def load() -> None:
    while state["model"] is None:
        try:
            import mlx.core as mx
            from mlx_audio.stt import load as load_stt
            mx.set_cache_limit(CACHE_LIMIT)
            m = load_stt(MODEL)
            import hw
            if hw.stt_dtype() == "bf16":   # tts/hw.py: 16 GB Macs → bfloat16, 24 GB+ → full precision
                # The weights come as float32 (618 M × 4 B = 2.5 GB). bfloat16 halves that (measured 2026-10-31:
                # 2.51 → 1.28 GB; 12 of the owner's takes: same words, 2 differed by a final full stop; a bit faster),
                # leaving room for the LLM and the voice on the shared GPU. ECHO_STT_DTYPE=f32 forces full precision.
                import gc
                m.set_dtype(mx.bfloat16)
                gc.collect()
                mx.clear_cache()
            m.generate(mx.array(np.zeros(16000, np.float32)))   # warm up
            state["model"], state["error"] = m, None
            print(f"speech recognition ready: {MODEL}", flush=True)
        except Exception as e:
            state["error"] = f"{type(e).__name__}: {e}"
            print("speech recognition failed to load:", state["error"], flush=True)
            time.sleep(30)


ANE_BIN = Path(os.environ.get("ECHO_ANE_STT",
                              str(Path(__file__).resolve().parents[1] / "voice" / "ane_stt" / ".build" / "release" / "ane-stt")))
ENGINE = os.environ.get("ECHO_STT_ENGINE", "ane" if ANE_BIN.exists() else "mlx")


class AneEngine:
    """The ane-stt program kept running; one request at a time (path of a raw float32 file in, one JSON line out)."""
    def __init__(self, binary: Path):
        self.binary, self.p, self.ready, self.error = binary, None, False, None
        self.lock = threading.Lock()
        self.dir = Path(tempfile.mkdtemp(prefix="echo-ane-stt-"))
        self.served = self.failed = 0

    def _ask(self, audio: np.ndarray, timeout: float) -> str:
        f = self.dir / "clip.f32"
        np.asarray(audio, dtype="<f4").tofile(f)
        try:
            self.p.stdin.write(str(f) + "\n")
            self.p.stdin.flush()
            if not select.select([self.p.stdout], [], [], timeout)[0]:
                raise TimeoutError(f"no answer in {timeout:.0f} s")
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("ane-stt stopped")
            o = json.loads(line)
            if "error" in o:
                raise RuntimeError(o["error"][:200])
            return str(o.get("text", "")).strip()
        finally:
            f.unlink(missing_ok=True)

    def start(self) -> None:
        with self.lock:
            self.stop_locked()
            try:
                self.p = subprocess.Popen([str(self.binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, text=True, bufsize=1)
                self._ask(np.zeros(16000, np.float32), timeout=180)   # loads (and compiles once) the Core ML model
                self.ready, self.error = True, None
                print(f"speech recognition ready: Parakeet v2 on the Neural Engine ({self.binary.name})", flush=True)
            except Exception as e:  # noqa: BLE001
                self.ready, self.error = False, f"{type(e).__name__}: {e}"[:200]
                print("neural engine speech recognition failed to start:", self.error, flush=True)
                self.stop_locked()

    def stop_locked(self) -> None:
        if self.p is not None:
            try:
                self.p.kill()
            except Exception:  # noqa: BLE001
                pass
            self.p = None
        self.ready = False

    def transcribe(self, audio: np.ndarray) -> str:
        with self.lock:
            if not self.ready:
                raise RuntimeError("neural engine not ready")
            try:
                text = self._ask(audio, timeout=max(15.0, len(audio) / 16000 * 2))
                self.served += 1
                return text
            except Exception:
                self.failed += 1
                self.stop_locked()
                threading.Thread(target=self.start, daemon=True).start()   # restart in the background
                raise


ane = AneEngine(ANE_BIN) if ENGINE == "ane" else None


def ready() -> bool:
    return bool((ane and ane.ready) or state["model"] is not None)


def _ensure_mlx() -> None:
    """The GPU fallback, loaded on first need (normally never, while the Neural Engine works)."""
    if state["model"] is None:
        MLX.submit(load).result()


def _transcribe_on_mlx_thread(audio: np.ndarray) -> str:
    import mlx.core as mx
    try:
        return state["model"].generate(mx.array(audio)).text.strip()
    finally:
        mx.clear_cache()


def transcribe(audio: np.ndarray) -> str:
    if ane is not None and ane.ready:
        try:
            return ane.transcribe(audio)
        except Exception as e:  # noqa: BLE001
            print("neural engine transcription failed, using the GPU version:", type(e).__name__,
                  str(e)[:120].replace("\n", " "), flush=True)   # never the audio or the text
    with lock:
        _ensure_mlx()
        return MLX.submit(_transcribe_on_mlx_thread, audio).result()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):   # no request logging (and transcripts are never logged)
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": ready(), "model": MODEL,
                                    "engine": "ane" if (ane and ane.ready) else ("mlx" if state["model"] is not None else None),
                                    "error": (ane.error if ane else None) or state["error"],
                                    **({"ane_served": ane.served, "ane_failed": ane.failed} if ane else {})})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/transcribe":
            return self._json(404, {"error": "not found"})
        if not ready():
            return self._json(503, {"error": "loading"})
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0 or n > MAX_BYTES or n % 4:
            return self._json(400, {"error": "bad audio"})
        audio = np.frombuffer(self.rfile.read(n), dtype="<f4").astype(np.float32)
        try:
            return self._json(200, {"text": transcribe(audio)})
        except Exception as e:
            print("transcription failed:", type(e).__name__, str(e)[:200].replace("\n", " "),   # error text only, never audio
                  f"(audio {len(audio) / 16000:.2f} s)", flush=True)
            return self._json(500, {"error": "transcription failed"})


if __name__ == "__main__":
    if ane is not None:
        def _start():
            ane.start()
            if not ane.ready:   # no Neural Engine: load the GPU version so listening still works
                MLX.submit(load)
        threading.Thread(target=_start, daemon=True).start()
    else:
        MLX.submit(load)   # loads on the MLX thread, like every transcription
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
