"""Awareness (Phase 2, ISOLATED): what Echo is attending to right now, as a small, inspectable bid list.

Design principles (docs/cognitive/edim/PHASE1_DESIGN.md §6): many sources post bids; a narrow spotlight (top 3) wins;
the winners are handed to subscribers. This is bookkeeping built on ideas from neuroscience (global workspace,
selection over time, monitoring); it does not claim or imply experience.

Rules only, RAM only, no model call, no network, no timer: a cycle runs when something happens (a turn, a reply, a
dream result, the 15 s quiet loop). Late tower results are just bids that expire; nothing ever waits for them.
The focus is advice for Executive Control (core/executive.py), which stays the only writer of the workspace.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

K = 3
FIXATION_CYCLES, HOLD_OUT = 5, 2       # in focus 5 cycles running → held out for 2
QUIET_AFTER = 10                       # a source absent this many cycles gets slot 3
MAX_BIDS = 64
DECAY = 0.05


@dataclass
class Bid:
    ref: str            # "thread:t3", "question:q4", "dream:435fa57e", "memory:202", "system:tower_offline"
    source: str         # edim | metacog | dream | daydream | initiative | system
    salience: float     # 0–1, from the source's own signals
    conf: float
    provenance: str     # rule or module name, never conversation text
    at: float
    expires: float


@dataclass
class Focus:
    items: list[dict]
    faded: list[str]
    cycle: int
    reason: str
    ms: float = 0.0
    why: dict = field(default_factory=dict)

    @property
    def focus(self) -> list[str]:
        return [i["ref"] for i in self.items]


class Awareness:
    def __init__(self, clock=time.time, goal_ref: str | None = None):
        self.clock = clock
        self.bids: dict[str, Bid] = {}
        self.cycle_n = 0
        self.streak: dict[str, int] = {}       # ref → cycles in focus running
        self.held: dict[str, int] = {}         # ref → cycles left out (fixation guard)
        self.seen: dict[str, int] = {}         # source → last cycle it was in focus
        self.focus: list[str] = []
        self.goal_ref = goal_ref
        self.subs: list = []
        self.timeline: list[dict] = []         # last 50 cycles, refs and scores only

    def post(self, bid: Bid) -> None:
        if not (0 <= bid.salience <= 1) or bid.expires <= self.clock():
            return
        self.bids[bid.ref] = bid
        if len(self.bids) > MAX_BIDS:
            for r in sorted(self.bids, key=lambda r: self.bids[r].at)[: len(self.bids) - MAX_BIDS]:
                self.bids.pop(r, None)

    def post_many(self, bids) -> None:
        for b in bids:
            self.post(b)

    def subscribe(self, fn) -> None:
        self.subs.append(fn)

    def score(self, b: Bid) -> float:
        s = b.salience * (0.5 + 0.5 * b.conf)
        if self.goal_ref and b.ref == self.goal_ref:
            s += 0.15
        return round(s - DECAY * self.streak.get(b.ref, 0), 4)

    def cycle(self, reason: str = "event") -> Focus:
        t0 = time.perf_counter()
        now = self.clock()
        self.cycle_n += 1
        for r in [r for r, b in self.bids.items() if b.expires <= now]:
            self.bids.pop(r)
        for r in list(self.held):
            self.held[r] -= 1
            if self.held[r] <= 0:
                self.held.pop(r)
        ranked = sorted((b for b in self.bids.values() if b.ref not in self.held), key=self.score, reverse=True)
        chosen = ranked[:K]
        # quiet slot: the best bid from a source not in focus for a long time takes slot 3
        quiet = [b for b in ranked[K:] if self.cycle_n - self.seen.get(b.source, 0) >= QUIET_AFTER
                 and b.source not in {c.source for c in chosen}]
        why = {}
        if quiet and len(chosen) == K:
            why[quiet[0].ref] = "quiet_slot"
            chosen[-1] = quiet[0]
        refs = [b.ref for b in chosen]
        for r in refs:
            self.streak[r] = self.streak.get(r, 0) + 1
            if self.streak[r] >= FIXATION_CYCLES:
                self.held[r], self.streak[r] = HOLD_OUT, 0
                why[r] = "fixation_guard_next"
        for r in list(self.streak):
            if r not in refs:
                self.streak.pop(r)
        for b in chosen:
            self.seen[b.source] = self.cycle_n
        faded = [r for r in self.focus if r not in refs]
        self.focus = refs
        f = Focus([{"ref": b.ref, "source": b.source, "score": self.score(b)} for b in chosen], faded, self.cycle_n,
                  reason, round((time.perf_counter() - t0) * 1000, 3), why)
        self.timeline = (self.timeline + [{"c": self.cycle_n, "at": now, "reason": reason, "focus": refs,
                                           "faded": faded}])[-50:]
        for fn in self.subs:
            try:
                fn(f)
            except Exception:  # noqa: BLE001  a subscriber can never break the cycle
                pass
        return f

    def snapshot(self) -> dict:
        return {"cycle": self.cycle_n, "focus": self.focus, "bids": len(self.bids),
                "held": dict(self.held), "timeline": self.timeline[-10:],
                "bid_list": [asdict(b) for b in sorted(self.bids.values(), key=self.score, reverse=True)[:10]]}
