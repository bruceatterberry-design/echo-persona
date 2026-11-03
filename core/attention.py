"""NDAS bridge, UCA 2.0 Phase 2: the activation network (core/ndas.py) fed by Echo's live events, SHADOW MODE.

Built from the memory graph (entities, visible memories, `relations` links) plus Echo's six cognitive systems, and
rebuilt when memory changes (a cheap count check in the quiet loop). Per message:
  on_message(t, entities, needs)  → stimulate the people/places named (from retrieval's entity detection) and the
                                     systems the controller says are needed; return a RECOMMENDATION made before seeing
                                     retrieval's results: top memories by activation + active systems
  on_retrieved(t, memory_ids)     → the memories really used prime the network, so the next turn starts in context
Nothing here changes a reply; core/server.py only logs the recommendation in the turn record next to what retrieval
did (ids only). Switched by ECHO_NDAS=1 (default off). Reads the memory DB, never writes it.
"""
from __future__ import annotations

import time

from core import ndas

SYSTEMS = ["executive", "memory", "language", "reasoning", "learning", "metacognition"]
NEED_TO_SYSTEM = {"executive": "executive", "language": "language", "memory": "memory", "memory_light": "memory",
                  "reasoning": "reasoning", "metacognition": "metacognition", "perception": "learning",
                  "learning": "learning"}
HOW_INPUT = {"named": 1.2, "speaker": 0.8, "you": 0.6}   # how strongly a detected entity is stimulated
RETRIEVED_INPUT = 0.9                                     # priming level for memories really used (θ = 1)
SYSTEM_INPUT = 1.0


class Attention:
    def __init__(self, store, params: ndas.Params | None = None, hub_split: bool = True):
        self.store, self.params, self.hub_split = store, params or ndas.Params(), hub_split
        self.net: ndas.Network | None = None
        self.signature: tuple | None = None
        self.builds = 0

    # ---- graph ----
    def _signature(self) -> tuple:
        db = self.store.db
        return (db.execute("SELECT count(*), coalesce(max(id),0) FROM memories WHERE hidden IS NULL").fetchone(),
                db.execute("SELECT count(*), coalesce(max(rowid),0) FROM relations").fetchone(),
                db.execute("SELECT count(*) FROM entities").fetchone())

    def build(self) -> None:
        db = self.store.db
        net = ndas.Network(self.params)
        for (i,) in db.execute("SELECT id FROM entities"):
            net.add_node(f"entity:{i}", "entity")
        for (i,) in db.execute("SELECT id FROM memories WHERE hidden IS NULL"):
            net.add_node(f"memory:{i}", "memory")
        for s in SYSTEMS:
            net.add_node(f"system:{s}", "system")
        links = [(f"{a}:{ai}", f"{b}:{bi}", float(w or 1.0)) for a, ai, b, bi, w in db.execute(
            "SELECT a_type, a_id, b_type, b_id, weight FROM relations WHERE status IS NULL OR status != 'retired'")]
        links = [x for x in links if x[0] in net.kind and x[1] in net.kind]
        deg: dict[str, int] = {}
        for a, b, _ in links:
            deg[a], deg[b] = deg.get(a, 0) + 1, deg.get(b, 0) + 1
        for a, b, w in links:   # a hub (e.g. "the owner", linked to most memories) splits its signal: weight / √degree
            if self.hub_split:
                net.connect(a, b, w / deg[a] ** 0.5, both=False)
                net.connect(b, a, w / deg[b] ** 0.5, both=False)
            else:
                net.connect(a, b, w)
        for s in SYSTEMS[1:]:
            net.connect("system:executive", f"system:{s}", 0.8)
        if self.net is not None:   # keep the old activation where the node still exists (continuity across rebuilds)
            for node in net.kind:
                if node in self.net.kind:
                    net.v[node], net.t_v[node] = self.net.v[node], self.net.t_v[node]
        self.net, self.builds = net, self.builds + 1

    def refresh_if_changed(self) -> bool:
        sig = self._signature()
        if sig != self.signature:
            self.signature = sig
            self.build()
            return True
        return False

    # ---- events ----
    def on_message(self, t: float, entities: list[dict], needs: list[str], k: int = 5) -> dict:
        if self.net is None:
            self.refresh_if_changed()
        t0, e0 = time.perf_counter(), self.net.events
        inp: dict[str, float] = {}
        for e in entities or []:
            node = f"entity:{e.get('id')}"
            inp[node] = max(inp.get(node, 0.0), HOW_INPUT.get(e.get("how", "named"), 0.6))
        for n in needs or []:
            if (s := NEED_TO_SYSTEM.get(n)):
                inp[f"system:{s}"] = SYSTEM_INPUT
        fired = self.net.stimulate(inp, t)
        mems = [int(n.split(":")[1]) for n, _ in self.net.active(t, k=k, kind="memory")]
        systems = sorted({f.node.split(":")[1] for f in fired if f.node.startswith("system:")})
        return {"memories": mems, "systems": systems, "fired": len(fired), "events": self.net.events - e0,
                "ms": round((time.perf_counter() - t0) * 1000, 3)}

    def on_retrieved(self, t: float, memory_ids: list[int]) -> None:
        """The memories really used are PRIMED (held just under threshold, then decaying), not fired: firing would reset
        them to rest and leave the unused ones stronger (found by tests/test_attention.py, 2026-11-01)."""
        if self.net is not None:
            for i in memory_ids or []:
                self.net.prime(f"memory:{i}", RETRIEVED_INPUT, t)
