"""Executive control (CA 2.0 Phase 7): one place that decides, with a logged reason for every decision.

What it decides (directive Layer C), all with plain rules and measurable signals — no extra model call:
  * module activation  — which one-time notes may join this message (grounding, metacognition, social, date sums,
                         self-model, tools). Each module still detects its own case; this decides whether it may run
                         and records what happened ("fired", "quiet", "switched_off", "command_not_done").
  * memory retrieval   — whether the memory search uses the previous message too (a real follow-up) or only this one.
  * resource / resumption — the wait for the first word is almost all prompt reading (first_token_ms ≈ prompt_ms on
                         41 real replies, 11-03). Measured (scripts/eval_executive.py, real model, copy of memory):
                         1. the old warm-up ended with a stand-in "Hello." message; qwen3.5 can't resume from inside
                            it, so the next message re-read ~500 tokens (2.5 s even with no memory notes). Fixed in
                            core/llm.py (exact_prefix): 2.5 → 0.25 s; with memory notes 4.7 → 2.5 s.
                         2. after a 30-minute break the window restarts: re-read once in quiet time (CacheKeeper.due):
                            memory questions 6–7 s → 3.2–3.8 s.
                         3. background jobs do NOT push the conversation out of the cache (re-read after a job: no
                            gain, 3.4 vs 3.2 s) — so no re-read for them.
                         What remains on memory questions (~2 s) is the length of the memory notes.
                         Each turn records the cache state at its start (fresh / disturbed:<job> / break).
  * task classification — core/controller.py (rules, < 1 ms) gives the task kind; it was shadow-only, now its kind
                         is part of every decision record.
The record (`TurnPlan.as_record()`, stored in the turn record as "exec") holds rule names and module names only, never
words from the message. Replaceable: nothing here depends on the model; switches are environment variables as before.

Retired here: NDAS (core/ndas.py + attention.py), the activation network that ran in shadow since 11-01. It never
changed a reply; measured: alone it found the right memory 13/88 vs 85/88 for retrieval and added 0 the retrieval
missed (UCA2_PHASE2_REPORT); the owner confirmed it is the "unsuccessful neural orchestrator" (decision D3, 11-02). Its
files and tests stay for the record; Echo no longer runs it (bringing it back = reverting the Phase 7 merge).
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

from core import focus, people

# ---- memory retrieval: is this message a follow-up that points back at the topic? ----
# Measured 11-03: "This is the owner, testing. It's freezing in here." was searched together with the previous question and
# brought three bridge-photo memories into a reply about the cold. Weather/time "it" and a spoken self-introduction
# don't point back at anything.
def _intro() -> re.Pattern:
    """"This is <name>, testing." / "It's me." at the start (names from this install's settings, core/people.py)."""
    return re.compile(r"^\W*(?:(?:hi|hey|hello|okay|ok)[, ]+)?(?:this is|it'?s|it is) (?:" + people.alternation(("me",)) +
                      r")\b[^.?!]*[.?!,]*\s*", re.I)
_DUMMY_IT = re.compile(r"\bit(?:'s| is| was| has been| feels| gets?| getting)\s+(?:so |really |too |very |pretty |a bit |"
                       r"kind of |getting )?(?:freezing|cold|chilly|hot|warm|boiling|humid|raining|snowing|windy|sunny|"
                       r"cloudy|dark|bright|late|early|noon|midnight|morning|evening|night|time\b)|"
                       r"\bwhat time is it\b|\bwhat day is it\b|\bhow(?:'s| is| was) it going\b|\bhow are things\b", re.I)


def referential(text: str) -> tuple[bool, str]:
    """(is a follow-up, rule name)."""
    t = _intro().sub("", text or "", count=1)
    if not t.strip():
        return False, "intro_only"
    rest = _DUMMY_IT.sub(" ", t)
    if rest != t and not focus.REF.search(rest):
        return False, "weather_or_time_it"
    if focus.is_referential(t):
        return True, "points_back"
    return False, "stands_alone"


# ---- module activation ----
SWITCHES = {"grounding": "ECHO_GROUNDING_NOTES", "name_check": "ECHO_METACOG_NOTES",
            "correction_check": "ECHO_METACOG_NOTES", "count_check": "ECHO_METACOG_NOTES",
            "social": "ECHO_SOCIAL_NOTES", "self_model": "ECHO_SELF_NOTES", "style": "ECHO_STYLE_NOTES",
            "style_filter": "ECHO_STYLE_FILTER"}


@dataclass
class TurnPlan:
    kind: str = "chat"                       # core/controller.py task kind
    kind_why: list[str] = field(default_factory=list)
    not_done: bool = False                   # an open/play request no command matched: only the "not done" note
    query: str = "as_said"                   # "with_previous" | "as_said"
    query_why: str = ""
    cache: str = "fresh"                     # the model's cache at the start of this turn: fresh | disturbed:<by> | break
    modules: dict[str, str] = field(default_factory=dict)   # module → fired | quiet | switched_off | command_not_done

    def may(self, module: str) -> bool:
        """May this module add its note to the message? Records why not."""
        if self.not_done:
            self.modules[module] = "command_not_done"
            return False
        env = SWITCHES.get(module)
        if env and os.environ.get(env, "1") == "0":
            self.modules[module] = "switched_off"
            return False
        self.modules.setdefault(module, "quiet")
        return True

    def fired(self, module: str) -> None:
        self.modules[module] = "fired"

    def as_record(self) -> dict:
        return {"kind": self.kind, "kind_why": self.kind_why, "query": self.query, "query_why": self.query_why,
                "cache": self.cache, "modules": dict(self.modules)}


def plan_turn(text: str, previous_user: str | None, kind: str = "chat", kind_why: list[str] | None = None,
              not_done: bool = False, cache: str = "fresh") -> tuple[TurnPlan, str]:
    """The plan for one message and the memory-search query it implies."""
    p = TurnPlan(kind=kind, kind_why=list(kind_why or []), not_done=not_done, cache=cache)
    is_ref, why = referential(text)
    if is_ref and previous_user:
        p.query, p.query_why = "with_previous", why
        return p, previous_user.strip()[-300:] + "\n" + text
    p.query_why = why if not is_ref else "no_previous_message"
    return p, text


# ---- the model's cache: state recorded per turn; re-read once after a break (see the module notes) ----
BREAK_S = 1800 + 60          # memory/store.py turns_window restarts the window after 30 min of quiet


class CacheKeeper:
    def __init__(self):
        self.reads: dict[str, int] = {}            # reason → count of conversation re-reads (startup, changed, break)
        self.break_done_for: int | None = None     # last turn id whose break was already re-read
        self.last: dict | None = None

    def state(self, disturbed: dict | None, last_turn_ts: float | None, now: float | None = None) -> str:
        """The cache as a new message arrives: fresh | disturbed:<by> | break."""
        now = now or time.time()
        if last_turn_ts and now - last_turn_ts > BREAK_S - 60:
            return "break"
        return f"disturbed:{disturbed['by']}" if disturbed else "fresh"

    def due(self, last_turn_id: int | None, last_turn_ts: float | None, now: float | None = None) -> str | None:
        """After a 30-minute break the conversation window restarts, so the next message would re-read everything:
        read it now, once per break (measured with the exact warm-up: memory questions 6–7 s → 3.2–3.8 s, small talk
        2.7 → 1.0 s). Not after background jobs: measured, they don't push the conversation out of the cache."""
        now = now or time.time()
        if last_turn_id and last_turn_ts and now - last_turn_ts > BREAK_S and self.break_done_for != last_turn_id:
            return "after_break"
        return None

    def done(self, why: str, ms: float, last_turn_id: int | None = None) -> None:
        if why == "after_break":
            self.break_done_for = last_turn_id
        self.reads[why] = self.reads.get(why, 0) + 1
        self.last = {"why": why, "ms": round(ms), "at": time.strftime("%H:%M:%S")}

    def snapshot(self) -> dict:
        return {"reads": dict(self.reads), "last": self.last}


# ---- reporting (/api/executive): how often each decision went which way, and what the cache did to latency ----
def report(records: list[dict]) -> dict:
    import statistics
    plans = [(r, r["exec"]) for r in records if isinstance(r.get("exec"), dict)]
    mods: dict[str, dict[str, int]] = {}
    queries: dict[str, int] = {}
    by_cache: dict[str, list[int]] = {}
    for r, e in plans:
        for m, v in (e.get("modules") or {}).items():
            mods.setdefault(m, {}).setdefault(v, 0)
            mods[m][v] += 1
        q = f"{e.get('query')}:{e.get('query_why')}"
        queries[q] = queries.get(q, 0) + 1
        if (r.get("route") or "").startswith("llm") and r.get("first_ms") is not None:
            by_cache.setdefault((e.get("cache") or "fresh").split(":")[0], []).append(r["first_ms"])

    def stats(v: list[int]) -> dict:
        v = sorted(v)
        return {"n": len(v), "median_ms": statistics.median(v), "p90_ms": v[max(0, int(len(v) * 0.9) - 1)],
                "max_ms": v[-1]}
    return {"turns": len(plans), "modules": mods, "memory_search": queries,
            "first_word_by_cache": {k: stats(v) for k, v in by_cache.items()}}
