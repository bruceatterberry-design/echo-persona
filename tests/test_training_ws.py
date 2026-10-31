"""Train Echo through the real chat socket (no Claude call: stops at "are you sure?" → no)."""
from fastapi.testclient import TestClient

from core import browser
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore


def until(ws, kind):
    while True:
        m = ws.receive_json()
        if m["type"] == kind:
            return m


def test_testing_thumbs_and_train_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "run", lambda act: True)        # never open the real Chrome in a test
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        hist = ws.receive_json()
        assert hist["training"] == {"testing": False, "busy": False, "wrong": 0}
        ws.send_json({"text": "hello"})
        assert "fid" not in until(ws, "reply")                     # testing off: no thumbs
        ws.send_json({"text": "Use Claude to evaluate Echo"})
        assert "Testing is on" in until(ws, "reply")["text"]
        assert until(ws, "training")["testing"] is True
        ws.send_json({"text": "how are you"})
        assert until(ws, "reply")["fid"]                          # a normal reply gets 👍/👎
        ws.send_json({"text": "open youtube and play steely dan"})
        r = until(ws, "reply")
        assert r["fid"]                                            # so does a command
        ws.send_json({"type": "feedback", "fid": r["fid"], "vote": "down", "note": "it didn't open"})
        assert until(ws, "training")["wrong"] == 1
        ws.send_json({"text": "Train Echo"})
        ask = until(ws, "reply")
        assert "Are you sure?" in ask["text"] and until(ws, "choices")["options"] == ["Yes", "No"]
        ws.send_json({"text": "No"})
        assert "not now" in until(ws, "reply")["text"].lower()


def test_unrecognised_play_request_is_not_claimed_and_no_life_question(tmp_path, monkeypatch):
    from memory.curiosity import Curiosity
    asked = []
    monkeypatch.setattr(Curiosity, "maybe_ask", lambda self, s, t, now=None: asked.append(t))
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "Could you open the Spotify app and play some jazz"})
        r = until(ws, "reply")
        assert "Nothing was opened or played" in r["text"]          # EchoBackend echoes what the model was sent
        ws.send_json({"text": "Okay then"})
        until(ws, "reply")
    assert asked == []                                                 # quiet after a command-like request
