"""Awareness (core/awareness.py), isolated: spotlight, fixation guard, quiet slot, expiry, EDIM bids (A1–A3, A7)."""
from core.awareness import Awareness, Bid
from core.dialogue import Dialogue


class Clock:
    def __init__(self):
        self.t = 1_762_700_000.0

    def __call__(self):
        return self.t


def bid(ref, src="edim", s=0.5, now=1_762_700_000.0, life=600):
    return Bid(ref, src, s, 0.9, "test", now, now + life)


def test_top_k_and_broadcast():
    c = Clock()
    a = Awareness(clock=c)
    got = []
    a.subscribe(got.append)
    a.subscribe(lambda f: 1 / 0)                     # a broken subscriber never breaks the cycle
    for i, s in enumerate((0.9, 0.2, 0.7, 0.5, 0.1)):
        a.post(bid(f"x{i}", s=s))
    f = a.cycle("turn")
    assert [i["ref"] for i in f.items] == ["x0", "x2", "x3"] and got and got[0] is f


def test_a2_fixation_guard():
    c = Clock()
    a = Awareness(clock=c)
    a.post(bid("heavy", "edim", 1.0))
    for r in ("b1", "b2", "b3"):
        a.post(bid(r, "metacog", 0.4))
    runs = [("heavy" in [i["ref"] for i in a.cycle().items]) for _ in range(12)]
    longest = max(len(s) for s in "".join("1" if r else "0" for r in runs).split("0"))
    assert longest <= 5 and runs.count(False) >= 2


def test_quiet_source_gets_a_slot():
    c = Clock()
    a = Awareness(clock=c)
    for r, s in (("e1", 0.9), ("e2", 0.85), ("e3", 0.8), ("e4", 0.75)):
        a.post(bid(r, "edim", s))
    a.post(bid("dream:1", "dream", 0.2))
    seen = [r for _ in range(15) for r in a.cycle().focus]
    assert "dream:1" in seen


def test_a3_late_tower_bids_expire():
    c = Clock()
    a = Awareness(clock=c)
    a.post(bid("dream:late", "dream", 0.9, life=30))
    assert "dream:late" in a.cycle().focus
    c.t += 31
    f = a.cycle("tower_down")
    assert "dream:late" not in f.focus and "dream:late" in f.faded
    a.post(Bid("dream:stale", "dream", 0.9, 0.9, "test", c.t - 100, c.t - 1))   # already expired: ignored
    assert "dream:stale" not in a.bids


def test_a1_topic_return_boost_from_edim_bids():
    c = Clock()
    d, a = Dialogue(clock=c), Awareness(clock=c)
    first = d.observe_user("My computer is overheating.", "owner")
    d.observe_reply("When does that happen?")
    d.observe_user("By the way, what time is it?", "owner")
    a.post_many(d.bids(c.t))
    f = a.cycle("turn")
    assert f"thread:{first.thread}" in f.focus       # suspended thread with an open question stays in mind
    back = d.observe_user("It happens mostly when gaming.", "owner")
    assert back.thread == first.thread and back.plan["action"] == "RESUME_THREAD"


def test_a7_cycle_is_cheap():
    c = Clock()
    a = Awareness(clock=c)
    for i in range(64):
        a.post(bid(f"r{i}", f"s{i % 6}", (i % 10) / 10))
    worst = max(a.cycle().ms for _ in range(200))
    assert worst < 1.0
