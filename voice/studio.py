"""Voice recording studio: guided prompts, immutable raw takes, quality checks.

Layout under dataset_root:
  consent/voice-consent.json                        the owner's authorization (required before recording)
  raw/voice/<session>/<take>.wav                    original 48 kHz recording, never modified
  raw/voice/<session>/<take>.meta.json              capture metadata + sha256, written once
  derived/voice/qc-v1/<session>/<take>.json         quality report, transcript, keep/reject status
"""
from __future__ import annotations

import difflib
import hashlib
import io
import json
import re
import time
import wave
from pathlib import Path

import numpy as np
import yaml

from core.config import ROOT

QC_VERSION = "qc-v1"
MAX_TAKE_BYTES = 60 * 1024 * 1024  # about 10 minutes of 48 kHz 16-bit mono
SESSION_RE = re.compile(r"^\d{8}-\d{6}$")
TAKE_RE = re.compile(r"^take-\d{4}$")
CONSENT_TEXT = ("I, the person named below, authorize these recordings of my voice to be used to create "
                "Echo's voice and personality. They are to be processed locally on my own computer "
                "and not uploaded to any outside service without my separate permission.")


class StudioError(Exception):
    pass


def load_prompts() -> dict[str, dict]:
    """Flatten prompts.yaml into {prompt_id: {...}} with stable ids like 'calm-03'."""
    data = yaml.safe_load((ROOT / "voice" / "prompts.yaml").read_text())
    out = {}
    for cat, c in data["categories"].items():
        for i, text in enumerate(c["prompts"], 1):
            pid = f"{cat}-{i:02d}"
            out[pid] = {"id": pid, "category": cat, "text": text, "style": c["style"], "read": c.get("read", True)}
    return out


def read_wav(data: bytes) -> tuple[np.ndarray, int]:
    if len(data) > MAX_TAKE_BYTES:
        raise StudioError("recording too large")
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                raise StudioError("expected 16-bit mono WAV")
            rate, frames = w.getframerate(), w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise StudioError("not a valid WAV file") from e
    if not 8000 <= rate <= 96000:
        raise StudioError("unsupported sample rate")
    return np.frombuffer(frames, np.int16).astype(np.float32) / 32768.0, rate


def _db(x: float) -> float:
    return round(20 * np.log10(max(x, 1e-7)), 1)


def quality(audio: np.ndarray, rate: int) -> dict:
    """Objective checks that matter for voice cloning."""
    dur = len(audio) / rate
    if dur < 0.3:
        return {"duration_s": round(dur, 2), "verdict": "fail", "issues": ["Too short."]}
    frame = int(rate * 0.02)
    n = len(audio) // frame
    rms = np.sqrt(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1))
    peak = float(np.max(np.abs(audio)))
    clip_ratio = float(np.mean(np.abs(audio) >= 0.999))
    noise = float(np.percentile(rms, 10))
    speech = float(np.percentile(rms, 90))
    snr = _db(speech) - _db(noise)
    q = {"duration_s": round(dur, 2), "peak_dbfs": _db(peak), "speech_dbfs": _db(speech),
         "noise_dbfs": _db(noise), "snr_db": round(snr, 1), "clipped_pct": round(clip_ratio * 100, 3)}
    fail, warn = [], []
    if dur < 1.0 and n < 50:
        warn.append("Very short.")
    if clip_ratio > 0.001 or peak >= 0.999:
        fail.append("Too loud: the sound is clipping. Move back a little or lower the mic volume.")
    elif q["peak_dbfs"] > -1.0:
        warn.append("Very close to too loud.")
    if q["speech_dbfs"] < -42:
        fail.append("Too quiet. Move closer to the mic or speak up.")
    elif q["speech_dbfs"] < -32:
        warn.append("A bit quiet.")
    if snr < 15:
        fail.append("Too much background noise compared with your voice.")
    elif snr < 25:
        warn.append("Some background noise.")
    q["issues"] = fail + warn
    q["verdict"] = "fail" if fail else "warn" if warn else "good"
    return q


def _words(t: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", t.lower().replace("’", "'"))


def read_match(expected: str, heard: str) -> float:
    """How closely what Whisper heard matches the prompt, 0..1."""
    return round(difflib.SequenceMatcher(None, _words(expected), _words(heard)).ratio(), 2)


class Studio:
    def __init__(self, dataset_root: str | Path):
        self.root = Path(dataset_root)
        self.prompts = load_prompts()

    # ---- paths / availability ----
    def available(self) -> bool:
        return self.root.is_dir()

    def _require(self) -> None:
        if not self.available():
            raise StudioError(f"Dataset drive not found at {self.root}. Is the external SSD plugged in?")

    def _raw(self, session: str) -> Path:
        return self.root / "raw" / "voice" / session

    def _qc(self, session: str) -> Path:
        return self.root / "derived" / "voice" / QC_VERSION / session

    @staticmethod
    def _check_ids(session: str, take: str | None = None) -> None:
        if not SESSION_RE.match(session) or (take is not None and not TAKE_RE.match(take)):
            raise StudioError("bad session or take id")

    # ---- consent ----
    def consent(self) -> dict | None:
        p = self.root / "consent" / "voice-consent.json"
        return json.loads(p.read_text()) if p.exists() else None

    def give_consent(self, name: str) -> dict:
        self._require()
        if not name.strip():
            raise StudioError("name required")
        p = self.root / "consent" / "voice-consent.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"name": name.strip()[:100], "statement": CONSENT_TEXT, "granted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "scope": ["voice_cloning", "personality_modeling"], "processing": "local_only"}
        p.write_text(json.dumps(rec, indent=2))
        return rec

    # ---- sessions / takes ----
    def new_session(self) -> str:
        self._require()
        sid = time.strftime("%Y%m%d-%H%M%S")
        while self._raw(sid).exists():
            time.sleep(1)
            sid = time.strftime("%Y%m%d-%H%M%S")
        self._raw(sid).mkdir(parents=True)
        return sid

    def save_take(self, session: str, data: bytes, prompt_id: str, custom_text: str = "") -> dict:
        """Write the original WAV once, plus capture metadata. Returns take id and QC report."""
        self._require()
        if not self.consent():
            raise StudioError("consent required before recording")
        self._check_ids(session)
        if prompt_id != "custom" and prompt_id not in self.prompts:
            raise StudioError("unknown prompt")
        raw = self._raw(session)
        if not raw.is_dir():
            raise StudioError("unknown session")
        audio, rate = read_wav(data)
        n = len(list(raw.glob("take-*.wav"))) + 1
        take = f"take-{n:04d}"
        wav_path = raw / f"{take}.wav"
        with open(wav_path, "xb") as f:  # "x": never overwrite an existing original
            f.write(data)
        wav_path.chmod(0o444)  # read-only: originals are immutable
        p = self.prompts.get(prompt_id)
        meta = {"session": session, "take": take, "prompt_id": prompt_id,
                "category": p["category"] if p else "custom",
                "prompt_text": p["text"] if p else custom_text.strip()[:300],
                "read_aloud": p["read"] if p else False,
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sample_rate": rate, "channels": 1,
                "format": "wav/pcm_s16le", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "capture": {"echo_cancellation": False, "noise_suppression": False, "auto_gain": False},
                "consent": "consent/voice-consent.json"}
        meta_path = raw / f"{take}.meta.json"
        with open(meta_path, "x") as f:
            json.dump(meta, f, indent=2)
        meta_path.chmod(0o444)
        qc = {"take": take, "session": session, "prompt_id": prompt_id, "status": "pending",
              "qc_version": QC_VERSION, "source_sha256": meta["sha256"], **quality(audio, rate)}
        self._write_qc(session, take, qc)
        return qc

    def add_transcript(self, session: str, take: str, heard: str) -> dict:
        qc = self.get_qc(session, take)
        meta = json.loads((self._raw(session) / f"{take}.meta.json").read_text())
        qc["transcript"] = heard
        if meta["read_aloud"]:
            qc["read_match"] = read_match(meta["prompt_text"], heard)
            if qc["read_match"] < 0.8:
                qc["issues"] = qc.get("issues", []) + ["The words heard don't quite match the prompt. You may want to redo it."]
                if qc["verdict"] == "good":
                    qc["verdict"] = "warn"
        self._write_qc(session, take, qc)
        return qc

    def set_status(self, session: str, take: str, status: str) -> dict:
        if status not in ("kept", "rejected"):
            raise StudioError("bad status")
        qc = self.get_qc(session, take)
        qc["status"] = status
        self._write_qc(session, take, qc)
        return qc

    def get_qc(self, session: str, take: str) -> dict:
        self._check_ids(session, take)
        p = self._qc(session) / f"{take}.json"
        if not p.exists():
            raise StudioError("unknown take")
        return json.loads(p.read_text())

    def _write_qc(self, session: str, take: str, qc: dict) -> None:
        d = self._qc(session)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{take}.json.tmp"
        tmp.write_text(json.dumps(qc, indent=2))
        tmp.replace(d / f"{take}.json")  # atomic

    def raw_path(self, session: str, take: str) -> Path:
        self._check_ids(session, take)
        return self._raw(session) / f"{take}.wav"

    # ---- progress ----
    def stats(self) -> dict:
        kept_ids: set[str] = set()
        by_cat: dict[str, int] = {}
        kept_s = total_s = 0.0
        takes = 0
        base = self.root / "derived" / "voice" / QC_VERSION
        if base.is_dir():
            for f in base.glob("*/take-*.json"):
                qc = json.loads(f.read_text())
                takes += 1
                total_s += qc.get("duration_s", 0)
                if qc.get("status") == "kept":
                    kept_s += qc.get("duration_s", 0)
                    kept_ids.add(qc["prompt_id"])
                    cat = qc["prompt_id"].rsplit("-", 1)[0]
                    by_cat[cat] = by_cat.get(cat, 0) + 1
        raw_bytes = sum(p.stat().st_size for p in (self.root / "raw" / "voice").glob("*/*.wav")) \
            if (self.root / "raw" / "voice").is_dir() else 0
        return {"takes": takes, "kept_minutes": round(kept_s / 60, 1), "recorded_minutes": round(total_s / 60, 1),
                "kept_prompt_ids": sorted(kept_ids), "kept_by_category": by_cat,
                "raw_mb": round(raw_bytes / 1e6, 1), "target_minutes": 30}
