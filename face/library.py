"""Face reference library: import photos from staging, find faces, let the owner review them.

Layout under dataset_root:
  staging/face/photos-current/...       dropped-in files (The owner's downloads); never modified
  staging/face/photos-younger/<age>/... younger photos, optional age subfolders ("20s", "30s"...)
  raw/face/<sha16>.<ext> (+ .meta.json) ORIGINAL photo, copied read-only once a face in it is approved
  derived/face/crops-v1/<sha16>-faceNN.jpg  512 px face crops (Apple Vision)
  derived/face/index-v1.json            catalog: photos, faces, quality, review status
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

from core.config import ROOT

FACEFIND = ROOT / "face" / "facefind"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp", ".gif"}
STATUSES = {"pending", "owner", "not_owner", "excluded"}
CROP_RE = re.compile(r"^[0-9a-f]{16}-face\d{2}\.jpg$")
MOTION_RE = re.compile(r"^idle-[a-z0-9]{1,30}\.mp4$")
ATLAS_FRAME_RE = re.compile(r"^(?:[A-Z]-)?[a-z]+-m\d{1,2}-e\d\.jpg$")  # optional head-pose prefix (v5)
MAX_ZIP_BYTES = 4 * 1024**3



def current_atlas(root: Path) -> Path:
    """The newest finished atlas (derived/face/atlas-vN with an atlas.json); a half-rendered one is never picked."""
    face = Path(root) / "derived" / "face"
    done = [p for p in face.glob("atlas-v*") if re.fullmatch(r"atlas-v\d+", p.name) and (p / "atlas.json").exists()]
    return max(done, key=lambda p: int(p.name[7:]), default=face / "atlas-v5")

class FaceError(Exception):
    pass


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def sharpness(path: Path) -> float:
    """Variance of a Laplacian on the central face area: low = blurry."""
    g = np.asarray(Image.open(path).convert("L").resize((256, 256)), dtype=np.float32)[48:208, 48:208]
    lap = g[1:-1, 1:-1] * -4 + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
    return round(float(lap.var()), 1)


def brightness(path: Path) -> float:
    return round(float(np.asarray(Image.open(path).convert("L")).mean()), 1)


def face_issues(f: dict) -> list[str]:
    out = []
    if f["face_px"] < 160:
        out.append("small / low resolution")
    if f["sharpness"] < 40:
        out.append("blurry")
    if f["brightness"] < 60:
        out.append("dark")
    elif f["brightness"] > 215:
        out.append("very bright")
    if abs(f["yaw"]) > 0.6:
        out.append("turned far to the side")
    return out


class FaceLibrary:
    def __init__(self, dataset_root: str | Path):
        self.root = Path(dataset_root)
        self.staging = self.root / "staging" / "face"
        self.crops = self.root / "derived" / "face" / "crops-v1"
        self.index_path = self.root / "derived" / "face" / "index-v1.json"
        self.raw = self.root / "raw" / "face"
        self.motion = self.root / "derived" / "face" / "motion-v1"   # idle loops from the owner's own videos
        self.lock = threading.Lock()

    # ---- index ----
    def load(self) -> dict:
        if self.index_path.exists():
            return json.loads(self.index_path.read_text())
        return {"version": 1, "photos": {}}

    def save(self, idx: dict) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx))
        tmp.replace(self.index_path)

    # ---- scanning ----
    def _unzip(self) -> int:
        n = 0
        for z in self.staging.rglob("*.zip"):
            dest = z.with_suffix("")
            if dest.exists():
                continue
            with zipfile.ZipFile(z) as zf:
                if sum(i.file_size for i in zf.infolist()) > MAX_ZIP_BYTES:
                    raise FaceError(f"{z.name} is too large to unpack")
                for info in zf.infolist():
                    name = Path(info.filename)
                    if info.is_dir() or name.is_absolute() or ".." in name.parts or name.suffix.lower() not in IMAGE_EXT:
                        continue  # only images, and never outside the destination (zip-slip)
                    target = dest / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                    n += 1
        return n

    def _group_age(self, p: Path) -> tuple[str, str]:
        rel = p.relative_to(self.staging).parts
        group = "younger" if rel[0] == "photos-younger" else "current"
        age = rel[1] if group == "younger" and len(rel) > 2 else ("current" if group == "current" else "unknown")
        return group, age[:30]

    def scan(self) -> dict:
        """Find new photos in staging, detect faces, add them to the index. Safe to run repeatedly."""
        if not self.staging.is_dir():
            raise FaceError("Face staging folder not found. Is the external SSD plugged in?")
        if not FACEFIND.exists():
            raise FaceError("face finder not built: run scripts/build_face_tools.sh")
        with self.lock:
            unzipped = self._unzip()
            idx = self.load()
            known = set(idx["photos"])
            files = [p for p in self.staging.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXT
                     and not p.name.startswith(".") and p.relative_to(self.staging).parts[0] in ("photos-current", "photos-younger")]
            skipped_links = sum(1 for p in self.staging.rglob("*.webloc"))
            new: dict[str, Path] = {}
            dupes = 0
            for p in files:
                sha = sha256_file(p)
                if sha in known or sha in new:
                    dupes += 1
                    continue
                new[sha] = p
            if new:
                self.crops.mkdir(parents=True, exist_ok=True)
                tmp = self.crops / f".tmp-{int(time.time())}"
                order = list(new.items())
                out = subprocess.run([str(FACEFIND), str(tmp)] + [str(p) for _, p in order],
                                     capture_output=True, text=True, timeout=600 + 5 * len(order))
                recs = {r["image"]: r for r in map(json.loads, filter(None, out.stdout.splitlines()))}
                for sha, p in order:
                    r = recs.get(str(p), {"error": "not processed"})
                    group, age = self._group_age(p)
                    photo = {"sha256": sha, "source": str(p.relative_to(self.root)), "name": p.name, "group": group,
                             "age_label": age, "width": r.get("width"), "height": r.get("height"),
                             "added": time.strftime("%Y-%m-%dT%H:%M:%S"), "error": r.get("error"), "faces": []}
                    for i, f in enumerate(r.get("faces", [])):
                        name = f"{sha[:16]}-face{i:02d}.jpg"
                        (tmp / f["crop"]).replace(self.crops / name)
                        face = {"crop": name, "box": f["box"], "face_px": f["face_px"], "yaw": round(f["yaw"], 2),
                                "roll": round(f["roll"], 2), "vision_quality": round(f["quality"], 2),
                                "feature": f["feature"], "status": "pending", "preferred": False}
                        face["sharpness"] = sharpness(self.crops / name)
                        face["brightness"] = brightness(self.crops / name)
                        face["issues"] = face_issues(face)
                        photo["faces"].append(face)
                    idx["photos"][sha] = photo
                shutil.rmtree(tmp, ignore_errors=True)
                self.save(idx)
            return {"new_photos": len(new), "duplicates": dupes, "unzipped": unzipped, "skipped_links": skipped_links,
                    "faces_found": sum(len(idx["photos"][s]["faces"]) for s in new)}

    # ---- review ----
    def summary(self) -> dict:
        """Index without the heavy feature vectors, plus 'looks like the owner' suggestions."""
        idx = self.load()
        faces = [(s, i, f) for s, p in idx["photos"].items() for i, f in enumerate(p["faces"])]
        mine = [np.array(f["feature"]) for _, _, f in faces if f["status"] == "owner" and f["feature"]]
        others = [np.array(f["feature"]) for _, _, f in faces if f["status"] == "not_owner" and f["feature"]]

        def sim(vecs, v):
            if not vecs or not len(v):
                return None
            m = np.stack(vecs)
            return float(np.max(m @ v / (np.linalg.norm(m, axis=1) * np.linalg.norm(v) + 1e-9)))

        photos = []
        for s, p in idx["photos"].items():
            pf = []
            for f in p["faces"]:
                v = np.array(f["feature"]) if f["feature"] else np.array([])
                a, b = sim(mine, v), sim(others, v)
                hint = None
                if a is not None:  # rough hint only; the owner decides
                    hint = round(a if b is None else a - b, 3)
                pf.append({k: v2 for k, v2 in f.items() if k != "feature"} | {"hint": hint})
            photos.append({k: v2 for k, v2 in p.items() if k != "faces"} | {"faces": pf})
        photos.sort(key=lambda p: (p["group"], p["added"], p["name"]))
        approved = [f for p in photos for f in p["faces"] if f["status"] == "owner"]
        return {"photos": photos, "counts": {
            "photos": len(photos), "faces": len(faces), "pending": sum(f["status"] == "pending" for _, _, f in faces),
            "approved_current": sum(1 for p in photos for f in p["faces"] if f["status"] == "owner" and p["group"] == "current"),
            "approved_younger": sum(1 for p in photos for f in p["faces"] if f["status"] == "owner" and p["group"] == "younger"),
            "preferred": sum(f["preferred"] for f in approved)},
            "links_waiting": sum(1 for _ in self.staging.rglob("*.webloc")) if self.staging.is_dir() else 0}

    def review(self, sha: str, face_i: int, status: str | None = None, preferred: bool | None = None,
               age_label: str | None = None) -> dict:
        with self.lock:
            idx = self.load()
            p = idx["photos"].get(sha)
            if not p or not 0 <= face_i < len(p["faces"]):
                raise FaceError("unknown face")
            f = p["faces"][face_i]
            if status is not None:
                if status not in STATUSES:
                    raise FaceError("bad status")
                f["status"] = status
                if status != "owner":
                    f["preferred"] = False
            if preferred is not None:
                f["preferred"] = bool(preferred) and f["status"] == "owner"
            if age_label is not None:
                if not re.fullmatch(r"[\w\- ]{1,30}", age_label):
                    raise FaceError("bad age label")
                p["age_label"] = age_label
            if f["status"] == "owner":
                self._preserve_original(p)
            self.save(idx)
            return {k: v for k, v in f.items() if k != "feature"}

    def _preserve_original(self, p: dict) -> None:
        """Copy an approved photo's original into raw/face, read-only, verified by checksum."""
        src = self.root / p["source"]
        dst = self.raw / f"{p['sha256'][:16]}{Path(p['name']).suffix.lower()}"
        if dst.exists() or not src.exists():
            return
        self.raw.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        if sha256_file(dst) != p["sha256"]:
            dst.unlink()
            raise FaceError("copy verification failed")
        dst.chmod(0o444)
        meta = dst.with_suffix(dst.suffix + ".meta.json")
        meta.write_text(json.dumps({"sha256": p["sha256"], "original_name": p["name"], "group": p["group"],
                                    "age_label": p["age_label"], "width": p["width"], "height": p["height"],
                                    "preserved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                    "consent": "approved by the owner in the face review page"}, indent=2))
        meta.chmod(0o444)
        p["preserved"] = str(dst.relative_to(self.root))

    def crop_path(self, name: str) -> Path:
        if not CROP_RE.match(name):
            raise FaceError("bad crop name")
        return self.crops / name

    @property
    def atlas(self) -> Path:   # pre-rendered by face/render_face.py (+ face/expression_transfer.py); newest finished one
        return current_atlas(self.root)

    def atlas_meta(self) -> dict | None:
        p = self.atlas / "atlas.json"
        return json.loads(p.read_text()) if p.exists() else None

    def atlas_frame(self, name: str) -> Path:
        if not ATLAS_FRAME_RE.match(name):
            raise FaceError("bad frame name")
        return self.atlas / "frames" / name

    def motion_meta(self) -> dict | None:
        p = self.motion / "motion.json"
        return json.loads(p.read_text()) if p.exists() else None

    def motion_file(self, name: str) -> Path:
        if not MOTION_RE.match(name):
            raise FaceError("bad clip name")
        return self.motion / name
