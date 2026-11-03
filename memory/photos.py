"""Photo collections Echo learns from: cats, places we've been, nature, our home, plus categories the owner adds.

Flow: photos arrive (drop folder or Google Photos picker) → Echo describes each one with his own model (local, vision) →
the owner adds a caption ("Milo on the porch, 2019") and presses Approve → only then is it saved as a memory Echo uses.
Rejected photos are never learned from. Approving keeps the original read-only.

Layout under dataset_root (the data drive):
  staging/photos/<category>/...          dropped-in or picked files; never modified
  derived/photos/view-v1/<sha16>.jpg     upright 1024 px copy (shown on the page, given to the model)
  derived/photos/index-v1.json           categories + photos + review status
  raw/photos/<category>/<sha16>.<ext>    ORIGINAL, copied read-only when approved
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx
from PIL import Image, ImageOps

from face.library import IMAGE_EXT, sha256_file

DEFAULT_CATEGORIES = {"cats": "Cats", "places": "Places we've been", "nature": "Nature", "home": "Our home"}
STATUSES = {"pending", "approved", "rejected"}
VIEW_RE = re.compile(r"^[0-9a-f]{16}\.jpg$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,29}$")
PROMPT = ("This photo is from the owner's \"{label}\" photo collection. Describe it in 2-3 plain sentences: what and who is in "
          "it, the setting, and anything notable (season, weather, colours). Never guess names of people, pets or places. "
          "No preamble.")


class PhotoError(Exception):
    pass


MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def camera_date(path: Path) -> str | None:
    """When the photo was TAKEN, from the camera's own data (EXIF DateTimeOriginal, else DateTime): "2022-01-15 10:17".
    Only the camera's record is trusted — never the file's date (that is when it was downloaded or copied)."""
    def read(f):
        with Image.open(f) as im:
            ex = im.getexif()
            return ex.get_ifd(0x8769).get(36867) or ex.get(306)
    raw = None
    try:
        raw = read(path)
    except Exception:   # HEIC and other formats Pillow can't open: convert a copy (sips keeps the camera data)
        tmp = Path("/tmp") / f".echo-exif-{path.stem[:20]}.jpg"
        try:
            subprocess.run(["sips", "-s", "format", "jpeg", str(path), "--out", str(tmp)], capture_output=True,
                           timeout=60, check=True)
            raw = read(tmp)
        except Exception:
            raw = None
        finally:
            tmp.unlink(missing_ok=True)
    m = re.match(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2})", str(raw or "").strip())
    if not m or not (1990 <= int(m[1]) <= 2100) or not (1 <= int(m[2]) <= 12):
        return None
    return f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}"


def nice_date(d: str | None) -> str | None:
    """'2022-01-15 10:17' → '15 January 2022'."""
    if not d:
        return None
    y, mo, da = d[:10].split("-")
    return f"{int(da)} {MONTHS[int(mo) - 1]} {y}"


def short_label(description: str | None) -> str:
    """'This photo shows a misty mountain landscape with snow…' → 'a misty mountain landscape with snow-dusted slopes'."""
    t = re.sub(r"^(?:this (?:nighttime |vintage |close-up )?(?:photo|picture|image|selfie|illustration)\s+"
               r"(?:shows|features|captures|is)\s+)", "", (description or "").strip(), flags=re.I)
    t = re.split(r"[.,;—:]", t)[0]
    words = t.split()
    return " ".join(words[:9]) + ("…" if len(words) > 9 else "") if words else "a photo"


def slugify(label: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", label.lower())).strip("-")[:30]


def upright_copy(src: Path, dst: Path, size: int = 1024) -> tuple[int, int]:
    """Upright (camera rotation applied) JPEG, longest side `size`. sips first so HEIC works."""
    tmp = dst.with_name("." + dst.name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["sips", "-s", "format", "jpeg", str(src), "--out", str(tmp)], capture_output=True, timeout=120, check=True)
    with Image.open(tmp) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        w, h = im.size
        im.thumbnail((size, size))
        im.save(dst, "JPEG", quality=88)
    tmp.unlink(missing_ok=True)
    return w, h


class OllamaDescriber:
    """Echo's own model looks at the photo (same model + settings as conversation, so nothing reloads)."""
    def __init__(self, host: str, model: str, options: dict, keep_alive: str = "24h"):
        self.host, self.model, self.options, self.keep_alive = host.rstrip("/"), model, options, keep_alive

    def __call__(self, image: Path, label: str) -> str:
        from core.llm import _post   # cancellable when run as a background job (core/sched.py)
        r = _post(f"{self.host}/api/chat", timeout=300, body={
            "model": self.model, "stream": False, "think": False, "keep_alive": self.keep_alive,
            "options": {**self.options, "temperature": 0.2},
            "messages": [{"role": "user", "content": PROMPT.format(label=label),
                          "images": [base64.b64encode(image.read_bytes()).decode()]}]})
        r.raise_for_status()
        return r.json()["message"]["content"].strip()[:1000]


class PhotoLibrary:
    def __init__(self, dataset_root: str | Path, store=None, describer=None):
        self.root = Path(dataset_root)
        self.staging = self.root / "staging" / "photos"
        self.view = self.root / "derived" / "photos" / "view-v1"
        self.index_path = self.root / "derived" / "photos" / "index-v1.json"
        self.raw = self.root / "raw" / "photos"
        self.store, self.describer = store, describer
        self.lock = threading.RLock()
        self.describing: dict = {"running": False, "done": 0, "total": 0, "error": None}

    # ---- index ----
    def load(self) -> dict:
        if self.index_path.exists():
            return json.loads(self.index_path.read_text())
        return {"version": 1, "categories": dict(DEFAULT_CATEGORIES), "photos": {}}

    def save(self, idx: dict) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx, indent=1))
        tmp.replace(self.index_path)

    def _check_drive(self) -> None:
        if not self.root.is_dir():
            raise PhotoError("Photo folder not found. Is the data drive plugged in?")

    # ---- categories ----
    def add_category(self, label: str) -> dict:
        label = label.strip()
        slug = slugify(label)
        if not label or len(label) > 40 or not SLUG_RE.match(slug):
            raise PhotoError("use a short name, letters and numbers (e.g. Family, Holidays)")
        self._check_drive()
        with self.lock:
            idx = self.load()
            if slug in idx["categories"]:
                raise PhotoError("that category already exists")
            idx["categories"][slug] = label
            self.save(idx)
            (self.staging / slug).mkdir(parents=True, exist_ok=True)
            return {"id": slug, "label": label}

    def folder(self, category: str) -> Path:
        if category not in self.load()["categories"]:
            raise PhotoError("unknown category")
        return self.staging / category

    # ---- scanning ----
    def scan(self) -> dict:
        """Find new photos in staging/photos/<category>/ (any depth). Safe to run repeatedly."""
        self._check_drive()
        with self.lock:
            idx = self.load()
            for c in idx["categories"]:
                (self.staging / c).mkdir(parents=True, exist_ok=True)
            new = dupes = errors = 0
            for p in sorted(self.staging.rglob("*")):
                rel = p.relative_to(self.staging).parts
                if not p.is_file() or p.suffix.lower() not in IMAGE_EXT or p.name.startswith(".") or rel[0] not in idx["categories"]:
                    continue
                sha = sha256_file(p)
                if sha in idx["photos"]:
                    dupes += 1
                    continue
                try:
                    w, h = upright_copy(p, self.view / f"{sha[:16]}.jpg")
                except Exception:
                    errors += 1
                    continue
                idx["photos"][sha] = {"sha256": sha, "category": rel[0], "name": p.name, "source": str(p.relative_to(self.root)),
                                      "width": w, "height": h, "added": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                      "taken": camera_date(p), "taken_checked": True,
                                      "status": "pending", "description": None, "caption": "", "memory_id": None}
                new += 1
            self.save(idx)
            return {"new_photos": new, "duplicates": dupes, "unreadable": errors}

    # ---- Echo describes ----
    def describe(self, sha: str) -> str:
        if not self.describer:
            raise PhotoError("Echo's model isn't available")
        idx = self.load()
        p = idx["photos"].get(sha)
        if not p or p["status"] not in STATUSES:
            raise PhotoError("unknown photo")
        text = self.describer(self.view / f"{sha[:16]}.jpg", idx["categories"].get(p["category"], p["category"]))
        with self.lock:
            idx = self.load()
            idx["photos"][sha]["description"] = text
            self.save(idx)
        return text

    def describe_waiting(self) -> dict:  # only "pending" photos: removed (the loved one) ones are never looked at
        """Describe every photo still waiting, one at a time, in the background."""
        with self.lock:
            if self.describing["running"]:
                return self.describing
            todo = [s for s, p in self.load()["photos"].items() if p["status"] == "pending" and not p["description"]]
            self.describing = {"running": bool(todo), "done": 0, "total": len(todo), "error": None}

        def run():
            from core.sched import SCHED, Preempted
            for s in todo:
                while True:   # conversation first (core/sched.py): wait while anyone talks, then the same photo again
                    try:
                        with SCHED.job("photos"):
                            self.describe(s)
                        self.describing["done"] += 1
                        self.describing.pop("waiting", None)
                        break
                    except Preempted as w:
                        self.describing["waiting"] = str(w)
                        time.sleep(3)
                    except Exception as e:
                        self.describing["error"] = f"{type(e).__name__}: {e}"[:200]
                        break
                if self.describing["error"]:
                    break
            self.describing["running"] = False

        if todo:
            threading.Thread(target=run, daemon=True).start()
        return self.describing

    # ---- the owner reviews ----
    def summary(self) -> dict:
        idx = self.load()
        photos = sorted((p for p in idx["photos"].values() if p["status"] in STATUSES),
                        key=lambda p: (p["category"], p["added"], p["name"]))
        counts = {c: {s: sum(1 for p in photos if p["category"] == c and p["status"] == s) for s in STATUSES}
                  for c in idx["categories"]}
        return {"categories": [{"id": k, "label": v} for k, v in idx["categories"].items()], "photos": photos,
                "counts": counts, "describing": self.describing}

    def review(self, sha: str, status: str | None = None, caption: str | None = None, category: str | None = None) -> dict:
        with self.lock:
            idx = self.load()
            p = idx["photos"].get(sha)
            if not p or p["status"] not in STATUSES:
                raise PhotoError("unknown photo")
            if caption is not None:
                p["caption"] = caption.strip()[:500]
            if category is not None:
                if category not in idx["categories"]:
                    raise PhotoError("unknown category")
                p["category"] = category
            if status is not None:
                if status not in STATUSES:
                    raise PhotoError("bad status")
                if status == "approved" and not (p["caption"] or p["description"]):
                    raise PhotoError("add a caption (or let Echo describe it) before approving")
                p["status"] = status
            if p["status"] == "approved":
                self._preserve_original(p)
                self._learn(p, idx["categories"][p["category"]], idx)
            elif p["memory_id"] and self.store:   # un-approved: Echo stops using it (kept hidden, can be restored)
                self.store.forget(p["memory_id"], by="owner:photos", reason="photo no longer approved")
                p["memory_id"] = None
            self.save(idx)
            return p

    def remove_loved_one(self, sha: str) -> dict:
        """the loved one declined (2026-11-02): her photos are never used. Removes every copy Echo made (view copy, kept original,
        Google/staging copy), the description and any memory. Only the checksum stays, so the same photo is never re-imported."""
        with self.lock:
            idx = self.load()
            p = idx["photos"].get(sha)
            if not p:
                raise PhotoError("unknown photo")
            if p["memory_id"] and self.store:
                self.store.forget(p["memory_id"], by="owner:photos", reason="the loved one is in this photo (she declined)")
            for rel in (p.get("preserved"), p.get("source")):
                if rel and (f := self.root / rel).exists():
                    f.chmod(0o644)
                    f.unlink()
            (self.view / f"{sha[:16]}.jpg").unlink(missing_ok=True)
            idx["photos"][sha] = {"sha256": sha, "category": p["category"], "name": "removed", "source": None,
                                  "added": p["added"], "status": "removed_loved_one", "description": None, "caption": "",
                                  "memory_id": None}
            self.save(idx)
            return idx["photos"][sha]

    def memory_text(self, p: dict, label: str, same_day: list[str] | None = None) -> str:
        """Taken (camera) and shared (with Echo) are two different dates and are always named apart (CA 2.0 Phase 4:
        Echo said a bridge photo from June 2022 was "taken on November 2", the day it was shared)."""
        parts = [f"A photo in my \"{label}\" collection."]
        parts.append(f"Taken on {nice_date(p['taken'])} (date from the camera)." if p.get("taken")
                     else "Date taken: not known (the photo has no camera date).")
        if p.get("added"):
            parts.append(f"Shared with me on {nice_date(p['added'][:10] + ' 00:00')}.")
        if p["caption"]:
            parts.append(f"The owner says: {p['caption'].rstrip('.')}.")
        if p["description"]:
            parts.append(f"What the photo shows: {p['description']}")
        if same_day:
            parts.append("Taken the same day: " + "; ".join(same_day[:5]) + ".")
        return " ".join(parts)[:2000]

    def _same_day(self, idx: dict, p: dict) -> list[str]:
        day = (p.get("taken") or "")[:10]
        if not day:
            return []
        return [short_label(q["description"]) + f" ({q['taken'][11:16]})"
                for q in sorted(idx["photos"].values(), key=lambda q: q.get("taken") or "")
                if q["sha256"] != p["sha256"] and q["status"] == "approved" and (q.get("taken") or "")[:10] == day]

    def _fields(self, p: dict) -> dict:
        return {"year": int(p["taken"][:4]), "when_text": nice_date(p["taken"])} if p.get("taken") else {}

    def _learn(self, p: dict, label: str, idx: dict | None = None) -> None:
        if not self.store:
            return
        from memory.store import Memory
        idx = idx or self.load()
        text = self.memory_text(p, label, self._same_day(idx, p))
        if p["memory_id"]:
            old = self.store.get(p["memory_id"])
            if old and old.content == text and not old.hidden:
                return
            self.store.forget(p["memory_id"], by="owner:photos", reason="photo caption changed")
        m, _ = self.store.add(Memory(kind="memory", content=text, source=f"photo:{p['sha256'][:16]}", told_by="owner",
                                     **self._fields(p)))
        p["memory_id"] = m.id

    # ---- consolidation (CA 2.0 Phase 4): dates, same-day links, a collection overview — no language model ----
    def consolidate(self) -> dict:
        """Bring every approved photo memory up to date: camera date (when taken) kept apart from when it was shared,
        links to photos taken the same day, and one overview memory of the collection. Changes go through the memory
        store's versioned update (old wording kept in history, restorable); nothing is deleted."""
        if not self.store:
            return {"updated": 0}
        self._check_drive()
        with self.lock:
            idx = self.load()
            dated = 0
            for p in idx["photos"].values():
                if not p.get("taken_checked") and p["status"] in STATUSES:
                    src = self.root / (p.get("preserved") or p.get("source") or "")
                    p["taken"] = camera_date(src) if src.is_file() else None
                    p["taken_checked"] = True
                    dated += bool(p["taken"])
            updated = 0
            for p in idx["photos"].values():
                if p["status"] != "approved" or not p.get("memory_id"):
                    continue
                cur = self.store.get(p["memory_id"])
                if not cur:
                    continue
                text = self.memory_text(p, idx["categories"].get(p["category"], p["category"]), self._same_day(idx, p))
                f = self._fields(p)
                if cur.content != text or cur.year != f.get("year"):
                    self.store.update(p["memory_id"], _action="consolidate", _by="consolidation:photos", content=text, **f)
                    updated += 1
            overview = self._overview(idx)
            self.save(idx)
            return {"dates_read": dated, "updated": updated, "overview": overview}

    def overview_text(self, idx: dict) -> str | None:
        ok = [p for p in idx["photos"].values() if p["status"] == "approved"]
        if not ok:
            return None
        cats = idx["categories"]
        by_cat = {}
        for p in ok:
            by_cat[cats.get(p["category"], p["category"])] = by_cat.get(cats.get(p["category"], p["category"]), 0) + 1
        dated = sorted((p for p in ok if p.get("taken")), key=lambda p: p["taken"])
        parts = [f"My photo collection (photos the owner shared with me and approved): {len(ok)} photos — "
                 + ", ".join(f"{k} {v}" for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])) + "."]
        if dated:
            years = {}
            for p in dated:
                years[p["taken"][:4]] = years.get(p["taken"][:4], 0) + 1
            top = max(years.items(), key=lambda kv: kv[1])
            o, n = dated[0], dated[-1]
            parts.append(f"Camera dates run from {nice_date(o['taken'])} to {nice_date(n['taken'])}"
                         + (f" ({len(ok) - len(dated)} photos have no camera date)." if len(dated) < len(ok) else "."))
            parts.append(f"The oldest photo, taken {nice_date(o['taken'])}, shows {short_label(o['description'])}"
                         f" (\"{cats.get(o['category'], o['category'])}\").")
            parts.append(f"The newest, taken {nice_date(n['taken'])}, shows {short_label(n['description'])}.")
            parts.append(f"Most were taken in {top[0]} ({top[1]}). Photos per year: "
                         + ", ".join(f"{y} {c}" for y, c in sorted(years.items())) + ".")
        return " ".join(parts)[:2000]

    def _overview(self, idx: dict) -> str:
        from memory.store import Memory
        text = self.overview_text(idx)
        mid = idx.get("overview_memory_id")
        cur = self.store.get(mid) if mid else None
        if not text:
            if cur:
                self.store.forget(mid, by="consolidation:photos", reason="no approved photos")
            return "none"
        if cur:
            if cur.content != text:
                self.store.update(mid, _action="consolidate", _by="consolidation:photos", content=text)
                return "updated"
            return "unchanged"
        m, _ = self.store.add(Memory(kind="knowledge", status="learned", confidence=0.95, content=text,
                                     source="consolidation:photo-collection"))
        idx["overview_memory_id"] = m.id
        return "made"

    def _preserve_original(self, p: dict) -> None:
        src = self.root / p["source"]
        dst = self.raw / p["category"] / f"{p['sha256'][:16]}{Path(p['name']).suffix.lower()}"
        if p.get("preserved") or not src.exists():
            return
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            if sha256_file(dst) != p["sha256"]:
                dst.unlink()
                raise PhotoError("copy verification failed")
            dst.chmod(0o444)
        p["preserved"] = str(dst.relative_to(self.root))

    def view_path(self, name: str) -> Path:
        if not VIEW_RE.match(name):
            raise PhotoError("bad name")
        return self.view / name
