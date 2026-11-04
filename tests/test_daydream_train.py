"""Daydream steps 5–6: training examples (Claude's wording left out), the safety gate, switch and undo."""
import json

import pytest
from fastapi.testclient import TestClient

from core import daydream, daydream_train as tr
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore
from tests.test_daydream import on, until

CLAUDE_DRAFT = "Set aside two hundred and fifty dollars every month and you will reach the goal by next summer easily."


def res(answer, vote_issues=(), revised=False):
    r = daydream.Result(task="How do we save $3,000?", answer=answer, ok=True, parts=["p"], id="dd-" + str(abs(hash(answer)) % 10**8))
    r.drafts = {"claude": [CLAUDE_DRAFT], "tower": ["tower text"]}
    r.final_issues = list(vote_issues)
    r.revised = revised
    return r


def card(personality=0.95, accuracy=None, vote=None):
    return {"modes": {"daydream": {"personality": personality, "accuracy": accuracy}}, "vote": vote}


# ---- Claude's wording never becomes training data ----
def test_overlap_counts_shared_8_word_runs():
    assert tr.claude_overlap("I'd say: set aside two hundred and fifty dollars every month and you will be fine.",
                             [CLAUDE_DRAFT]) > 0
    assert tr.claude_overlap("I'd put $250 aside each month; my guess is we'll be there by summer.", [CLAUDE_DRAFT]) == 0


def test_copying_claude_is_left_out_even_with_thumbs_up(tmp_path):
    ex = tr.Examples(tmp_path / "e.sqlite")
    r = res("Here's my plan: set aside two hundred and fifty dollars every month and you will reach the goal.")
    row = ex.consider(r, card(vote="up"), 0.8)
    assert not row["include"] and "Claude" in row["reason"]
    assert ex.counts() == {"total": 1, "included": 0, "claude_left_out": 1, "thumbs_up": 1}


@pytest.mark.parametrize("kw,include,why", [
    ({}, True, "high score"),
    ({"personality": 0.5}, False, "personality"),
    ({"accuracy": 0.5}, False, "accuracy"),
    ({"vote": "down"}, False, "👎"),
    ({"vote": "up", "personality": 0.2}, True, "👍")])
def test_selection_rules(tmp_path, kw, include, why):
    ex = tr.Examples(tmp_path / "e.sqlite")
    row = ex.consider(res("I'd put $250 aside each month; my guess is summer."), card(**kw), 0.8)
    assert row["include"] is include and why in row["reason"]


def test_left_over_mistake_blocks_unless_revised(tmp_path):
    ex = tr.Examples(tmp_path / "e.sqlite")
    bad = [{"kind": "mistake", "note": "maths"}]
    assert not ex.consider(res("I'd say eight months.", bad), card(), 0.8)["include"]
    assert ex.consider(res("I'd say twelve months.", bad, revised=True), card(), 0.8)["include"]


def test_vote_later_changes_the_decision(tmp_path):
    ex = tr.Examples(tmp_path / "e.sqlite")
    r = res("I'd put $250 aside; summer.")
    ex.consider(r, card(personality=0.4), 0.8)
    assert ex.counts()["included"] == 0
    assert ex.vote(r.id, "up", 0.8) and ex.counts()["included"] == 1
    assert not ex.vote("nope", "up", 0.8)


def test_export_chat_format(tmp_path):
    ex = tr.Examples(tmp_path / "e.sqlite")
    for i in range(12):
        ex.consider(res(f"I think answer number {i} is the one."), card(), 0.8)
    out = ex.export(tmp_path / "data", "I am Echo.")
    assert out["train"] + out["valid"] == 12 and out["valid"] == 1
    line = json.loads((tmp_path / "data" / "train.jsonl").read_text().splitlines()[0])
    assert [m["role"] for m in line["messages"]] == ["system", "user", "assistant"]
    assert line["messages"][0]["content"] == "I am Echo." and line["messages"][1]["content"] == "How do we save $3,000?"
    assert CLAUDE_DRAFT not in (tmp_path / "data" / "train.jsonl").read_text()


# ---- the safety gate ----
CURRENT = {"reasoning": 0.8, "daydream_set": 0.6, "identity": 1.0, "invention": 0.2, "personality": 0.9}


def test_gate_refuses_a_worse_model():
    ok, why = tr.gate(CURRENT, CURRENT | {"reasoning": 0.7})
    assert not ok and any("reasoning got worse" in w for w in why)


@pytest.mark.parametrize("change,word", [({"identity": 0.67}, "identity"), ({"invention": 0.4}, "invention"),
                                         ({"personality": 0.85}, "personality")])
def test_gate_refuses_better_reasoning_that_costs_identity_invention_or_personality(change, word):
    ok, why = tr.gate(CURRENT, CURRENT | {"reasoning": 0.95} | change)
    assert not ok and any(word in w for w in why)


def test_gate_refuses_no_improvement_and_missing_numbers():
    ok, why = tr.gate(CURRENT, dict(CURRENT))
    assert not ok and "not better" in why[-1]
    ok, why = tr.gate(CURRENT, CURRENT | {"reasoning": 0.95, "identity": None})
    assert not ok and any("not measured" in w for w in why)


def test_gate_accepts_a_better_model():
    ok, why = tr.gate(CURRENT, CURRENT | {"daydream_set": 0.75, "invention": 0.1})
    assert ok and why == ["daydream_set 0.600 → 0.750"]


def test_baseline_numbers_scores_invention():
    pub = {"summary": {"reasoning": {"accuracy": "7/10"}, "identity": {"accuracy": "3/3"}}}
    answers = {"results": [{"set": "unknowns", "a": "I don't remember that."},
                           {"set": "unknowns", "a": "It was Mrs. Smith."}, {"set": "simple", "a": "Hi"}]}
    assert tr.baseline_numbers(pub, answers) == {"reasoning": 0.7, "identity": 1.0, "invention": 0.5}


# ---- switch / undo ----
def test_switch_and_undo(tmp_path):
    cfgp = tmp_path / "echo.yaml"
    cfgp.write_text("llm:\n  backend: ollama\n  model: qwen3.5:9b-q4_K_M   # standard engine\n")
    v = tr.Versions(cfgp, tmp_path / "models.json")
    assert v.undo() is None                                            # nothing to undo yet
    v.switch("echo-lora-1", ["reasoning 0.8 → 0.9"])
    assert v.current() == "echo-lora-1" and "# standard engine" in cfgp.read_text()
    assert v.undo() == {"from": "echo-lora-1", "to": "qwen3.5:9b-q4_K_M"}
    assert v.current() == "qwen3.5:9b-q4_K_M"
    log = [json.loads(x) for x in tr.LOG.read_text().splitlines()]
    assert [e["event"] for e in log] == ["switched", "undo"]


@pytest.mark.parametrize("text", ["undo training", "Echo, undo the last training.", "please undo my training"])
def test_undo_words(text):
    assert daydream.is_undo_training(text)


def test_socket_undo_without_history(tmp_path, monkeypatch):
    on(tmp_path, monkeypatch)
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "undo training"})
        assert "no training to undo" in until(ws, "reply")["text"]


def test_socket_undo_switches_back_and_restarts(tmp_path, monkeypatch):
    on(tmp_path, monkeypatch)
    restarted = []
    monkeypatch.setattr(tr, "restart_core", lambda *a, **k: restarted.append(1))
    monkeypatch.setattr(tr.Versions, "undo", lambda self: {"from": "echo-lora-1", "to": "qwen3.5:9b-q4_K_M"})
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "undo training"})
        assert "qwen3.5:9b-q4_K_M" in until(ws, "reply")["text"]
    assert restarted == [1]
