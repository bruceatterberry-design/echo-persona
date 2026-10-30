"""Versions of Echo's personality, so changes can be compared and undone (Phase 3).

A version is a copy of everything that shapes how Echo talks: the approved profile (profile.json), the review
decisions (draft.json) and the base prompt (config/personality.example.yaml). Stored in data/personality/versions/
<NNN>-<time>/ with version.json (note, time, sha256 of each file). Nothing is ever deleted; restoring first saves the
current state as a new version, so a restore can itself be undone.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import shutil
import time
from pathlib import Path


class Versions:
    def __init__(self, pdir: Path, prompt_file: Path):
        self.pdir, self.prompt = Path(pdir), Path(prompt_file)
        self.dir = self.pdir / "versions"

    def files(self) -> dict[str, Path]:
        return {"profile.json": self.pdir / "profile.json", "draft.json": self.pdir / "draft.json",
                "personality.yaml": self.prompt}

    @staticmethod
    def _hash(p: Path) -> str | None:
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None

    def current_hashes(self) -> dict[str, str | None]:
        return {k: self._hash(p) for k, p in self.files().items()}

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.dir.glob("*/version.json")):
            out.append(json.loads(d.read_text()))
        return out

    def latest(self) -> dict | None:
        v = self.list()
        return v[-1] if v else None

    def current_id(self) -> str | None:
        """The version identical to what's in use now, if any."""
        now = self.current_hashes()
        return next((v["id"] for v in reversed(self.list()) if v["hashes"] == now), None)

    def snapshot(self, note: str = "") -> dict:
        """Save what's in use now (unless it's identical to the latest version)."""
        last = self.latest()
        if last and last["hashes"] == self.current_hashes():
            return last
        n = int(last["id"].split("-")[0]) + 1 if last else 1
        vid = f"{n:03d}-{time.strftime('%Y%m%d-%H%M%S')}"
        d = self.dir / vid
        d.mkdir(parents=True)
        for k, p in self.files().items():
            if p.exists():
                shutil.copy2(p, d / k)
        prof = self._load(d / "profile.json")
        meta = {"id": vid, "at": time.time(), "note": note[:200], "hashes": self.current_hashes(),
                "traits": len(prof.get("traits", [])), "summary_lines": len(prof.get("condensed", []))}
        (d / "version.json").write_text(json.dumps(meta, indent=2))
        return meta

    def snapshot_if_stale(self, min_age_s: float = 1800, note: str = "automatic, before changes") -> dict | None:
        """Before an edit: keep a copy of the state as it was, at most once per editing session."""
        last = self.latest()
        if last and (last["hashes"] == self.current_hashes() or time.time() - last["at"] < min_age_s):
            return None
        return self.snapshot(note)

    @staticmethod
    def _load(p: Path) -> dict:
        return json.loads(p.read_text()) if p.exists() else {}

    def _state(self, vid: str | None) -> dict[str, Path]:
        if vid in (None, "current"):
            return self.files()
        d = self.dir / vid
        if not (d / "version.json").exists() or "/" in vid or ".." in vid:
            raise KeyError(vid)
        return {k: d / k for k in self.files()}

    def diff(self, a: str, b: str = "current") -> dict:
        A, B = self._state(a), self._state(b)
        pa, pb = self._load(A["profile.json"]), self._load(B["profile.json"])
        ta = {t["trait"] for t in pa.get("traits", [])}; tb = {t["trait"] for t in pb.get("traits", [])}
        ca, cb = pa.get("condensed", []), pb.get("condensed", [])
        ya = A["personality.yaml"].read_text().splitlines() if A["personality.yaml"].exists() else []
        yb = B["personality.yaml"].read_text().splitlines() if B["personality.yaml"].exists() else []
        return {"a": a, "b": b, "traits_added": sorted(tb - ta), "traits_removed": sorted(ta - tb),
                "summary_added": [x for x in cb if x not in ca], "summary_removed": [x for x in ca if x not in cb],
                "prompt": [l for l in difflib.unified_diff(ya, yb, "before", "after", lineterm="", n=0)][2:]}

    def restore(self, vid: str) -> dict:
        src = self._state(vid)
        self.snapshot(f"automatic, before restoring {vid}")
        for k, p in self.files().items():
            if src[k].exists():
                shutil.copy2(src[k], p)
        return {"restored": vid}
