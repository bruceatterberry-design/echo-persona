"""Persistent cognitive state (CA 2.0 Phase 2): what Echo was in the middle of survives a restart.

Before this, a core restart (after every update, or by the watchdog) forgot: the offer Echo just made ("want me to open
the weather?"), the "which one?" question he just asked, what he opened last ("close that"), and who he was talking
with, about what. Now it is saved after every turn to data/state.json (previous copy kept as state.prev.json: the
"last good state") and restored at startup, each item only while it is still fresh.

Kept small and inspectable (GET /api/state). Ids and short labels only — never message text or memory contents, and
nothing here is a memory: the memory database stays the only source of what Echo knows. Echo's prompt is unchanged.
Schema: docs/cognitive/ca2/COGNITIVE_STATE_SCHEMA.md.
"""
from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from pathlib import Path

VERSION = 1
# how long each item stays valid (seconds)
FRESH = {"offer": 180, "which_one": 180, "opened": 3600, "last_turn": 7 * 86400}
RECENT_MEMORIES = 12


class CognitiveState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.prev = self.path.with_name(self.path.stem + ".prev.json")
        self.lock = threading.Lock()
        self.data, self.restored_from = self._load()

    def _load(self) -> tuple[dict, str | None]:
        for p in (self.path, self.prev):   # a damaged file falls back to the last good one
            try:
                d = json.loads(p.read_text())
                if isinstance(d, dict) and d.get("version") == VERSION:
                    return d, p.name
            except (OSError, json.JSONDecodeError):
                continue
        return {"version": VERSION}, None

    def _save(self) -> None:
        self.data["saved_at"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        if self.path.exists():
            self.path.replace(self.prev)
        tmp.replace(self.path)

    def set(self, key: str, value) -> None:
        """Set one section (None removes it) and save."""
        with self.lock:
            if value is None:
                self.data.pop(key, None)
            else:
                self.data[key] = value
            self._save()

    def get(self, key: str, now: float | None = None):
        """A section, only while it is still fresh (FRESH seconds after its 'at')."""
        v = self.data.get(key)
        if isinstance(v, dict) and key in FRESH and (now or time.time()) - v.get("at", 0) > FRESH[key]:
            return None
        return v

    # ---- what a turn leaves behind ----
    def turn(self, speaker: str | None, route: str, goal: str | None, memory_ids: list[int],
             entities: list[str] | None = None, now: float | None = None) -> None:
        now = now or time.time()
        with self.lock:
            recent = [i for i in memory_ids if isinstance(i, int)]
            for i in self.data.get("focus", {}).get("memory_ids", []):
                if i not in recent:
                    recent.append(i)
            self.data["last_turn"] = {"at": now, "speaker": speaker or "typed", "route": route, "goal": goal}
            focus = {"at": now, "memory_ids": recent[:RECENT_MEMORIES]}
            if entities:
                focus["entities"] = entities[:12]
            elif "entities" in self.data.get("focus", {}):
                focus["entities"] = self.data["focus"]["entities"]
            self.data["focus"] = focus
            self._save()

    def offer(self, act, now: float | None = None) -> None:
        """Echo's open offer (a browser Action). Offers that would run a script are never written to disk."""
        if act is None or getattr(act, "script", None):
            self.set("offer", None)
            return
        self.set("offer", {"at": now or time.time(), "act": dataclasses.asdict(act)})

    def restored_offer(self, action_cls):
        o = self.get("offer")
        if not o:
            return None
        fields = {f.name for f in dataclasses.fields(action_cls)}
        d = {k: v for k, v in o["act"].items() if k in fields and k != "script"}
        return action_cls(**d), o["at"]

    def snapshot(self) -> dict:
        now = time.time()
        out = {"restored_from": self.restored_from, "saved_at": self.data.get("saved_at")}
        for k in ("last_turn", "focus", "offer", "which_one", "opened"):
            v = self.data.get(k)
            if v is not None:
                out[k] = {**v, "fresh": self.get(k, now) is not None} if isinstance(v, dict) else v
        return out
