"""CA 2.0 Phase 10: long-term evaluation — dormant until enough data, then a baseline, then ok / watch."""
import json
import time

from fastapi.testclient import TestClient

from core import longterm
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import Memory, MemoryStore

DAY = 86400


def recs(n, ms, days=4, t0=None):
    t0 = t0 or time.time() + 60
    return [{"route": "llm", "first_ms": ms, "started": t0 + (i % days) * DAY,
             "exec": {"modules": {"grounding": "fired" if i % 4 == 0 else "quiet", "social": "quiet"}}}
            for i in range(n)]


def make(tmp_path, settings=None):
    sp = tmp_path / "lt.json"
    sp.write_text(json.dumps(settings or {}))
    return longterm.LongTerm(tmp_path / "lt.sqlite", sp), MemoryStore(tmp_path / "m.sqlite")


def test_dormant_until_thresholds_then_baseline_then_ok_or_watch(tmp_path):
    lt, store = make(tmp_path)
    r = lt.run(recs(20, 900), {"lessons": []}, {"asked": ["q1"]}, store, now=time.time())
    assert r["first_word"]["state"] == "dormant" and r["first_word"]["data"]["replies"] == 20
    assert r["notes"]["state"] == "dormant" and r["questions"]["state"] == "dormant" and r["lessons"]["state"] == "dormant"
    assert r["memory_health"]["state"] == "baseline_set"                      # always on
    r = lt.run(recs(120, 900), {"lessons": []}, {}, store)
    assert r["first_word"]["state"] == "baseline_set" and r["notes"]["state"] == "baseline_set"
    r = lt.run(recs(120, 1000), {"lessons": []}, {}, store)
    assert r["first_word"]["state"] == "ok" and r["notes"]["state"] == "ok"
    r = lt.run(recs(120, 3000), {"lessons": []}, {}, store)                   # p90 900 → 3000 ms
    assert r["first_word"]["state"] == "watch" and "slow replies" in r["first_word"]["why"][0]
    assert lt.reset_baseline("first_word")
    assert lt.run(recs(120, 3000), {"lessons": []}, {}, store)["first_word"]["state"] == "baseline_set"


def test_too_few_days_stays_dormant(tmp_path):
    lt, store = make(tmp_path)
    assert lt.run(recs(200, 900, days=1), {"lessons": []}, {}, store)["first_word"]["state"] == "dormant"


def test_thresholds_are_settings_for_any_install(tmp_path):
    lt, store = make(tmp_path, {"min": {"first_word_replies": 5, "first_word_days": 1}, "every_hours": 1})
    assert lt.run(recs(6, 900, days=1), {"lessons": []}, {}, store)["first_word"]["state"] == "baseline_set"
    assert lt.settings()["min"]["notes_turns"] == 100                         # untouched defaults kept
    assert not lt.due() and lt.due(time.time() + 3601)
    (tmp_path / "off").mkdir()
    off, _ = make(tmp_path / "off", {"enabled": False})
    assert not off.due()


def test_only_data_after_install_counts(tmp_path):
    lt, store = make(tmp_path)
    old = recs(200, 20000, t0=time.time() - 10 * DAY)                      # before the evaluator: ignored
    r = lt.run(old, {"lessons": []}, {"asked": [f"q{i}" for i in range(13)]}, store)
    assert r["first_word"]["data"]["replies"] == 0 and r["questions"]["data"]["asked"] == 0
    r = lt.run(old + recs(60, 900), {"lessons": []}, {"asked": [f"q{i}" for i in range(13)] + ["photo:1"]}, store)
    assert r["first_word"]["value"]["p90_ms"] == 900 and r["questions"]["data"]["asked"] == 1


def test_questions_and_lessons(tmp_path):
    lt, store = make(tmp_path)
    lt.start({}, 0)
    st = {"asked": [f"q{i}" for i in range(6)] + [f"photo:{i}" for i in range(6)], "answered_ids": ["q1"]}
    rep = {"lessons": [{"id": 2, "active": True, "measured": [{}], "verdict": "worse_since"},
                       {"id": 3, "active": True, "measured": [{}], "verdict": "not_enough_data"}]}
    r = lt.run([], rep, st, store)
    assert r["questions"]["state"] == "baseline_set" and r["lessons"]["value"] == {"2": "worse_since"}
    r = lt.run([], rep, st, store)
    assert r["questions"]["state"] == "watch" and "unanswered" in r["questions"]["why"][0]
    assert r["lessons"]["state"] == "watch" and "2" in r["lessons"]["why"][0]


def test_memory_health_watches_integrity_and_losses(tmp_path):
    lt, store = make(tmp_path)
    for i in range(20):
        store.add(Memory(content=f"Memory number {i} about the garden.", source="t"))
    lt.run([], {"lessons": []}, {}, store)
    ids = [r[0] for r in store.db.execute("SELECT id FROM memories LIMIT 5")]
    with store.db:
        store.db.execute(f"UPDATE memories SET hidden=1 WHERE id IN ({','.join(map(str, ids))})")
    r = lt.run([], {"lessons": []}, {}, store)
    assert r["memory_health"]["state"] == "watch" and "fell from 20 to 15" in r["memory_health"]["why"][0]


def test_numbers_only_and_endpoint(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite")
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), store))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "My secret garden plan is to plant tulips."})
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            pass
    r = client.post("/api/longterm/run").json()
    assert r["first_word"]["state"] == "dormant"
    s = client.get("/api/longterm").json()
    assert s["data_leaves_this_computer"] is False and s["last_run"]["memory_health"]["state"] == "baseline_set"
    assert b"tulips" not in (tmp_path / "longterm.sqlite").read_bytes()
