"""Pre-render Echo's face pieces from one approved photo with LivePortrait's retargeting controls.

Runs in LivePortrait's own environment:
  PYTORCH_ENABLE_MPS_FALLBACK=1 animate/.venv/bin/python face/render_face.py calibrate|atlas|atlas5 <photo> <out_dir>  (or scripts/build_face.sh)

calibrate: a labelled contact sheet of test settings (to choose values).
atlas:     every expression x mouth level x eye state as JPEG frames + atlas.json for the window.
"""
from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path

ANIMATE = Path(__file__).resolve().parents[1] / "animate"
sys.path.insert(0, str(ANIMATE))

# LivePortrait's retargeting lives in its web-UI module, which imports gradio. Echo doesn't install gradio,
# so provide the two names that module uses (they're only for error popups in the web UI).
_gr = types.ModuleType("gradio")
_gr.Error = type("Error", (Exception,), {"__init__": lambda self, *a, **k: Exception.__init__(self, *a)})
_gr.Info = lambda *a, **k: None
sys.modules["gradio"] = _gr

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from src.config.argument_config import ArgumentConfig  # noqa: E402
from src.config.crop_config import CropConfig  # noqa: E402
from src.config.inference_config import InferenceConfig  # noqa: E402
from src.gradio_pipeline import GradioPipeline  # noqa: E402

MAX_SIDE = 1280      # work on a downscaled copy: faster, and plenty for the window
SCALE = 2.3          # crop scale around the face (LivePortrait default for retargeting)

NEUTRAL = dict(input_head_pitch_variation=0.0, input_head_yaw_variation=0.0, input_head_roll_variation=0.0,
               mov_x=0.0, mov_y=0.0, mov_z=1.0, lip_variation_zero=0.0, lip_variation_one=0.0,
               lip_variation_two=0.0, lip_variation_three=0.0, smile=0.0, wink=0.0, eyebrow=0.0,
               eyeball_direction_x=0.0, eyeball_direction_y=0.0)

# Conservative expressions (the spec asks for restrained, natural displays).
EXPRESSIONS = {
    "neutral":    {},
    "smiling":    {"smile": 0.9},
    "thoughtful": {"eyebrow": 6.0, "eyeball_direction_x": 6.0, "eyeball_direction_y": 4.0},
    "concerned":  {"eyebrow": -8.0, "smile": -0.15},
    "attentive":  {"eyebrow": 4.0},
}


# Mouth positions, chosen from calibration sheets on The owner's photo.
# v4 (The owner: v3 looked like gritted teeth, "not relaxed and casual"): speech is driven by the JAW. Four jaw heights
# ("lip" ratio) per lip style, so the teeth part and close smoothly; lips only add rounding or a slight spread.
# No lip-tightening (lip_variation_three) except a soft closure for m/b/p. "pout" is unused: it skews the lips sideways.
JAW = [0.06, 0.12, 0.19, 0.27]
STYLES = {
    "relaxed": {},                                                  # ah, uh, and the default
    "spread":  {"lip_variation_two": 2.0},                          # ee, eh (very slight)
    "round":   {"lip_variation_one": 12.0, "smile": -0.2},          # oo, oh, w
}
VISEMES = {"rest": {}, "mbp": {"lip_variation_three": -25.0}}
for _style, _settings in STYLES.items():
    for _j, _lip in enumerate(JAW, 1):
        VISEMES[f"{_style}{_j}"] = {"lip": _lip} | _settings


def combine(*parts: dict) -> dict:
    """Merge settings; numeric controls that appear twice add up (e.g. a smile plus a rounded mouth)."""
    out: dict = {}
    for p in parts:
        for k, v in p.items():
            out[k] = out[k] + v if k in out and k not in ("eye", "lip") else v
    return out


def fields(cls, d):
    return cls(**{k: v for k, v in d.items() if hasattr(cls, k)})


class Renderer:
    def __init__(self, photo: Path, work: Path):
        args = ArgumentConfig()
        self.pipe = GradioPipeline(fields(InferenceConfig, args.__dict__), fields(CropConfig, args.__dict__), args)
        img = cv2.imread(str(photo))
        h, w = img.shape[:2]
        k = MAX_SIDE / max(h, w)
        if k < 1:
            img = cv2.resize(img, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA)
        work.mkdir(parents=True, exist_ok=True)
        self.src = work / "source-working.jpg"   # a copy; the original photo is never modified
        cv2.imwrite(str(self.src), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        self.eye0, self.lip0 = self.pipe.init_retargeting_image(SCALE, 0.0, 0.0, str(self.src))
        print(f"source eye ratio {self.eye0}, lip ratio {self.lip0}", flush=True)

    def frame(self, eye=None, lip=None, **expr):
        p = NEUTRAL | expr
        _crop, full = self.pipe.execute_image_retargeting(
            self.eye0 if eye is None else eye, self.lip0 if lip is None else lip,
            p["input_head_pitch_variation"], p["input_head_yaw_variation"], p["input_head_roll_variation"],
            p["mov_x"], p["mov_y"], p["mov_z"], p["lip_variation_zero"], p["lip_variation_one"],
            p["lip_variation_two"], p["lip_variation_three"], p["smile"], p["wink"], p["eyebrow"],
            p["eyeball_direction_x"], p["eyeball_direction_y"], str(self.src), SCALE)
        return cv2.cvtColor(full, cv2.COLOR_RGB2BGR)


def face_region(img, r: Renderer):
    """Crop around the face for contact sheets."""
    h, w = img.shape[:2]
    s = min(h, w) // 2
    cy, cx = int(h * 0.36), w // 2
    return cv2.resize(img[max(0, cy - s // 2): cy + s // 2, max(0, cx - s // 2): cx + s // 2], (300, 300))


def calibrate(r: Renderer, out: Path):
    tests = [("source", {}), ("lip 0.20", {"lip": 0.20}), ("lip 0.35", {"lip": 0.35}), ("lip 0.50", {"lip": 0.50}),
             ("eyes 0.0", {"eye": 0.0}), ("eyes 0.15", {"eye": 0.15}), ("smile 0.7", {"smile": 0.7}),
             ("smile 1.0", {"smile": 1.0}), ("brow +8", {"eyebrow": 8.0}), ("brow -8", {"eyebrow": -8.0}),
             ("look 6,4", {"eyeball_direction_x": 6.0, "eyeball_direction_y": 4.0}),
             ("yaw +5", {"input_head_yaw_variation": 5.0})]
    tiles, times = [], []
    for label, kw in tests:
        t = time.perf_counter()
        img = r.frame(**kw)
        times.append(time.perf_counter() - t)
        tile = face_region(img, r)
        cv2.putText(tile, label, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        tiles.append(tile)
    rows = [np.hstack(tiles[i:i + 4]) for i in range(0, len(tiles), 4)]
    cv2.imwrite(str(out / "calibration.jpg"), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 85])
    print(f"per frame: first {times[0]:.2f}s, then avg {np.mean(times[1:]):.2f}s", flush=True)


def sheet(r: Renderer, out: Path, spec: Path, name: str):
    """Contact sheet from a JSON list of [label, settings] pairs (for choosing mouth shapes)."""
    tests = json.loads(spec.read_text())
    tiles = []
    for label, kw in tests:
        tile = face_region(r.frame(**kw), r)
        tile = tile[110:300, 40:260]  # mouth and chin
        tile = cv2.resize(tile, (330, 285))
        cv2.putText(tile, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
        tiles.append(tile)
    while len(tiles) % 4:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [np.hstack(tiles[i:i + 4]) for i in range(0, len(tiles), 4)]
    cv2.imwrite(str(out / name), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 85])


# Head positions (degrees). The owner asked for natural head movement; small angles look natural and keep the background intact.
POSES = {
    "C": {},                                          # straight ahead
    "L": {"input_head_yaw_variation": -4.0},          # turned slightly (talking + idle)
    "R": {"input_head_yaw_variation": 4.0},
    "U": {"input_head_pitch_variation": -3.0},        # chin slightly up (idle / listening)
    "D": {"input_head_pitch_variation": 3.0},         # small nod down (listening, acknowledging)
    "T": {"input_head_roll_variation": 3.0},          # gentle tilt (attentive)
}
VIEW = (80, 0, 800, 1066)   # x, y, w, h of the part the window shows; frames are stored cropped to this
OUT_SIZE = (600, 800)       # stored size (keeps the browser's memory use reasonable)


def plan_frames():
    """Which (pose, expression, mouth, eye) combinations to render. Talking poses get every mouth shape;
    listening poses only need a closed mouth. Blinks everywhere except talking at a side angle."""
    for pose in POSES:
        for ex in EXPRESSIONS:
            for mi, mouth in enumerate(VISEMES):
                for ei in range(3):
                    talking_pose, rest = pose in ("C", "L", "R"), mouth == "rest"
                    if pose == "C" or (talking_pose and (ei == 0 or rest)) or (not talking_pose and rest):
                        yield pose, ex, mi, mouth, ei


def atlas_v5(r: Renderer, out: Path, eyes: list[float]):
    frames = out / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    todo = list(plan_frames())
    meta = {"version": 5, "source": r.src.name, "visemes": list(VISEMES), "viseme_settings": VISEMES,
            "poses": {k: v for k, v in POSES.items()}, "eye_levels": eyes, "expressions": list(EXPRESSIONS),
            "cropped": True, "frames": {}}
    t0 = time.perf_counter()
    x, y, w, h = VIEW
    for n, (pose, ename, mi, mouth, ei) in enumerate(todo, 1):
        name = f"{pose}-{ename}-m{mi}-e{ei}.jpg"
        img = r.frame(**combine(EXPRESSIONS[ename], VISEMES[mouth], POSES[pose], {"eye": eyes[ei]}))
        img = cv2.resize(img[y:y + h, x:x + w], OUT_SIZE, interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(frames / name), img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        meta["frames"][f"{pose}/{ename}/{mouth}/{ei}"] = f"frames/{name}"
        if n % 50 == 0 or n == len(todo):
            print(f"  {n}/{len(todo)} frames ({time.perf_counter() - t0:.0f}s)", flush=True)
    meta |= {"width": OUT_SIZE[0], "height": OUT_SIZE[1], "render_seconds": round(time.perf_counter() - t0, 1)}
    (out / "atlas.json").write_text(json.dumps(meta, indent=2))


def atlas(r: Renderer, out: Path, eyes: list[float]):
    frames = out / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    meta = {"version": 2, "source": r.src.name, "visemes": list(VISEMES), "viseme_settings": VISEMES,
            "eye_levels": eyes, "expressions": list(EXPRESSIONS), "frames": {}}
    t0 = time.perf_counter()
    for ename, ex in EXPRESSIONS.items():
        for li, (vname, vis) in enumerate(VISEMES.items()):
            for ei, eye in enumerate(eyes):
                name = f"{ename}-m{li}-e{ei}.jpg"
                img = r.frame(**combine(ex, vis, {"eye": eye}))
                cv2.imwrite(str(frames / name), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
                meta["frames"][f"{ename}/{vname}/{ei}"] = f"frames/{name}"
        print(f"  {ename} done ({time.perf_counter() - t0:.0f}s)", flush=True)
    h, w = img.shape[:2]
    meta |= {"width": w, "height": h, "render_seconds": round(time.perf_counter() - t0, 1)}
    (out / "atlas.json").write_text(json.dumps(meta, indent=2))


if __name__ == "__main__":
    mode, photo, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    r = Renderer(photo, out)
    if mode == "calibrate":
        calibrate(r, out)
    elif mode == "atlas5":
        atlas_v5(r, out, [r.eye0, 0.15, 0.0])
    elif mode == "sheet":
        sheet(r, out, Path(sys.argv[4]), sys.argv[5])
    else:
        eyes = [float(x) for x in (sys.argv[4] if len(sys.argv) > 4 else "-1,0.15,0").split(",")]
        atlas(r, out, [r.eye0 if x < 0 else x for x in eyes])
