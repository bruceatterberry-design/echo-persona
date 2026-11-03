"""UCA 2.0 Phase 1: NDAS mechanisms, each tested on its own; both engines must behave the same."""
import math

import pytest

from core import ndas


def chain(engine=ndas.Network, **kw):
    """A — B — C — D, weights 1; plus E inhibits D."""
    n = engine(ndas.Params(**kw))
    for x in "ABCDE":
        n.add_node(x, "memory")
    n.connect("A", "B", 1.0)
    n.connect("B", "C", 1.0)
    n.connect("C", "D", 1.0)
    n.connect("E", "D", -2.0, both=False)
    return n.freeze() if hasattr(n, "freeze") else n


ENGINES = [ndas.Network, ndas.ArrayNetwork]


@pytest.mark.parametrize("E", ENGINES)
def test_threshold(E):
    n = chain(E)
    assert n.stimulate({"A": 0.9}, t=0) == []                       # below threshold: no firing
    assert n.potential("A", 0) == pytest.approx(0.9)
    assert [f.node for f in n.stimulate({"A": 0.2}, t=0)] == ["A"]  # inputs add up → fires


@pytest.mark.parametrize("E", ENGINES)
def test_decay_is_exponential_and_lazy(E):
    n = chain(E, tau_s=60)
    n.stimulate({"A": 0.8}, t=0)
    assert n.potential("A", 60) == pytest.approx(0.8 * math.exp(-1), rel=1e-5)
    assert n.potential("A", 60 * math.log(2)) == pytest.approx(0.4, rel=1e-5)   # half-life τ·ln2
    assert n.stimulate({"A": 0.5}, t=600) == []        # old input has faded: 0.8e^-10 + 0.5 < 1


@pytest.mark.parametrize("E", ENGINES)
def test_propagation_scales_by_weight_and_gain_and_fades_with_distance(E):
    n = chain(E, gain=0.6)
    fired = n.stimulate({"A": 2.0}, t=0)
    assert [f.node for f in fired] == ["A", "B"]         # 2.0·0.6 = 1.2 fires B; B → C gets 0.72 < 1
    assert n.potential("C", 0) == pytest.approx(1.2 * 0.6, rel=1e-5)
    assert n.potential("D", 0) == 0.0                     # never reached
    assert [f.depth for f in fired] == [0, 1]


@pytest.mark.parametrize("E", ENGINES)
def test_inhibition_prevents_firing(E):
    n = chain(E, gain=1.0)
    n.stimulate({"E": 1.0}, t=0)                          # E fires, sends −2 to D
    assert n.potential("D", 0) == pytest.approx(-2.0)
    assert n.stimulate({"D": 1.5}, t=0) == []             # would fire alone, inhibited now
    m = chain(E, gain=1.0)
    assert [f.node for f in m.stimulate({"D": 1.5}, t=0)][:1] == ["D"]


@pytest.mark.parametrize("E", ENGINES)
def test_refractory(E):
    n = chain(E, refractory_s=5)
    assert n.stimulate({"A": 1.0}, t=0)
    assert n.stimulate({"A": 5.0}, t=1) == []             # too soon
    assert n.stimulate({"A": 1.0}, t=6)                   # recovered


@pytest.mark.parametrize("E", ENGINES)
def test_budget_caps_a_cascade(E):
    n = E(ndas.Params(gain=1.0, max_events=10, refractory_s=0))
    for i in range(100):                                  # a ring that would fire forever without limits
        n.connect(f"n{i}", f"n{(i + 1) % 100}", 1.0, both=False)
    if hasattr(n, "freeze"):
        n.freeze()
    before = n.events
    n.stimulate({"n0": 1.0}, t=0)
    assert n.events - before == 10


@pytest.mark.parametrize("E", ENGINES)
def test_threshold_per_node_and_selective_activation(E):
    n = E(ndas.Params())
    n.add_node("easy", "memory", threshold=0.3)
    n.add_node("hard", "memory", threshold=5.0)
    n.add_node("sys", "system")
    if hasattr(n, "freeze"):
        n.freeze()
    fired = n.stimulate({"easy": 0.4, "hard": 0.4, "sys": 0.2}, t=0)
    assert [f.node for f in fired] == ["easy"]
    act = n.active(t=0, floor=0.1)
    assert [a[0] for a in act] == ["hard", "sys"]          # easy reset after firing; strongest first
    assert n.active(t=0, floor=0.1, k=1) == act[:1]        # winners-take-all
    assert n.active(t=0, floor=0.1, kind="system")[0][0] == "sys"


@pytest.mark.parametrize("E", ENGINES)
def test_low_activation_never_deletes(E):
    n = chain(E)
    n.stimulate({"A": 0.5}, t=0)
    n.potential("A", 10_000)
    assert n.potential("A", 10_000) < 1e-9 and "A" in (n.kind if hasattr(n, "kind") else n.ids)


@pytest.mark.parametrize("E", ENGINES)
def test_temporal_log(E):
    n = chain(E, refractory_s=0)
    n.stimulate({"A": 1.0}, t=10)          # A fires, B gets 0.6
    n.stimulate({"C": 1.0}, t=20)          # C fires; B's leftover 0.6·e^(−10/60) + 0.6 crosses 1 → B fires too
    recent = n.recently_fired(15)
    assert [f.node for f in recent] == ["C", "B"] and all(f.t == 20 for f in recent)
    assert [f.node for f in n.recently_fired(0)] == ["A", "C", "B"]


def test_engines_agree_on_a_random_graph():
    import random
    rnd = random.Random(7)
    a = ndas.Network(ndas.Params(gain=0.8))
    for i in range(300):
        a.add_node(f"x{i}", "memory", threshold=rnd.choice([0.8, 1.0, 1.3]))
    for _ in range(900):
        a.connect(f"x{rnd.randrange(300)}", f"x{rnd.randrange(300)}", rnd.choice([-1.0, 0.4, 0.9, 1.5]))
    b = ndas.ArrayNetwork.from_network(a)
    for t in range(0, 100, 7):
        inp = {f"x{rnd.randrange(300)}": rnd.uniform(0.5, 2.5) for _ in range(3)}
        fa, fb = a.stimulate(dict(inp), t), b.stimulate(dict(inp), t)
        assert [f.node for f in fa] == [f.node for f in fb]
    for node in list(a.kind)[:50]:
        assert a.potential(node, 120) == pytest.approx(b.potential(node, 120), rel=1e-4, abs=1e-6)


@pytest.mark.parametrize("E", ENGINES)
def test_prime_holds_below_threshold_without_firing(E):
    n = chain(E)
    n.prime("A", 5.0, t=0)                     # capped just under threshold, never fires
    assert n.recently_fired(0) == [] and 0.98 < n.potential("A", 0) < 1.0
    n.prime("A", 0.2, t=0)                     # never lowers
    assert n.potential("A", 0) > 0.98
    assert [f.node for f in n.stimulate({"A": 0.05}, t=0)][:1] == ["A"]   # a small push now fires it
