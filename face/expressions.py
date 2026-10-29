"""Expression recordings from the Collector app → the avatar's face (runs in Echo's own environment).

After "Copy from phone", any session with expression takes (joy, surprised, sad, angry, bewildered) gets a preview sheet
made automatically (face/expression_transfer.py in LivePortrait's environment). You look at it in Avatar Collector;
"Put on the avatar's face" renders a new atlas version with the choices in config/face_expressions.json (same math every
time: your movement from your relaxed photo to the expression, added to the current face photo). Nothing is applied
without that click, and older atlas versions are kept.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path

from face.library import current_atlas

ROOT = Path(__file__).resolve().parent.parent
LP_PYTHON = ROOT / "animate" / ".venv" / "bin" / "python"
SCRIPT = ROOT / "face" / "expression_transfer.py"
FEELINGS = ("joy", "surprised", "sad", "angry", "bewildered")
SESSION_RE = re.compile(r"^\d{8}-\d{6}$")
job = {"running": False, "what": None, "error": None}
_lock = threading.Lock()


def _face(root: Path) -> Path:
    return Path(root) / "derived" / "face"


def sessions(root: Path) -> list[str]:
    """Phone sessions that hold at least one expression photo and video."""
    out = []
    for s in sorted((Path(root) / "raw" / "phone").glob("*")):
        kinds = set()
        for meta in s.glob("take-*.json"):
            try:
                d = json.loads(meta.read_text())
            except (OSError, ValueError):
                continue
            if d.get("expression_label") in FEELINGS:
                kinds.add(d.get("task_id", "").split("-")[0])
        if SESSION_RE.match(s.name) and {"photo", "face"} <= kinds:
            out.append(s.name)
    return out


def previews(root: Path) -> dict[str, Path]:
    """session → folder with its expression-deltas.json and expressions-sheet.jpg."""
    out = {}
    for d in _face(root).glob("expressions-*"):
        f = d / "expression-deltas.json"
        if f.exists() and (d / "expressions-sheet.jpg").exists():
            out[Path(json.loads(f.read_text())["session"]).name] = d
    return out


def status(root: Path) -> dict:
    atlas = current_atlas(root)
    meta = json.loads((atlas / "atlas.json").read_text()) if (atlas / "atlas.json").exists() else {}
    used = Path(meta.get("carried_expressions", {}).get("from", "")).parent.name
    pv = previews(root)
    return {"job": dict(job), "atlas": atlas.name, "expressions": meta.get("expressions", []),
            "sessions": [{"session": s, "preview": s in pv, "on_face": s in pv and pv[s].name == used}
                         for s in sessions(root)]}


def _run(args: list[str]) -> None:
    env = os.environ | {"PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    r = subprocess.run([str(LP_PYTHON), str(SCRIPT), *args], cwd=ROOT, env=env, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1][:200] if (r.stderr or r.stdout) else "failed")


def _background(what: str, fn) -> bool:
    with _lock:
        if job["running"]:
            return False
        job.update(running=True, what=what, error=None)

    def go():
        try:
            fn()
        except Exception as e:
            job["error"] = f"{type(e).__name__}: {e}"[:300]
        finally:
            job.update(running=False, what=None)
    threading.Thread(target=go, daemon=True, name="face-expressions").start()
    return True


def make_missing_previews(root: Path) -> bool:
    """Called after phone processing: preview sheets for new expression sessions (about a minute each)."""
    todo = [s for s in sessions(root) if s not in previews(root)]
    if not todo:
        return False
    return _background("preview", lambda: [
        _run(["sheet", str(_face(root) / f"expressions-{s}"), str(Path(root) / "raw" / "phone" / s)]) for s in todo])


def apply(root: Path, session: str) -> bool:
    """Render a new atlas (next version number) with this session's expressions on the current face."""
    pv = previews(root)
    if session not in pv:
        raise KeyError(session)
    cur = current_atlas(root)
    nxt = _face(root) / f"atlas-v{int(cur.name[7:]) + 1}"
    return _background("apply", lambda: _run(["atlas", str(pv[session] / "expression-deltas.json"), str(nxt), str(cur)]))
