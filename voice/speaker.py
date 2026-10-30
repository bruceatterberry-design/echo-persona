"""Who is talking? Local speaker recognition (sherpa-onnx + a small voice-print model, CPU, ~10 ms per phrase).

Each known person has a voice profile: voice prints (embeddings) from a few of their recordings. A new phrase is
compared with every profile (cosine similarity). It's only called a person when the match is strong *and* clearly
better than the next person; otherwise "unknown". Nothing is sent anywhere; audio isn't stored by this module.

Profiles: data/speakers/<name>.npz (prints + where they came from). Built by enroll(); see docs/SPEAKERS.md.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import numpy as np

SR = 16000
MIN_SECONDS = 1.0          # shorter phrases carry too little voice to tell people apart
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")


class SpeakerID:
    def __init__(self, model: str | Path, profiles_dir: str | Path, threshold: float = 0.5, margin: float = 0.1):
        self.model, self.dir = Path(model), Path(profiles_dir)
        self.threshold, self.margin = threshold, margin
        self._ex = None
        self._lock = threading.Lock()
        self.profiles: dict[str, np.ndarray] = {}   # name -> mean print (unit length)
        self.reload()

    @property
    def available(self) -> bool:
        return self.model.exists()

    def _extractor(self):
        if self._ex is None:
            import sherpa_onnx
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(self.model), num_threads=1, provider="cpu")
            self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        return self._ex

    def embed(self, audio: np.ndarray) -> np.ndarray:
        with self._lock:
            ex = self._extractor()
            s = ex.create_stream()
            s.accept_waveform(sample_rate=SR, waveform=np.ascontiguousarray(audio, dtype=np.float32))
            s.input_finished()
            v = np.array(ex.compute(s), dtype=np.float32)
        return v / (np.linalg.norm(v) + 1e-9)

    # ---- profiles ----
    def reload(self) -> None:
        self.profiles = {}
        for f in sorted(self.dir.glob("*.npz")):
            prints = np.load(f)["prints"]
            m = prints.mean(axis=0)
            self.profiles[f.stem] = m / (np.linalg.norm(m) + 1e-9)

    def enroll(self, name: str, clips: list[np.ndarray], sources: list[str], replace: bool = False) -> dict:
        """Add voice prints for `name` (each clip ≥ MIN_SECONDS). Returns a summary of the profile."""
        if not NAME_RE.match(name):
            raise ValueError("bad name")
        prints, srcs = [], []
        for clip, src in zip(clips, sources):
            if len(clip) >= MIN_SECONDS * SR:
                prints.append(self.embed(clip)); srcs.append(src)
        if not prints:
            raise ValueError("no clip was long enough (need at least 1 s of speech)")
        self.dir.mkdir(parents=True, exist_ok=True)
        f = self.dir / f"{name}.npz"
        if f.exists() and not replace:
            old = np.load(f)
            prints = list(old["prints"]) + prints
            srcs = json.loads(str(old["sources"])) + srcs
        np.savez(f, prints=np.stack(prints), sources=json.dumps(srcs), updated=time.time())
        self.reload()
        return self.info(name)

    def info(self, name: str) -> dict:
        f = self.dir / f"{name}.npz"
        if not f.exists():
            return {"name": name, "samples": 0}
        d = np.load(f)
        return {"name": name, "samples": int(len(d["prints"])), "updated": float(d["updated"])}

    def forget(self, name: str) -> bool:
        f = self.dir / f"{name}.npz"
        if NAME_RE.match(name) and f.exists():
            f.unlink(); self.reload(); return True
        return False

    # ---- recognition ----
    def scores(self, audio: np.ndarray) -> dict[str, float]:
        v = self.embed(audio)
        return {n: float(v @ p) for n, p in self.profiles.items()}

    def identify(self, audio: np.ndarray) -> dict:
        """{"speaker": name or "unknown", "score": best similarity, "scores": {...}} — "unknown" when the phrase
        is too short, no profile matches strongly, or two people match almost equally."""
        if not self.profiles or not self.available or len(audio) < MIN_SECONDS * SR:
            return {"speaker": "unknown", "score": 0.0, "scores": {}}
        sc = self.scores(audio)
        ranked = sorted(sc.items(), key=lambda x: -x[1])
        best, b = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else -1.0
        who = best if b >= self.threshold and b - second >= self.margin else "unknown"
        return {"speaker": who, "score": round(b, 3), "scores": {k: round(v, 3) for k, v in sc.items()}}
