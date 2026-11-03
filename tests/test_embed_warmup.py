"""Stage 3a: a failed meaning-search query loads the model in the background (no stuck aborted-load loop)."""
import threading
import time

from memory.embed import Embedder


def test_failed_query_warms_in_background_with_long_wait():
    e = Embedder("http://127.0.0.1:9", timeout=30.0)
    calls, done = [], threading.Event()

    def fake(texts, timeout=None):
        calls.append(timeout)
        if timeout == 30.0:
            done.set()
            return [[0.0]]
        return None   # the quick 1 s query fails (model still loading)
    e._embed = fake
    t = time.perf_counter()
    assert e.query("hello") is None
    assert time.perf_counter() - t < 0.5            # the reply isn't held up
    assert done.wait(2) and calls[:2] == [1.0, 30.0] and e.warmups == 1


def test_one_warmup_at_a_time():
    e = Embedder("http://127.0.0.1:9")
    gate, started = threading.Event(), []

    def fake(texts, timeout=None):
        if timeout != 1.0:
            started.append(1)
            gate.wait(2)
        return None
    e._embed = fake
    for _ in range(5):
        e.query("x")
    time.sleep(0.1)
    assert len(started) == 1
    gate.set()


def test_success_does_not_warm():
    e = Embedder("http://127.0.0.1:9")
    e._embed = lambda texts, timeout=None: [[1.0, 2.0]]
    assert e.query("x") == [1.0, 2.0] and e.warmups == 0
