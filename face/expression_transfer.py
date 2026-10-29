"""Carry your recorded expressions (joy, surprised, sad, angry, bewildered) onto the current face photo.

LivePortrait describes a face's expression as keypoint offsets ("exp"). For each feeling we measure how your face
moved from your relaxed phone photo to the expression (photo, and the strongest frame of the video), then add that
movement to the current face photo. Everything else about the photo (glasses, beard, background) stays as it is.

Runs in LivePortrait's own environment:
  PYTORCH_ENABLE_MPS_FALLBACK=1 ~/EchoPersona/animate/.venv/bin/python face/expression_transfer.py sheet <out_dir> [session]
  PYTORCH_ENABLE_MPS_FALLBACK=1 ~/EchoPersona/animate/.venv/bin/python face/expression_transfer.py atlas <deltas.json> <out_dir> [base atlas] [feeling,...]

sheet: preview (A photo 60%, B photo 100%, C video peak) per feeling + expression-deltas.json.
atlas: the current atlas plus one set of frames per chosen feeling (choices in config/face_expressions.json).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).parent))
from render_face import (OUT_SIZE, SCALE, VIEW, VISEMES, POSES, Renderer, combine, face_region,  # noqa: E402
                         plan_frames)  # (importing render_face sets up LivePortrait)

CONFIG = Path(__file__).resolve().parent.parent / "config"


def dataset_root() -> Path:
    """dataset_root from config/echo.yaml (this env has no YAML library, and only that one line is needed)."""
    cfg = CONFIG / "echo.yaml"
    m = re.search(r"""^dataset_root:\s*["']?([^"'#\n]+?)["']?\s*(?:#.*)?$""", cfg.read_text(), re.M) if cfg.exists() else None
    return Path(m.group(1)).expanduser() if m else Path("/Volumes/External storage/EchoPersona-Data")


def newest_atlas(face: Path) -> Path:
    done = [p for p in face.glob("atlas-v*") if re.fullmatch(r"atlas-v\d+", p.name) and (p / "atlas.json").exists()]
    return max(done, key=lambda p: int(p.name[7:]))


def relaxed_photo(phone: Path) -> Path:
    """The newest Collector "Photo: straight on" take: you relaxed, looking at the camera."""
    for meta in sorted(phone.glob("*/take-*.json"), reverse=True):
        if json.loads(meta.read_text()).get("task_id") == "photo-straight" and meta.with_suffix(".jpg").exists():
            return meta.with_suffix(".jpg")
    sys.exit('No relaxed photo yet: record a Collector session with "Photo: straight on" first.')


DATA = dataset_root()
CURRENT = newest_atlas(DATA / "derived" / "face")              # the atlas the new feelings are added to
SESSION = max((DATA / "raw" / "phone").glob("*"), default=None)   # newest phone session (default for "sheet")
CHOICES = CONFIG / "face_expressions.json"
EMOTIONS = ["joy", "surprised", "sad", "angry", "bewildered"]
LIPS = [6, 12, 14, 17, 19, 20]   # LivePortrait's lip keypoints; the rest of the face keeps the full expression
# How much of the recorded mouth to keep. While talking, the mouth shapes follow the voice (a carried-over open
# mouth can make every feeling look "wide open"). Overridable per feeling in config/face_expressions.json.
MOUTH_REST, MOUTH_TALKING = 0.5, 0.0


def with_mouth(delta: torch.Tensor, keep: float) -> torch.Tensor:
    d = delta.clone()
    d[:, LIPS] *= keep
    return d


def takes(label: str, session: Path = SESSION) -> tuple[Path, Path]:
    """(photo, video) of one feeling from the expression session (the last kept take of each kind)."""
    photo = video = None
    for meta in sorted(session.glob("take-*.json")):
        d = json.loads(meta.read_text())
        if d.get("expression_label") == label:
            if d.get("task_id", "").startswith("photo-"):
                photo = meta.with_suffix(".jpg")
            elif d.get("task_id", "").startswith("face-"):
                video = meta.with_suffix(".mp4")
    return photo, video


class Expressions:
    def __init__(self, r: Renderer):
        self.r, self.pipe = r, r.pipe
        self.extra: torch.Tensor | None = None
        orig = self.pipe.prepare_retargeting_image

        def prepare(*a, **k):   # add the carried-over expression to the photo's own one
            out = list(orig(*a, **k))
            if self.extra is not None:
                out[4] = dict(out[4], exp=out[4]["exp"] + self.extra.to(out[4]["exp"].device))
            return tuple(out)
        self.pipe.prepare_retargeting_image = prepare

    @torch.no_grad()
    def exp(self, img_bgr: np.ndarray) -> torch.Tensor | None:
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        self.pipe.cropper.update_config({"scale": SCALE})
        crop = self.pipe.cropper.crop_source_image(rgb, self.pipe.cropper.crop_cfg)
        if crop is None:
            return None
        w = self.pipe.live_portrait_wrapper
        return w.get_kp_info(w.prepare_source(crop["img_crop_256x256"]))["exp"].cpu()

    def video_peak(self, video: Path, neutral: torch.Tensor) -> tuple[torch.Tensor, np.ndarray]:
        """The frame that moved furthest from relaxed (10 frames a second)."""
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(video), "-vf", "fps=10", f"{tmp}/f%03d.jpg"], check=True)
            best = (-1.0, None, None)
            for f in sorted(Path(tmp).glob("f*.jpg")):
                img = cv2.imread(str(f))
                e = self.exp(img)
                if e is not None and (d := float((e - neutral).norm())) > best[0]:
                    best = (d, e, img)
        return best[1] - neutral, best[2]

    def render(self, delta: torch.Tensor | None, strength: float = 1.0) -> np.ndarray:
        self.extra = None if delta is None else delta * strength
        try:
            return self.r.frame()
        finally:
            self.extra = None


def label(tile, text):
    cv2.putText(tile, text, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 4)
    cv2.putText(tile, text, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return tile


def own_tile(img):
    """Your own phone photo, cropped to your face for comparison."""
    h, w = img.shape[:2]
    s = int(min(h, w) * 0.75)
    cy, cx = int(h * 0.42), w // 2
    return cv2.resize(img[max(0, cy - s // 2): cy + s // 2, max(0, cx - s // 2): cx + s // 2], (300, 300))


def sheet(out: Path, session: Path = SESSION):
    out.mkdir(parents=True, exist_ok=True)
    r = Renderer(CURRENT / "source-working.jpg", out / "work")
    x = Expressions(r)
    neutral_photo = relaxed_photo(DATA / "raw" / "phone")
    neutral = x.exp(cv2.imread(str(neutral_photo)))
    rows, deltas = [], {}
    base = label(face_region(x.render(None), r), "Avatar now")
    for name in EMOTIONS:
        photo, video = takes(name, session)
        if not photo or not video:
            print(f"  {name}: not in this session, skipped", flush=True)
            continue
        pimg = cv2.imread(str(photo))
        d_photo = x.exp(pimg) - neutral
        d_video, vimg = x.video_peak(video, neutral)
        deltas[name] = {"photo": d_photo.numpy().tolist(), "video": d_video.numpy().tolist()}
        rows.append(np.hstack([
            label(own_tile(pimg), f"You: {name}"),
            base.copy(),
            label(face_region(x.render(d_photo, 0.6), r), "A: photo 60%"),
            label(face_region(x.render(d_photo, 1.0), r), "B: photo 100%"),
            label(face_region(x.render(d_video, 1.0), r), "C: video peak"),
        ]))
        print(f"  {name} done", flush=True)
    cv2.imwrite(str(out / "expressions-sheet.jpg"), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 85])
    (out / "expression-deltas.json").write_text(json.dumps({"neutral": str(neutral_photo), "session": str(session),
                                                            "deltas": deltas}))
    print("sheet:", out / "expressions-sheet.jpg", flush=True)


def atlas(deltas_file: Path, out: Path, current: Path | None = None, only: list[str] | None = None):
    """Copy the current atlas, then render every pose x mouth x eye frame for each chosen feeling."""
    choices = json.loads(CHOICES.read_text())["choices"]          # {"joy": {"from": "photo", "strength": 1.0}, ...}
    if only:   # re-render just these feelings; the others keep their frames from the base atlas
        choices = {k: v for k, v in choices.items() if k in only}
    deltas = json.loads(deltas_file.read_text())["deltas"]
    if out.exists():
        sys.exit(f"{out} already exists; pick a new version folder")
    final, out = out, out.with_name(out.name + "-partial")   # renamed when done, so Echo never loads half an atlas
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(current or CURRENT, out)
    meta = json.loads((out / "atlas.json").read_text())
    r = Renderer(out / "source-working.jpg", out / "work")
    x = Expressions(r)
    eyes = meta["eye_levels"]
    todo = [f for f in plan_frames() if f[1] == "neutral"]            # same layout as every other expression
    t0, n, (vx, vy, vw, vh) = time.perf_counter(), 0, VIEW
    for name, c in choices.items():
        full = torch.tensor(deltas[name][c["from"]]) * c.get("strength", 1.0)
        for pose, _neutral, _mi, mouth, ei in todo:
            keep = c.get("mouth_rest", MOUTH_REST) if mouth == "rest" else c.get("mouth_talking", MOUTH_TALKING)
            x.extra = with_mouth(full, keep)
            fname = f"{pose}-{name}-m{list(VISEMES).index(mouth)}-e{ei}.jpg"
            img = r.frame(**combine(VISEMES[mouth], POSES[pose], {"eye": eyes[ei]}))
            img = cv2.resize(img[vy:vy + vh, vx:vx + vw], OUT_SIZE, interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(out / "frames" / fname), img, [cv2.IMWRITE_JPEG_QUALITY, 88])
            meta["frames"][f"{pose}/{name}/{mouth}/{ei}"] = f"frames/{fname}"
            n += 1
        x.extra = None
        if name not in meta["expressions"]:   # re-rendering a feeling replaces its frames
            meta["expressions"].append(name)
        print(f"  {name}: {len(todo)} frames ({time.perf_counter() - t0:.0f}s)", flush=True)
    meta |= {"version": int(final.name.split("-v")[-1]),
             "carried_expressions": {"from": str(deltas_file), "choices": json.loads(CHOICES.read_text())["choices"]},
             "render_seconds_added": round(time.perf_counter() - t0, 1)}
    (out / "atlas.json").write_text(json.dumps(meta, indent=2))
    shutil.rmtree(out / "work", ignore_errors=True)
    out.rename(final)
    print(f"atlas: {final} (+{n} frames)", flush=True)


def mouth_sheet(deltas_file: Path, out: Path):
    """Quick check before a full atlas: each chosen feeling at rest and talking, with the mouth settings."""
    choices = json.loads(CHOICES.read_text())["choices"]
    deltas = json.loads(deltas_file.read_text())["deltas"]
    r = Renderer(CURRENT / "source-working.jpg", out / "work")
    x = Expressions(r)
    eyes = json.loads((CURRENT / "atlas.json").read_text())["eye_levels"]
    tiles = []
    for name, c in choices.items():
        full = torch.tensor(deltas[name][c["from"]]) * c.get("strength", 1.0)
        for mouth, keep in (("rest", c.get("mouth_rest", MOUTH_REST)), ("relaxed2", c.get("mouth_talking", MOUTH_TALKING)),
                            ("round3", c.get("mouth_talking", MOUTH_TALKING))):
            x.extra = with_mouth(full, keep)
            tiles.append(label(face_region(r.frame(**combine(VISEMES[mouth], {"eye": eyes[0]})), r), f"{name} {mouth}"))
    x.extra = None
    rows = [np.hstack(tiles[i:i + 3]) for i in range(0, len(tiles), 3)]
    cv2.imwrite(str(out / "mouth-sheet.jpg"), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 85])
    shutil.rmtree(out / "work", ignore_errors=True)
    print("sheet:", out / "mouth-sheet.jpg", flush=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["sheet"] and len(sys.argv) in (3, 4):
        sheet(Path(sys.argv[2]), Path(sys.argv[3]) if len(sys.argv) == 4 else SESSION)
    elif sys.argv[1:2] == ["mouths"] and len(sys.argv) == 4:
        mouth_sheet(Path(sys.argv[2]), Path(sys.argv[3]))
    elif sys.argv[1:2] == ["atlas"] and len(sys.argv) in (4, 5, 6):   # ... [base atlas] [feeling,feeling]
        atlas(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]) if len(sys.argv) >= 5 else None,
              sys.argv[5].split(",") if len(sys.argv) == 6 else None)
    else:
        sys.exit(__doc__)
