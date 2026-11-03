"""NDAS: Neural Dynamics and Attention System, UCA 2.0 Phase 1 (isolated prototype; NOT wired into Echo).

A small event-driven activation network, inspired by spiking neurons but plain arithmetic:
  * every node (a person, place, memory, concept or one of Echo's cognitive systems) has an activation potential, a
    firing threshold and a refractory period;
  * activation DECAYS exponentially with time, computed lazily when the node is touched (no polling, no clock thread);
  * stimulate() adds input; a node whose potential reaches its threshold FIRES: it propagates
    potential × edge weight × gain to its neighbours (negative weights inhibit), then resets and is refractory;
  * propagation is processed by priority (strongest input first) under an event budget, so one message can never
    flood the graph;
  * optional k-winners-take-all keeps activation sparse (lateral inhibition);
  * every firing is time-stamped in a bounded log (temporal activation tracking).
Nothing is ever deleted: low activation only means "not active now". No LLM, no I/O, no threads.

Two engines with the same behaviour (tests check they agree): `Network` (dicts, pure Python) and `ArrayNetwork`
(numpy arrays + CSR adjacency), compared by scripts/bench_ndas.py.
"""
from __future__ import annotations

import heapq
import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Params:
    tau_s: float = 60.0           # decay time constant: potential × e^(−Δt/τ); half-life = τ·ln2 ≈ 42 s
    threshold: float = 1.0        # default firing threshold
    gain: float = 0.6             # share of a firing node's output passed along an edge of weight 1
    refractory_s: float = 5.0     # a node can't fire again for this long
    rest: float = 0.0             # potential after firing
    max_events: int = 200         # propagation budget per stimulate() call
    min_signal: float = 0.05      # weaker signals aren't propagated (sparse activity)
    log_size: int = 1000          # firings kept for temporal tracking


@dataclass
class Firing:
    node: str
    t: float
    potential: float
    depth: int


class Network:
    """Dict-based engine (reference implementation)."""

    def __init__(self, params: Params | None = None):
        self.p = params or Params()
        self.kind: dict[str, str] = {}
        self.theta: dict[str, float] = {}
        self.v: dict[str, float] = {}          # potential at time self.t_v[node]
        self.t_v: dict[str, float] = {}
        self.refractory_until: dict[str, float] = {}
        self.out: dict[str, list[tuple[str, float]]] = {}
        self.log: deque[Firing] = deque(maxlen=self.p.log_size)
        self.events = 0                        # total propagation events processed (for budgets / benchmarks)

    # ---- structure ----
    def add_node(self, node: str, kind: str = "concept", threshold: float | None = None) -> None:
        if node not in self.kind:
            self.kind[node], self.v[node], self.t_v[node] = kind, 0.0, 0.0
            self.out[node] = []
        if threshold is not None:
            self.theta[node] = threshold

    def connect(self, a: str, b: str, weight: float, both: bool = True) -> None:
        self.add_node(a)
        self.add_node(b)
        self.out[a].append((b, weight))
        if both:
            self.out[b].append((a, weight))

    # ---- dynamics ----
    def potential(self, node: str, t: float) -> float:
        """Decayed potential at time t (lazy: nothing runs between events)."""
        dt = max(0.0, t - self.t_v.get(node, t))
        return self.v.get(node, 0.0) * math.exp(-dt / self.p.tau_s)

    def _set(self, node: str, value: float, t: float) -> None:
        self.v[node], self.t_v[node] = value, t

    def stimulate(self, inputs: dict[str, float], t: float) -> list[Firing]:
        """Add input to nodes at time t and run the resulting cascade (priority order, budgeted). Returns firings."""
        heap = [(-amount, 0, i, node) for i, (node, amount) in enumerate(inputs.items()) if node in self.kind]
        heapq.heapify(heap)
        seq, fired, budget = len(heap), [], self.p.max_events
        while heap and budget > 0:
            neg, depth, _, node = heapq.heappop(heap)
            budget -= 1
            self.events += 1
            v = self.potential(node, t) + (-neg)
            self._set(node, v, t)
            if v < self.theta.get(node, self.p.threshold) or t < self.refractory_until.get(node, -1.0):
                continue
            f = Firing(node, t, v, depth)
            fired.append(f)
            self.log.append(f)
            self._set(node, self.p.rest, t)
            self.refractory_until[node] = t + self.p.refractory_s
            for nb, w in self.out[node]:
                sig = v * w * self.p.gain
                if abs(sig) >= self.p.min_signal:
                    seq += 1
                    heapq.heappush(heap, (-sig, depth + 1, seq, nb))
        return fired

    def prime(self, node: str, level: float, t: float) -> None:
        """Raise a node's potential to at least `level` WITHOUT firing (a working-memory trace: what was just used stays
        near threshold and decays, instead of firing and resetting to rest)."""
        if node in self.kind:
            self._set(node, max(self.potential(node, t), min(level, self.theta.get(node, self.p.threshold) * 0.99)), t)

    def active(self, t: float, k: int | None = None, kind: str | None = None, floor: float = 0.05) -> list[tuple[str, float]]:
        """Nodes with potential ≥ floor (optionally of one kind), strongest first; k = winners-take-all."""
        rows = [(n, self.potential(n, t)) for n in self.kind if kind is None or self.kind[n] == kind]
        rows = sorted((r for r in rows if r[1] >= floor), key=lambda r: -r[1])
        return rows[:k] if k else rows

    def recently_fired(self, since: float) -> list[Firing]:
        return [f for f in self.log if f.t >= since]


class ArrayNetwork:
    """numpy engine: same behaviour, arrays + CSR adjacency. Build with from_network() or add/connect then freeze()."""

    def __init__(self, params: Params | None = None):
        self.p = params or Params()
        self.ids: dict[str, int] = {}
        self.names: list[str] = []
        self.kinds: list[str] = []
        self._edges: list[tuple[int, int, float]] = []
        self._theta: dict[int, float] = {}
        self.log: deque[Firing] = deque(maxlen=self.p.log_size)
        self.events = 0
        self.frozen = False

    def add_node(self, node: str, kind: str = "concept", threshold: float | None = None) -> None:
        if node not in self.ids:
            self.ids[node] = len(self.names)
            self.names.append(node)
            self.kinds.append(kind)
        if threshold is not None:
            self._theta[self.ids[node]] = threshold

    def connect(self, a: str, b: str, weight: float, both: bool = True) -> None:
        self.add_node(a)
        self.add_node(b)
        self._edges.append((self.ids[a], self.ids[b], weight))
        if both:
            self._edges.append((self.ids[b], self.ids[a], weight))

    def freeze(self) -> "ArrayNetwork":
        n = len(self.names)
        e = sorted(self._edges, key=lambda x: x[0])   # stable: keeps each node's links in insertion order (as Network)
        src = np.array([x[0] for x in e], dtype=np.int32)
        self.dst = np.array([x[1] for x in e], dtype=np.int32)
        self.w = np.array([x[2] for x in e], dtype=np.float64)
        self.indptr = np.zeros(n + 1, dtype=np.int32)
        np.add.at(self.indptr, src + 1, 1)
        self.indptr = np.cumsum(self.indptr, dtype=np.int32)
        self.theta = np.full(n, self.p.threshold, dtype=np.float64)
        for i, th in self._theta.items():
            self.theta[i] = th
        self.v = np.zeros(n, dtype=np.float64)
        self.t_v = np.zeros(n, dtype=np.float64)
        self.refr = np.full(n, -1.0, dtype=np.float64)
        self.frozen = True
        return self

    @classmethod
    def from_network(cls, net: Network) -> "ArrayNetwork":
        a = cls(net.p)
        for node, kind in net.kind.items():
            a.add_node(node, kind, net.theta.get(node))
        for node, outs in net.out.items():
            for nb, w in outs:
                a._edges.append((a.ids[node], a.ids[nb], w))
        return a.freeze()

    def _pot(self, i: int, t: float) -> float:
        return float(self.v[i]) * math.exp(-max(0.0, t - self.t_v[i]) / self.p.tau_s)

    def potential(self, node: str, t: float) -> float:
        return self._pot(self.ids[node], t) if node in self.ids else 0.0

    def prime(self, node: str, level: float, t: float) -> None:
        if node in self.ids:
            i = self.ids[node]
            self.v[i], self.t_v[i] = max(self._pot(i, t), min(level, float(self.theta[i]) * 0.99)), t

    def stimulate(self, inputs: dict[str, float], t: float) -> list[Firing]:
        heap = [(-a, 0, k, self.ids[n]) for k, (n, a) in enumerate(inputs.items()) if n in self.ids]
        heapq.heapify(heap)
        seq, fired, budget = len(heap), [], self.p.max_events
        while heap and budget > 0:
            neg, depth, _, i = heapq.heappop(heap)
            budget -= 1
            self.events += 1
            v = self._pot(i, t) + (-neg)
            self.v[i], self.t_v[i] = v, t
            if v < self.theta[i] or t < self.refr[i]:
                continue
            f = Firing(self.names[i], t, v, depth)
            fired.append(f)
            self.log.append(f)
            self.v[i], self.refr[i] = self.p.rest, t + self.p.refractory_s
            lo, hi = self.indptr[i], self.indptr[i + 1]
            sig = v * self.w[lo:hi] * self.p.gain
            keep = np.abs(sig) >= self.p.min_signal
            for nb, s in zip(self.dst[lo:hi][keep].tolist(), sig[keep].tolist()):
                seq += 1
                heapq.heappush(heap, (-s, depth + 1, seq, nb))
        return fired

    def active(self, t: float, k: int | None = None, kind: str | None = None, floor: float = 0.05) -> list[tuple[str, float]]:
        pot = self.v * np.exp(-np.maximum(0.0, t - self.t_v) / self.p.tau_s)
        idx = np.nonzero(pot >= floor)[0]
        rows = [(self.names[i], float(pot[i])) for i in idx if kind is None or self.kinds[i] == kind]
        rows.sort(key=lambda r: -r[1])
        return rows[:k] if k else rows

    def recently_fired(self, since: float) -> list[Firing]:
        return [f for f in self.log if f.t >= since]
