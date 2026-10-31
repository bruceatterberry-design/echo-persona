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
LIVE_MAX = 30              # newest room-mic prints kept per person ("live:…" sources; no audio is stored)
LIVE_MIN = 3               # room prints needed before they count as a second centroid
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")


class SpeakerID:
    def __init__(self, model: str | Path, profiles_dir: str | Path, threshold: float = 0.5, margin: float = 0.1):
        self.model, self.dir = Path(model), Path(profiles_dir)
        self.threshold, self.margin = threshold, margin
        self._ex = None
        self._lock = threading.Lock()
        self.profiles: dict[str, np.ndarray] = {}   # name -> centroids (k × dim, unit length): recordings, room mic
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
        """Each person: one centroid from their recordings, plus one from live room-mic prints once there are LIVE_MIN
        (the owner's studio prints scored only 0.30-0.44 through the room mic, so Echo kept calling him "unknown")."""
        self.profiles = {}
        for f in sorted(self.dir.glob("*.npz")):
            d = np.load(f)
            prints = d["prints"]
            srcs = json.loads(str(d["sources"])) if "sources" in d else [""] * len(prints)
            live = np.array([str(s).startswith("live:") for s in srcs[:len(prints)]] + [False] * (len(prints) - len(srcs)))
            groups = [prints[~live]] if (~live).any() else []
            if live.sum() >= LIVE_MIN:
                groups.append(prints[live])
            if not groups:
                continue
            cents = [g.mean(axis=0) for g in groups]
            self.profiles[f.stem] = np.stack([c / (np.linalg.norm(c) + 1e-9) for c in cents])

    def add_live(self, name: str, emb: np.ndarray, source: str) -> int:
        """Add one room-mic print (from a phrase that was confidently or confirmed this person). Keeps the newest
        LIVE_MAX. Returns how many live prints the person now has."""
        f = self.dir / f"{name}.npz"
        if not NAME_RE.match(name) or not f.exists():
            raise ValueError("no such profile")
        with self._lock:
            d = np.load(f)
            prints, srcs = list(d["prints"]), json.loads(str(d["sources"]))
            prints.append(np.asarray(emb, dtype=np.float32)); srcs.append(f"live:{source}:{time.strftime('%Y-%m-%d %H:%M')}")
            live_idx = [i for i, s in enumerate(srcs) if s.startswith("live:")]
            for i in live_idx[:-LIVE_MAX]:   # drop the oldest room prints beyond the cap
                prints[i] = None
            keep = [i for i, p in enumerate(prints) if p is not None]
            np.savez(f, prints=np.stack([prints[i] for i in keep]), sources=json.dumps([srcs[i] for i in keep]),
                     updated=time.time())
        self.reload()
        return min(len(live_idx), LIVE_MAX)

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
        return self._scores(self.embed(audio))

    def _scores(self, v: np.ndarray) -> dict[str, float]:
        return {n: float(np.max(c @ v)) for n, c in self.profiles.items()}   # the closer of recordings / room mic

    def identify(self, audio: np.ndarray) -> dict:
        """{"speaker": name or "unknown", "score": best similarity, "scores": {...}} — "unknown" when the phrase
        is too short, no profile matches strongly, or two people match almost equally."""
        if not self.profiles or not self.available or len(audio) < MIN_SECONDS * SR:
            return {"speaker": "unknown", "score": 0.0, "scores": {}}
        v = self.embed(audio)
        sc = self._scores(v)
        ranked = sorted(sc.items(), key=lambda x: -x[1])
        best, b = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else -1.0
        who = best if b >= self.threshold and b - second >= self.margin else "unknown"
        return {"speaker": who, "score": round(b, 3), "scores": {k: round(x, 3) for k, x in sc.items()},
                "best": best, "gap": round(b - second, 3), "emb": v}
