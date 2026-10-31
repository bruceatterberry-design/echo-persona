"""Echo voice service: the owner's cloned voice (Qwen3-TTS via mlx-audio), local only.

Runs in its own environment (~/EchoPersona/tts) so its dependencies never clash with Echo Persona Core.
  POST /speak  {"text": "..."}  -> audio/wav (24 kHz mono 16-bit)
  POST /stream {"text": "..."}  -> chunked raw PCM s16le mono as it is generated (X-Sample-Rate header)
  GET  /health                  -> {"ok": true, "model": ..., "loaded": bool}
Binds to 127.0.0.1 only. One generation at a time.
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("HF_HOME", str(Path(__file__).resolve().parents[1] / "models" / "hf"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import numpy as np  # noqa: E402

MODEL = os.environ.get("ECHO_TTS_MODEL", "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16")


def dataset_root() -> Path:
    """dataset_root from config/echo.yaml (this env has no YAML library, and only that one line is needed)."""
    cfg = Path(__file__).resolve().parents[1] / "config" / "echo.yaml"
    m = re.search(r"""^dataset_root:\s*["']?([^"'#\n]+?)["']?\s*(?:#.*)?$""", cfg.read_text(), re.M) if cfg.exists() else None
    return Path(m.group(1)).expanduser() if m else Path("/Volumes/External storage/EchoPersona-Data")


REFS = Path(os.environ.get("ECHO_TTS_REFS") or dataset_root() / "derived" / "voice" / "refs-v1")
VOICE_CFG = Path(__file__).resolve().parents[1] / "config" / "voice.json"   # {"reference": "<name in refs.json>"}
REF_NAME = (json.loads(VOICE_CFG.read_text()).get("reference") if VOICE_CFG.exists() else None) \
    or os.environ.get("ECHO_TTS_REF", "storytelling")
PORT = int(os.environ.get("ECHO_TTS_PORT", "8766"))
MAX_CHARS = 600

state = {"model": None, "ref_audio": None, "ref_text": None, "error": None, "refs": {}}
lock = threading.Lock()


CACHE_LIMIT = 512 * 1024 * 1024   # cap MLX's reuse cache of freed GPU memory (it grows with every new sentence length)


def load() -> None:
    try:
        import mlx.core as mx
        mx.set_cache_limit(CACHE_LIMIT)
        from huggingface_hub import snapshot_download
        from mlx_audio.tts.utils import load_model
        refs = json.loads((REFS / "refs.json").read_text())
        from mlx_audio.utils import load_audio
        state["ref_audio"] = str(REFS / f"ref-{REF_NAME}.wav")
        state["ref_text"] = refs[REF_NAME]["text"]
        state["model"] = load_model(Path(snapshot_download(MODEL)))
        import hw
        if hw.tts_bits() == 8:   # tts/hw.py: 16 GB Macs → 8-bit voice (1.61× vs 0.96× real time, −0.85 GB), 24 GB+ → 16-bit
            import gc

            import mlx.nn as nn
            nn.quantize(state["model"].talker, group_size=64, bits=8)
            gc.collect()
            mx.clear_cache()
        print("voice:", hw.describe(), flush=True)
        # decode the reference once, instead of re-reading and resampling the file for every sentence
        state["ref_audio"] = load_audio(state["ref_audio"], sample_rate=state["model"].sample_rate)
        synth("Hello.")  # warm up
        print(f"voice service ready: {MODEL}, reference '{REF_NAME}'", flush=True)
    except Exception as e:  # report through /health rather than crash
        state["error"] = f"{type(e).__name__}: {e}"
        print("voice service failed to load:", state["error"], flush=True)


def load_with_retry() -> None:
    """At boot the external SSD may not be mounted yet: keep trying every 30 s until the voice loads."""
    while state["model"] is None:
        load()
        if state["model"] is None:
            time.sleep(30)
    state["error"] = None


def reference(name: str | None):
    """(audio, text) for a named reference (for comparing references); default = the configured one."""
    if not name or name == REF_NAME:
        return state["ref_audio"], state["ref_text"]
    if name not in state["refs"]:
        from mlx_audio.utils import load_audio
        meta = json.loads((REFS / "refs.json").read_text())
        if name not in meta or not re.fullmatch(r"[\w-]{1,40}", name):
            raise KeyError(name)
        state["refs"][name] = (load_audio(str(REFS / f"ref-{name}.wav"), sample_rate=state["model"].sample_rate), meta[name]["text"])
    return state["refs"][name]


def synth(text: str, ref: str | None = None) -> bytes:
    ref_audio, ref_text = reference(ref)
    with lock:
        t = time.perf_counter()
        parts, rate = [], 24000
        for r in state["model"].generate(text=text, ref_audio=ref_audio, ref_text=ref_text,
                                          lang_code="en", split_pattern="\n"):
            parts.append(np.array(r.audio, dtype=np.float32))
            rate = r.sample_rate
        audio = np.concatenate(parts) if parts else np.zeros(1, np.float32)
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm.tobytes())
        print(f"spoke {len(audio) / rate:.1f}s audio in {time.perf_counter() - t:.1f}s", flush=True)
        return buf.getvalue()


STREAM_INTERVAL = float(os.environ.get("ECHO_TTS_STREAM_INTERVAL", "0.8"))  # seconds of audio per piece


def synth_stream(text: str):
    """Yield (sample_rate, int16 PCM bytes) pieces as soon as the model produces them."""
    with lock:
        t, first = time.perf_counter(), None
        total = 0
        for r in state["model"].generate(text=text, ref_audio=state["ref_audio"], ref_text=state["ref_text"],
                                          lang_code="en", split_pattern="\n", stream=True,
                                          streaming_interval=STREAM_INTERVAL):
            a = np.array(r.audio, dtype=np.float32)
            if not a.size:
                continue
            first = first or time.perf_counter() - t
            total += a.size / r.sample_rate
            yield r.sample_rate, (np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes()
        print(f"streamed {total:.1f}s audio, first piece {first or 0:.2f}s, total {time.perf_counter() - t:.1f}s", flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # don't log request lines (they'd be noise; text is never logged)
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": state["model"] is not None, "model": MODEL, "reference": REF_NAME,
                                    "error": state["error"]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/speak", "/stream"):
            return self._json(404, {"error": "not found"})
        if state["model"] is None:
            return self._json(503, {"error": state["error"] or "loading"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 10_000:
                return self._json(413, {"error": "too large"})
            body = json.loads(self.rfile.read(n))
            text, ref = str(body.get("text", "")).strip()[:MAX_CHARS], body.get("ref")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad request"})
        if not text:
            return self._json(400, {"error": "empty text"})
        if self.path == "/stream":
            return self._stream(text)
        try:
            wav = synth(text, ref)
        except KeyError:
            return self._json(400, {"error": "unknown reference"})
        except Exception as e:
            print("synthesis failed:", type(e).__name__, flush=True)
            return self._json(500, {"error": "synthesis failed"})
        self.send_response(200); self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav))); self.end_headers(); self.wfile.write(wav)


def _stream_impl(self, text: str) -> None:
    self.send_response(200)
    self.send_header("Content-Type", "application/octet-stream")
    self.send_header("X-Sample-Rate", str(state["model"].sample_rate))
    self.send_header("Transfer-Encoding", "chunked")
    self.end_headers()
    try:
        for _rate, pcm in synth_stream(text):
            self.wfile.write(f"{len(pcm):x}\r\n".encode() + pcm + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
    except (BrokenPipeError, ConnectionResetError):
        print("stream cancelled by client", flush=True)  # barge-in: listener hung up


Handler._stream = _stream_impl


if __name__ == "__main__":
    threading.Thread(target=load_with_retry, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

