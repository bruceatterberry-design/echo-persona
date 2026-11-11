"""EDIM + awareness wiring (Phase 3): the only place core/server.py touches core/dialogue.py and core/awareness.py.

Switches (read at startup; restart Echo after changing). Environment variable first, else config/edim.json:
    ECHO_EDIM       off (default) | shadow | on
    ECHO_AWARENESS  off (default) | shadow | on
  off     nothing runs, nothing is saved: Echo is exactly as before
  shadow  computed and recorded (turn record `dialogue`, /api/dialogue, /api/awareness, state.json `dialogue`),
          but NOTHING is added to what the model reads
  on      EDIM may add its one-time "[Conversation note: …]" (still through executive's module switch "dialogue";
          ECHO_EDIM_NOTES=0 turns just the note off). Awareness "on" = shadow for now: it has no consumer that
          changes a reply yet (Phase 4 decides).

Every call is wrapped: an EDIM failure is logged (type only) and the turn goes on as if EDIM were off.
Order of writes: EDIM owns state.json `dialogue`; awareness is RAM only; core/executive.py stays the only writer of
the workspace focus. Nothing here touches the memory database, the network or the language model.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from core import dialogue as dlg
from core.awareness import Awareness

log = logging.getLogger("echo.edim")
MODES = ("off", "shadow", "on")


SETTINGS = Path(__file__).resolve().parents[1] / "config" / "edim.json"   # {"edim": "on", "awareness": "off"}


def mode(env: str, settings: Path | None = None) -> str:
    """The switch: the environment variable if set, else config/edim.json, else off."""
    m = os.environ.get(env)
    if not m:
        try:
            key = {"ECHO_EDIM": "edim", "ECHO_AWARENESS": "awareness"}[env]
            m = json.loads((settings or SETTINGS).read_text()).get(key)
        except Exception:  # noqa: BLE001  missing or unreadable file = off
            m = None
    m = (m or "off").strip().lower()
    return m if m in MODES else "off"


class EdimLink:
    def __init__(self, cstate, edim: str | None = None, awareness: str | None = None):
        self.cstate = cstate
        self.edim_mode = edim or mode("ECHO_EDIM")
        self.aw_mode = awareness or mode("ECHO_AWARENESS")
        self.d: dlg.Dialogue | None = None
        self.aw: Awareness | None = None
        self.last: dict = {}
        self.errors = 0
        self.ms: list[float] = []
        if self.edim_mode != "off":
            try:
                self.d = dlg.load(cstate)
                if self.d.problems:
                    log.warning("dialogue state restored with %d problem(s)", len(self.d.problems))
            except Exception as e:  # noqa: BLE001
                self._fail("load", e)
        if self.aw_mode != "off":
            self.aw = Awareness()

    def _fail(self, where: str, e: Exception) -> None:
        self.errors += 1
        log.warning("edim %s failed: %s", where, type(e).__name__)

    @property
    def active(self) -> bool:
        return self.d is not None or self.aw is not None

    # ---- a person's message (every route), before anything answers it ----
    def user(self, text: str, speaker: str | None, how: str = "voice") -> None:
        self.last = {}
        if not self.active or not (text or "").strip():
            return
        t0 = time.perf_counter()
        try:
            if self.d is not None:
                conf = {"voice": 0.9, "told": 1.0, "sticky": 0.6, "typed": 0.7}.get(how, 0.5)
                up = self.d.observe_user(text, speaker if speaker and speaker != "unknown" else None, conf, how=how)
                self.last = {"up": up}
            if self.aw is not None:
                if self.d is not None:
                    self.aw.post_many(self.d.bids())
                act = self.d.active() if self.d is not None else None
                self.aw.goal_ref = f"thread:{act['id']}" if act else None
                self.last["focus"] = self.aw.cycle("turn")
        except Exception as e:  # noqa: BLE001
            self._fail("user", e)
            self.last = {}
        self.ms = (self.ms + [round((time.perf_counter() - t0) * 1000, 3)])[-200:]

    def skip(self) -> None:
        self.last = {}

    def note(self) -> str | None:
        """The one-time note for this message, only in mode 'on'."""
        up = self.last.get("up")
        if self.edim_mode != "on" or up is None:
            return None
        return up.plan.get("note")

    def curiosity_ok(self) -> tuple[bool, str]:
        """May Echo add his own curiosity question to this reply? EDIM off (or failing) = yes, as before."""
        up = self.last.get("up")
        if self.d is None or up is None or self.edim_mode != "on":
            return True, "edim_not_on"
        try:
            ok, why = self.d.may_ask_own(up)
        except Exception as e:  # noqa: BLE001
            self._fail("curiosity", e)
            return True, "edim_error"
        self.last["curiosity"] = "ok" if ok else f"held:{why}"
        return ok, why

    def reply(self, text: str) -> None:
        if self.d is None:
            return
        try:
            self.d.observe_reply(text)
        except Exception as e:  # noqa: BLE001
            self._fail("reply", e)

    # ---- end of the turn: the record (ids and rule names only) and the saved state ----
    def record(self) -> dict:
        if not self.active:
            return {}
        out: dict = {"edim": self.edim_mode, "awareness": self.aw_mode, "ms": self.ms[-1] if self.ms else None}
        try:
            if (up := self.last.get("up")) is not None:
                out.update(up.record())
            if self.last.get("curiosity"):
                out["curiosity"] = self.last["curiosity"]
            if (f := self.last.get("focus")) is not None:
                out["focus"], out["faded"], out["cycle_ms"] = f.focus, f.faded, f.ms
            if self.d is not None:
                dlg.save(self.cstate, self.d)
        except Exception as e:  # noqa: BLE001
            self._fail("record", e)
        return out

    def report(self) -> dict:
        ms = sorted(self.ms)
        out = {"edim": self.edim_mode, "awareness": self.aw_mode, "errors": self.errors,
               "update_ms": {"n": len(ms), "median": ms[len(ms) // 2] if ms else None,
                             "p95": ms[int(len(ms) * 0.95)] if ms else None},
               "llm_calls": 0, "persisted": self.d is not None}
        try:
            if self.d is not None:
                out["dialogue"] = self.d.snapshot()
            if self.aw is not None:
                out["awareness_state"] = self.aw.snapshot()
        except Exception as e:  # noqa: BLE001
            self._fail("report", e)
        return out
