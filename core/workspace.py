"""Cognitive workspace, Stage 1a (docs/cognitive/DEVELOPMENT_RISK_ASSESSMENT.md, approved by the owner 2026-11-01).

OBSERVE ONLY: one structured record per conversation turn (which path handled it, who spoke, which memories were
used, tools, LLM calls, timings). It changes nothing about how Echo answers. Later stages read these records
(controller shadow mode, confidence labels, the dashboard).

Privacy: NO message text, reply text, addresses or memory contents, only ids, counts, names of code paths and times.
Kept 30 days (the owner, 2026-11-01), then deleted. Its own database (data/workspace.sqlite), separate from memory.
Never breaks a conversation: every write is wrapped and failures are only logged.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("echo.workspace")

KEEP_DAYS = 30
ROUTES = ("voice_confirm", "trainer", "music", "browser", "llm", "llm_tools", "llm_error")


@dataclass
class TurnRecord:
    started: float = field(default_factory=time.time)
    t0: float = field(default_factory=time.perf_counter, repr=False)
    route: str = ""                      # which existing path answered (ROUTES)
    speaker: str = "typed"               # owner / loved_one / unknown / typed (who, never what)
    remembered: bool = False             # "remember that…" saved a memory this turn
    hints: list[str] = field(default_factory=list)       # one-time notes added: follow / ask / not_done / voice_ask
    memory_ids: list[int] = field(default_factory=list)  # memories retrieval put in the prompt
    recall_ms: float | None = None
    meaning_search: bool | None = None                 # False = the query vector timed out (>1 s): keywords only
    tools: list[str] = field(default_factory=list)       # tool names + ok/no, no addresses
    llm_calls: int = 0
    prompt_tokens: int | None = None
    reply_tokens: int | None = None
    first_ms: int | None = None
    total_ms: int | None = None
    plan: dict = field(default_factory=dict)            # Stage 2a: core/controller.py's decision (shadow, not used)
    plan_agrees: bool | None = None                    # does the decision match the route Echo really took?
    grounding: dict = field(default_factory=dict)      # Stage 4a: memory/grounding.py label (shadow, not used)
    skill_shadow: list = field(default_factory=list)   # Stage 6b: shortcut vs model (agree, decision_ms), not used
    ecw: dict = field(default_factory=dict)            # UCA2 Phase 2: live workspace snapshot at the end of the turn
    ndas: dict = field(default_factory=dict)           # UCA2 Phase 2: NDAS shadow (retired in CA 2.0 Phase 7: empty)
    exec: dict = field(default_factory=dict)           # CA 2.0 Phase 7: core/executive.py decisions + reasons

    def judge_plan(self) -> None:
        """command ↔ browser/music, research ↔ llm_tools, chat/memory/reasoning ↔ llm (voice_confirm/trainer:
        not judged; llm_error: unknown)."""
        kind, r = self.plan.get("kind"), self.route
        if not kind or r in ("voice_confirm", "trainer", "llm_error"):
            self.plan_agrees = None
        else:
            want = {"command": ("browser", "music"), "research": ("llm_tools",)}.get(kind, ("llm",))
            self.plan_agrees = r in want

    def first_word(self) -> None:
        if self.first_ms is None:
            self.first_ms = round((time.perf_counter() - self.t0) * 1000)


ECW_EVENTS = 20   # pending_events kept per turn


@dataclass
class LiveState:
    """Echo Cognitive Workspace (UCA 2.0 Phase 2): the ONE live, per-turn working state the cognitive systems share.
    Temporary working context only (ids and labels, never memory contents or message text); the authoritative memory
    stays in the memory DB. Parts of a turn publish() events; the end-of-turn snapshot goes into the TurnRecord."""
    current_goal: str | None = None          # controller kind: chat / memory / reasoning / research / command
    active_context: dict = field(default_factory=dict)    # speaker, route
    related_concepts: list = field(default_factory=list)  # entity ids named
    retrieved_evidence: list = field(default_factory=list)  # memory ids retrieval used
    confidence: str | None = None            # grounding label (memory/grounding.py)
    next_action: str | None = None           # reply / tools / command
    active_systems: list = field(default_factory=list)    # controller's needs
    activation_signals: dict = field(default_factory=dict)  # NDAS recommendation (shadow)
    pending_events: list = field(default_factory=list)
    task_priority: str = "live"              # live conversation; background jobs would be "background"

    def publish(self, event: str, **kw) -> None:
        for k, v in kw.items():
            if hasattr(self, k):
                setattr(self, k, v)
        if len(self.pending_events) < ECW_EVENTS:
            self.pending_events.append(event)

    def snapshot(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], {})}


class WorkspaceLog:
    def __init__(self, path: Path, keep_days: int = KEEP_DAYS):
        self.path, self.keep_days = Path(path), keep_days
        self.lock = threading.Lock()
        self._pruned = 0.0
        try:
            with self._db() as c:
                c.execute("""CREATE TABLE IF NOT EXISTS turns (id INTEGER PRIMARY KEY, started REAL NOT NULL,
                             route TEXT, speaker TEXT, total_ms INTEGER, first_ms INTEGER, llm_calls INTEGER,
                             record TEXT NOT NULL)""")
                c.execute("CREATE INDEX IF NOT EXISTS turns_started ON turns(started)")
        except Exception as e:  # noqa: BLE001
            log.warning("workspace log unavailable: %s", type(e).__name__)

    def _db(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=2)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def save(self, rec: TurnRecord, route: str | None = None) -> None:
        """Finish and store a record. Never raises."""
        try:
            if route:
                rec.route = route
            rec.judge_plan()
            rec.total_ms = round((time.perf_counter() - rec.t0) * 1000)
            d = {k: v for k, v in asdict(rec).items() if k != "t0"}
            with self.lock, self._db() as c:
                c.execute("INSERT INTO turns(started, route, speaker, total_ms, first_ms, llm_calls, record) "
                          "VALUES (?,?,?,?,?,?,?)", (rec.started, rec.route, rec.speaker, rec.total_ms, rec.first_ms,
                                                     rec.llm_calls, json.dumps(d)))
                if time.time() - self._pruned > 3600:
                    self._prune(c)
        except Exception as e:  # noqa: BLE001
            log.warning("workspace record not saved: %s", type(e).__name__)

    def _prune(self, c: sqlite3.Connection) -> None:
        n = c.execute("DELETE FROM turns WHERE started < ?", (time.time() - self.keep_days * 86400,)).rowcount
        self._pruned = time.time()
        if n:
            log.info("workspace: %d records older than %d days deleted", n, self.keep_days)

    def prune(self) -> None:
        try:
            with self.lock, self._db() as c:
                self._prune(c)
        except Exception as e:  # noqa: BLE001
            log.warning("workspace prune failed: %s", type(e).__name__)

    def recent(self, limit: int = 50) -> list[dict]:
        with self._db() as c:
            return [json.loads(r[0]) for r in
                    c.execute("SELECT record FROM turns ORDER BY id DESC LIMIT ?", (limit,))]

    def records(self, days: float = 30.0) -> list[dict]:
        """All records of the last `days` (oldest first), e.g. for core/skills.py."""
        with self._db() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT record FROM turns WHERE started >= ? ORDER BY id",
                                                         (time.time() - days * 86400,))]

    def summary(self, days: float = 1.0) -> dict:
        """Counts per route and median/p95 times over the last `days` (for the status page / dashboard)."""
        with self._db() as c:
            rows = c.execute("SELECT route, total_ms, first_ms, llm_calls, record FROM turns WHERE started >= ?",
                             (time.time() - days * 86400,)).fetchall()
        judged = [a for a in (json.loads(r[4]).get("plan_agrees") for r in rows) if a is not None]
        recs = [json.loads(r[4]) for r in rows]
        fell_back = sum(1 for r in recs if r.get("meaning_search") is False)
        searched = sum(1 for r in recs if r.get("meaning_search") is not None)
        grounding: dict[str, int] = {}
        for r in rows:
            if (g := json.loads(r[4]).get("grounding", {}).get("label")):
                grounding[g] = grounding.get(g, 0) + 1

        def pct(xs, p):
            xs = sorted(x for x in xs if x is not None)
            return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else None
        routes: dict[str, int] = {}
        for r in rows:
            routes[r[0]] = routes.get(r[0], 0) + 1
        llm = [r for r in rows if (r[0] or "").startswith("llm")]
        return {"days": days, "turns": len(rows), "routes": routes,
                "first_ms_p50": pct([r[2] for r in llm], .5), "first_ms_p95": pct([r[2] for r in llm], .95),
                "total_ms_p50": pct([r[1] for r in rows], .5), "total_ms_p95": pct([r[1] for r in rows], .95),
                "llm_calls": sum(r[3] or 0 for r in rows),
                "controller_agrees": f"{sum(judged)}/{len(judged)}" if judged else None, "grounding": grounding,
                "meaning_search_fell_back": f"{fell_back}/{searched}" if searched else None}
