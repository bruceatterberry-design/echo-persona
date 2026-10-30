"""Process verified phone sessions (raw/phone/<id>/) into Echo's datasets. Originals are never modified.

Per session, once (marker: derived/phone/ingest-v1/<id>/done.json; delete it to reprocess):
  voice WAV        → quality + local Whisper transcript + read-match → derived/voice/qc-v1/phone-<id>/<take>.json
                     (counts in the Voice Studio, same format as studio takes)
  face/answer MP4  → frames at 2 fps (ffmpeg) → Apple Vision face check → best frames per take kept under
                     derived/face/phone-frames-v1/<id>/<take>/ ; audio transcribed
  answer MP4       → also an example of how the owner talks → derived/personality/examples-v1.jsonl (for review later)
  photo JPG        → copied to staging/face/photos-current/phone/ so the owner reviews it in the Face Library
  memory WAV       → ("Tell me a memory") transcribed in ~60 s pieces → derived/memories/transcripts-v1/<id>/<take>.json,
                     then on_memory(...) so the server can suggest memories for the owner to review (memory/candidates.py)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from face.library import FACEFIND, brightness, sharpness
from voice.studio import QC_VERSION, quality, read_match, read_wav
from voice.stt import STTBackend, decode_audio

VERSION = "ingest-v1"
FRAMES_PER_SECOND = 2
KEEP_FRAMES = 6


def _transcribe(stt: STTBackend, data: bytes) -> str:
    return stt.transcribe_sync(decode_audio(data))


def transcribe_long(stt: STTBackend, audio, piece_s: int = 60) -> str:
    """Long recordings in ~60 s pieces, each cut at the quietest moment of its last 10 s (not mid-word)."""
    import numpy as np
    sr, out, i = 16000, [], 0
    while i < len(audio):
        end = min(len(audio), i + piece_s * sr)
        if end < len(audio):
            win = audio[end - 10 * sr:end]
            e = np.convolve(win ** 2, np.ones(1600) / 1600, mode="valid")   # 0.1 s energy
            end = end - 10 * sr + int(np.argmin(e)) + 800
        out.append(stt.transcribe_sync(audio[i:end]).strip())
        i = end
    return " ".join(t for t in out if t)


def _video_audio(mp4: Path) -> bytes:
    return subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(mp4), "-vn", "-ac", "1", "-ar", "16000",
                           "-f", "wav", "pipe:1"], capture_output=True, timeout=300, check=True).stdout


def _best_frames(mp4: Path, out: Path) -> dict:
    """Sample frames, find the face in each with Apple Vision, keep the sharpest well-framed ones."""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(mp4), "-vf", f"fps={FRAMES_PER_SECOND}",
                        "-q:v", "2", str(tdp / "f%04d.jpg")], timeout=600, check=True)
        frames = sorted(tdp.glob("f*.jpg"))
        if not frames or not FACEFIND.exists():
            return {"frames": len(frames), "with_face": 0, "kept": []}
        r = subprocess.run([str(FACEFIND), str(tdp / "crops")] + [str(f) for f in frames],
                           capture_output=True, text=True, timeout=600)
        scored = []
        for line in filter(None, r.stdout.splitlines()):
            rec = json.loads(line)
            if len(rec.get("faces", [])) != 1:      # only frames where the owner is alone in view
                continue
            f = rec["faces"][0]
            crop = tdp / "crops" / f["crop"]
            s = sharpness(crop)
            scored.append((f["quality"] * 100 + min(s, 400) / 10 - abs(f["yaw"]) * 20, rec["image"], f, s, brightness(crop)))
        scored.sort(key=lambda x: -x[0])
        out.mkdir(parents=True, exist_ok=True)
        kept = []
        for score, img, f, s, b in scored[:KEEP_FRAMES]:
            name = Path(img).name
            shutil.copyfile(img, out / name)
            kept.append({"frame": name, "time_s": round((int(name[1:5]) - 1) / FRAMES_PER_SECOND, 1), "face_px": f["face_px"],
                         "vision_quality": round(f["quality"], 2), "sharpness": s, "brightness": b, "yaw": round(f["yaw"], 2)})
        return {"frames": len(frames), "with_face": len(scored), "kept": kept}


def process_session(root: Path, sid: str, stt: STTBackend, progress: Callable[[str], None] = lambda m: None,
                    on_memory: Callable[[dict], None] | None = None) -> dict:
    src = root / "raw" / "phone" / sid
    marker = root / "derived" / "phone" / VERSION / sid / "done.json"
    if marker.exists():
        return json.loads(marker.read_text())
    manifest = json.loads((src / "manifest.json").read_text())
    report = {"session": sid, "version": VERSION, "voice": 0, "videos": 0, "photos": 0, "answers": 0, "frames_kept": 0,
              "memories": 0, "errors": []}
    qc_dir = root / "derived" / "voice" / QC_VERSION / f"phone-{sid}"
    frames_dir = root / "derived" / "face" / "phone-frames-v1" / sid
    examples = root / "derived" / "personality" / "examples-v1.jsonl"
    photo_drop = root / "staging" / "face" / "photos-current" / "phone"
    for t in manifest["takes"]:
        if t.get("status") == "rejected":
            continue
        f = src / t["file"]
        progress(f"{sid}/{t['file']}")
        try:
            if f.suffix == ".wav":
                audio, rate = read_wav(f.read_bytes())
                qc = {"take": t["take"], "session": f"phone-{sid}", "prompt_id": t["task_id"], "status": t["status"],
                      "qc_version": QC_VERSION, "source": str(f.relative_to(root)), "source_sha256": t["sha256"],
                      **quality(audio, rate)}
                is_memory = t.get("task_kind") == "memory"
                heard = transcribe_long(stt, decode_audio(f.read_bytes())) if is_memory else _transcribe(stt, f.read_bytes())
                qc["transcript"] = heard
                if t.get("prompt_text") and not is_memory:   # a memory answers a question; nothing to read-match
                    qc["read_match"] = read_match(t["prompt_text"], heard)
                if is_memory and heard.strip():
                    told = {"source": str(f.relative_to(root)), "source_sha256": t["sha256"], "session": sid,
                            "take": t["take"], "question": t.get("prompt_text"), "period": t.get("life_period"),
                            "recorded_at": t.get("recorded_at"), "transcript": heard}
                    tdir = root / "derived" / "memories" / "transcripts-v1" / sid
                    tdir.mkdir(parents=True, exist_ok=True)
                    (tdir / f"{t['take']}.json").write_text(json.dumps(told, indent=2))
                    report["memories"] += 1
                    if on_memory:
                        on_memory(told)
                qc_dir.mkdir(parents=True, exist_ok=True)
                (qc_dir / f"{t['take']}.json").write_text(json.dumps(qc, indent=2))
                report["voice"] += 1
            elif f.suffix == ".mp4":
                info = _best_frames(f, frames_dir / t["take"])
                heard = _transcribe(stt, _video_audio(f))
                info |= {"task_id": t["task_id"], "expression_label": t.get("expression_label"), "transcript": heard,
                         "source": str(f.relative_to(root)), "source_sha256": t["sha256"]}
                (frames_dir / t["take"]).mkdir(parents=True, exist_ok=True)
                (frames_dir / t["take"] / "frames.json").write_text(json.dumps(info, indent=2))
                report["videos"] += 1
                report["frames_kept"] += len(info["kept"])
                if t.get("task_kind") == "answer" and heard.strip():
                    examples.parent.mkdir(parents=True, exist_ok=True)
                    with open(examples, "a") as ex:   # evidence of how the owner talks; reviewed before any use
                        ex.write(json.dumps({"source": str(f.relative_to(root)), "session": sid, "question": t.get("prompt_text"),
                                             "answer": heard, "recorded_at": t.get("recorded_at"), "reviewed": False}) + "\n")
                    report["answers"] += 1
            elif f.suffix == ".jpg":
                photo_drop.mkdir(parents=True, exist_ok=True)
                dst = photo_drop / f"{sid}-{f.name}"
                if not dst.exists():
                    shutil.copyfile(f, dst)
                report["photos"] += 1
        except Exception as e:   # one bad file never stops the rest
            report["errors"].append(f"{t['file']}: {type(e).__name__}")
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(report, indent=2))
    return report


def pending(root: Path) -> list[str]:
    raw = root / "raw" / "phone"
    if not raw.is_dir():
        return []
    return sorted(p.name for p in raw.iterdir() if (p / "receipt.json").exists()
                  and not (root / "derived" / "phone" / VERSION / p.name / "done.json").exists())


def process_all(root: Path, stt: STTBackend, progress: Callable[[str], None] = lambda m: None,
                on_memory: Callable[[dict], None] | None = None) -> list[dict]:
    return [process_session(root, sid, stt, progress, on_memory) for sid in pending(root)]
