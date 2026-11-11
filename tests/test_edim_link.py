"""EDIM + awareness wired into the real reply path (Phase 3): flag matrix (A4), shadow adds nothing, failures never
block a reply, restart restores, records hold no words."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from core import dialogue as dlg
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore

TALK = ["My computer is overheating badly.", "By the way, what time is it?", "It happens mostly when gaming."]


def chat(client, text):
    out = []
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": text})
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            if m["type"] == "reply":
                out.append(m.get("text", ""))
    return " ".join(out)


def app(tmp_path, monkeypatch, edim="off", aw="off"):
    monkeypatch.setenv("ECHO_EDIM", edim)
    monkeypatch.setenv("ECHO_AWARENESS", aw)
    return TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))


def records(tmp_path):
    rows = sqlite3.connect(tmp_path / "workspace.sqlite").execute("SELECT record FROM turns ORDER BY id").fetchall()
    return [json.loads(r[0]) for r in rows]


@pytest.mark.parametrize("edim,aw", [("off", "off"), ("shadow", "off"), ("off", "shadow"), ("shadow", "shadow"),
                                     ("on", "off"), ("on", "on")])
def test_a4_flag_matrix(tmp_path, monkeypatch, edim, aw):
    client = app(tmp_path, monkeypatch, edim, aw)
    replies = [chat(client, t) for t in TALK]
    assert all(replies)                                           # Echo answers in every combination
    recs = records(tmp_path)
    assert len(recs) == 3
    state = json.loads((tmp_path / "state.json").read_text())
    if edim == "off" and aw == "off":
        assert all(r.get("dialogue") == {} for r in recs) and "dialogue" not in state
    if edim == "off":
        assert "dialogue" not in state and all("thread" not in r["dialogue"] for r in recs)
    else:
        assert state["dialogue"]["turn"] == 3 and recs[-1]["dialogue"]["thread"]
    if aw != "off":
        assert "focus" in recs[-1]["dialogue"]
    noted = any("Conversation note" in r for r in replies)
    assert noted == (edim == "on")                                 # only "on" adds anything the model reads
    if edim == "on":
        assert "dialogue_note" in recs[-1]["hints"] and recs[-1]["dialogue"]["plan"]["action"] == "RESUME_THREAD"
    stored = [r[0] for r in MemoryStore(tmp_path / "m.sqlite").db.execute("SELECT sent FROM turns WHERE role='user'")
              if r[0]]
    assert not any("Conversation note" in s for s in stored)       # never kept in the history


def test_note_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHO_EDIM_NOTES", "0")
    client = app(tmp_path, monkeypatch, "on")
    replies = [chat(client, t) for t in TALK]
    assert not any("Conversation note" in r for r in replies)
    assert records(tmp_path)[-1]["exec"]["modules"].get("dialogue") == "switched_off"


def test_edim_failure_never_blocks_a_reply(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("broken")
    monkeypatch.setattr(dlg.Dialogue, "observe_user", boom)
    monkeypatch.setattr(dlg.Dialogue, "observe_reply", boom)
    client = app(tmp_path, monkeypatch, "on", "on")
    assert all(chat(client, t) for t in TALK)
    rep = client.get("/api/dialogue").json()
    assert rep["errors"] >= 3


def test_corrupt_saved_state_starts_fresh(tmp_path, monkeypatch):
    (tmp_path / "state.json").write_text(json.dumps({"version": 1, "dialogue": {"v": 7, "threads": "junk"}}))
    client = app(tmp_path, monkeypatch, "shadow")
    assert chat(client, TALK[0])
    assert client.get("/api/dialogue").json()["dialogue"]["problems"] == ["reset: ValueError"]


def test_restart_restores_threads(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch, "shadow")
    for t in TALK[:2]:
        chat(client, t)
    client2 = app(tmp_path, monkeypatch, "shadow")                # a restarted core, same files
    snap = client2.get("/api/dialogue").json()["dialogue"]
    assert snap["turn"] == 2 and len(snap["threads"]) == 2
    chat(client2, TALK[2])
    assert records(tmp_path)[-1]["dialogue"]["plan"]["action"] == "RESUME_THREAD"


def test_records_hold_no_words(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch, "shadow", "shadow")
    chat(client, "I installed a new purple giraffe GPU.")
    chat(client, "It seems to run cooler.")
    raw = (tmp_path / "workspace.sqlite").read_bytes()
    assert b"giraffe" not in raw and b"cooler" not in raw


def test_off_is_inert(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    chat(client, TALK[0])
    rep = client.get("/api/dialogue").json()
    assert rep["edim"] == "off" and rep["awareness"] == "off" and rep["update_ms"]["n"] == 0


def test_settings_file_and_env_priority(tmp_path, monkeypatch):
    from core import edim_link
    f = tmp_path / "edim.json"
    monkeypatch.delenv("ECHO_EDIM", raising=False)
    assert edim_link.mode("ECHO_EDIM", f) == "off"                  # no file: off
    f.write_text('{"edim": "on", "awareness": "bogus"}')
    assert edim_link.mode("ECHO_EDIM", f) == "on"
    assert edim_link.mode("ECHO_AWARENESS", f) == "off"             # unknown value: off
    monkeypatch.setenv("ECHO_EDIM", "off")
    assert edim_link.mode("ECHO_EDIM", f) == "off"                  # environment wins
    f.write_text("{broken")
    monkeypatch.delenv("ECHO_EDIM")
    assert edim_link.mode("ECHO_EDIM", f) == "off"                  # unreadable: off


@pytest.mark.parametrize("edim,expect", [("on", "ask_held"), ("off", "ask")])
def test_curiosity_question_held_when_upset(tmp_path, monkeypatch, edim, expect):
    monkeypatch.setenv("ECHO_EDIM", edim)
    monkeypatch.setenv("ECHO_AWARENESS", "off")
    from memory.curiosity import Curiosity
    monkeypatch.setattr(Curiosity, "maybe_ask", lambda self, spk, text, now=None: {"id": "q-test",
                                                                                   "text": "Where did you grow up?"})
    cfg = PersonaConfig()
    client = TestClient(create_app(cfg, EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "I'm really sad today.", "speaker": "owner"})
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            pass
    rec = records(tmp_path)[-1]
    assert expect in rec["hints"]
    if edim == "on":
        assert rec["dialogue"]["curiosity"] == "held:emotional_disclosure"
