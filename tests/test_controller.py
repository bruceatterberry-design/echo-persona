"""Stage 2a controller (shadow mode): deterministic rule-based task classification, recorded but not acted on."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from core import controller
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore
from scripts.baseline import IDENTITY, REASONING, SIMPLE, UNKNOWNS

LABELLED = ([(q, "chat") for q in SIMPLE] + [(q, "reasoning") for q, _ in REASONING] + [(q, "chat") for q, _ in IDENTITY]
            + [(q, "memory") for q in UNKNOWNS] + [
    ("Open the loved one's YouTube", "command"), ("Play some Steely Dan", "command"),
    ("What's the weather in Springfield tomorrow?", "research"), ("Read https://example.com and summarize it", "research"),
    ("Look up the news about the election", "research"), ("Why do leaves change color in the fall?", "reasoning"),
    ("Should I fix the car or buy a new one?", "reasoning"), ("Do you remember our trip to Epcot?", "memory"),
    ("I love you", "chat"), ("Tell me a joke", "chat"), ("Open up about your day", "chat")])


@pytest.mark.parametrize("text,kind", LABELLED)
def test_labelled_questions(text, kind):
    assert controller.classify(text).kind == kind


def test_known_names_make_memory_questions():
    assert controller.classify("How is Tom doing these days?").kind == "chat"
    d = controller.classify("How is Tom doing these days?", frozenset({"tom"}))
    assert d.kind == "memory" and d.why == ["known_name"]


def test_deterministic_and_no_message_words():
    a, b = controller.classify("What's 15 percent of 80?"), controller.classify("What's 15 percent of 80?")
    assert a == b and a.budget_s == controller.THINK_BUDGET_S
    assert all(w in ("math", "quantity", "logic", "deliberate") for w in a.why)


def test_decision_recorded_in_shadow_mode(tmp_path):
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "How many minutes are in three and a half hours?"})
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            pass
    rec = json.loads(sqlite3.connect(tmp_path / "workspace.sqlite").execute("SELECT record FROM turns").fetchone()[0])
    assert rec["plan"]["kind"] == "reasoning" and rec["route"] == "llm" and rec["plan_agrees"] is True
    assert client.get("/api/workspace").json()["summary"]["controller_agrees"] == "1/1"


@pytest.mark.parametrize("text,kind", [
    ("the loved one said hi", "chat"), ("the owner is out fishing today", "chat"), ("I went to the store", "chat"),
    ("Remember when we went to Epcot?", "memory"), ("Do you remember Pat", "memory"),
    ("Start at https://en.wikipedia.org/wiki/Special_education and follow the links to the article about IDEA", "research"),
    ("What does https://example.com say?", "research"), ("Open https://apple.com", "command"),
])
def test_sorter_fixes(text, kind):
    """2026-11-01: statements that mention people aren't memory questions (they'd get "I don't remember");
    an address + reading words is research, not a command."""
    assert controller.classify(text).kind == kind


def test_known_name_only_in_questions():
    names = frozenset({"milo"})
    assert controller.classify("Milo knocked a glass off the table", names).kind == "chat"
    assert controller.classify("What does Milo like to eat?", names).kind == "memory"
