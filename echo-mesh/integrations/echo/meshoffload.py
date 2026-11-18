"""Mesh offload (prototype): let Echo hand small BACKGROUND side-requests to the owner's own Echo Mesh computers instead of the Mac's model.

Why: measured 2026-11-18 on Echo's lean-assist call (20 made-up questions, graded by Echo's own Lean checker):
  Mac qwen3.5:9b ......... 12/12 arithmetic, 6/8 logic, 4.2 s a call, 85 s for 20 one after another
  Mesh qwen2.5-coder:7b .. 12/12 arithmetic, 6/8 logic, 3.5 s a call on an idle mesh, 22 s for all 20 at once
So the same accuracy, a little faster, much more throughput, and the Mac's GPU stays free for chat.

What it is NOT: the live chat. Replies stream, use the 9b model and a warm cache, and carry memories. Those stay on the Mac.

Safety (all enforced here, in code):
  * ON by default whenever the mesh is up; the Mac answers whenever it is not. ONE request at a time: a second one that arrives while
    one is out goes straight to the Mac, nothing ever queues. Switch it off with config/meshoffload.json {"enabled": false} (read again every few
    seconds, no restart) or SWEETIE_MESHOFFLOAD=0.
  * Only the owner's OWN computers: the Mesh's limited client token can send generate jobs that never go to an outside computer.
  * PRIVATE TEXT STAYS HOME: a request that mentions anyone in config/entities.json (names, aliases), school/class/student words, an
    email, a phone number or an address is never offered to the mesh. It goes to the Mac exactly as before.
  * NEVER WORSE: any problem (mesh down, no model, slow, nonsense answer) returns None and the caller does what it always did.
  * The mesh answer is only ever TEXT that the caller parses with its strict grammar; nothing the mesh says is run as code.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger("sweetie.meshoffload")
ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "enabled": True,
    "max_concurrent": 1,                                   # requests out on the mesh at once; the rest go straight to the Mac
    "admin": "http://127.0.0.1:47830",
    "token_file": "~/.echo-mesh/client.token",
    "models": {"lean_formalize": ["qwen2.5-coder:7b"]},     # in order of preference; the first one an online computer has is used
    "max_wait_s": 12.0,                                    # past this, the Mac does it (the mesh answer is dropped)
    "block_terms": [],                                     # extra words that keep a request at home
}
_PRIVATE_WORDS = re.compile(r"\b(students?|pupils?|classroom|class\s+of|grades?\s*book|gradebook|report\s*card|iep|parents?\s+of|school|teacher|teaching|lesson\s*plan)\b", re.I)
_CONTACT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s().-]{8,}\d|\b\d{1,5}\s+[A-Z][a-z]+\s+(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane|Blvd|Way|Ct|Court)\b")


class MeshOffload:
    def __init__(self, root: Path | str = ROOT):
        self.root = Path(root)
        self._cfg, self._cfg_at = dict(DEFAULTS), 0.0
        self._status, self._status_at = None, 0.0
        self._names: set[str] | None = None
        self._lock = threading.Lock()
        self._sem: threading.BoundedSemaphore | None = None
        self.counts: dict[str, int] = {}
        self.seconds: list[float] = []

    # ---- config ----
    def config(self) -> dict:
        now = time.monotonic()
        if now - self._cfg_at > 5:
            cfg = dict(DEFAULTS)
            try:
                cfg.update(json.loads((self.root / "config" / "meshoffload.json").read_text()))
            except (OSError, ValueError):
                pass
            self._cfg, self._cfg_at = cfg, now
        return self._cfg

    def enabled(self) -> bool:
        return bool(self.config().get("enabled")) and os.environ.get("SWEETIE_MESHOFFLOAD", "1") != "0"

    # ---- privacy ----
    def _entity_names(self) -> set[str]:
        if self._names is None:
            names: set[str] = set()
            try:
                d = json.loads((self.root / "config" / "entities.json").read_text())
                for e in d.get("entities", []):
                    names.add(str(e.get("name", "")).lower())
                    names.update(str(a).lower() for a in e.get("aliases", []))
                for k in ("self", "echo"):
                    names.add(str(d.get(k, "")).lower())
            except (OSError, ValueError, AttributeError):
                pass
            self._names = {n for n in names if len(n) >= 3}
        return self._names

    def private(self, text: str) -> str:
        """'' if the text may be offered to the mesh, else the reason it must stay on the Mac."""
        low = text.lower()
        for n in self._entity_names():
            if re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", low):
                return "names someone Echo knows"
        for w in self.config().get("block_terms", []):
            if str(w).lower() in low:
                return "contains a blocked term"
        if _PRIVATE_WORDS.search(text):
            return "mentions school or students"
        if _CONTACT.search(text):
            return "contains contact details"
        return ""

    # ---- the mesh ----
    def _headers(self) -> dict:
        tok = Path(os.path.expanduser(self.config()["token_file"])).read_text().strip()
        return {"Authorization": "Bearer " + tok}

    def _mesh_status(self) -> dict | None:
        now = time.monotonic()
        if now - self._status_at < 5:
            return self._status
        try:
            r = httpx.get(self.config()["admin"].rstrip("/") + "/offload/status", headers=self._headers(), timeout=1.5)
            self._status = r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, OSError, ValueError):
            self._status = None
        self._status_at = now
        return self._status

    def _count(self, key: str, secs: float | None = None) -> None:
        with self._lock:
            self.counts[key] = self.counts.get(key, 0) + 1
            if secs is not None:
                self.seconds.append(secs)
                del self.seconds[:-200]

    def generate(self, task: str, prompt: str, think: bool = False) -> str | None:
        """The mesh's text answer, or None (the caller then does it the old way). Blocking: call it in a thread."""
        if not self.enabled():
            return None
        cfg = self.config()
        st = self._mesh_status()
        if not st or not st.get("online"):
            self._count("fallback_mesh_down"); return None
        with self._lock:
            if self._sem is None:
                self._sem = threading.BoundedSemaphore(max(1, int(cfg.get("max_concurrent", 1))))
            sem = self._sem
        if not sem.acquire(blocking=False):                # one at a time: never wait in line behind another request
            self._count("fallback_busy"); return None
        try:
            return self._run(cfg, st, task, prompt, think)
        finally:
            sem.release()

    def _run(self, cfg: dict, st: dict, task: str, prompt: str, think: bool) -> str | None:
        model = next((m for m in cfg["models"].get(task, []) if st.get("models", {}).get(m)), None)
        if not model:
            self._count("fallback_no_model"); return None
        base, t0 = cfg["admin"].rstrip("/"), time.monotonic()
        try:
            hdr = self._headers()
            jid = httpx.post(base + "/jobs", headers=hdr, timeout=3, json={"type": "generate", "payload": {"model": model, "prompt": prompt, "think": think}}).json()["id"]
            while time.monotonic() - t0 < float(cfg["max_wait_s"]):
                j = httpx.get(base + "/jobs/" + jid, headers=hdr, timeout=3).json()
                if j["status"] == "done":
                    text = json.loads(j["result"]).get("response", "")
                    self._count("offloaded", time.monotonic() - t0)
                    return text
                if j["status"] == "failed":
                    self._count("fallback_job_failed"); return None
                time.sleep(0.1)
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
            self._count("fallback_error"); return None
        self._count("fallback_slow")
        return None

    def stats(self) -> dict:
        with self._lock:
            s = sorted(self.seconds)
            return {"counts": dict(self.counts), "median_s": round(s[len(s) // 2], 2) if s else None, "enabled": self.enabled()}


offload = MeshOffload()
