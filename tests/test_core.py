import pytest
from fastapi.testclient import TestClient

from core.config import PersonaConfig, load_config
from core.llm import EchoBackend, make_backend
from core.schema import parse_reply
from core.server import create_app
from memory.store import MemoryStore


def test_parse_valid_reply():
    r = parse_reply('{"text": "Hello.", "expression": "smiling", "intensity": 0.3}')
    assert (r.text, r.expression, r.intensity) == ("Hello.", "smiling", 0.3)


def test_parse_clamps_and_sanitizes():
    r = parse_reply('{"text": "Hi", "expression": "furious", "intensity": 7}')
    assert r.expression == "neutral" and r.intensity == 1.0


def test_parse_malformed_falls_back_to_text():
    r = parse_reply("just some words")
    assert r.text == "just some words" and r.expression == "neutral"


def test_parse_empty():
    assert parse_reply("").text


def test_default_config_is_local_only(tmp_path):
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg.server.host == "127.0.0.1"
    assert cfg.llm.host.startswith("http://127.0.0.1")


def test_unknown_backend_rejected():
    import pytest
    cfg = PersonaConfig()
    cfg.llm.backend = "cloud"
    with pytest.raises(ValueError):
        make_backend(cfg.llm)


def test_health_and_ui(tmp_path):
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / 'm.sqlite')))
    assert client.get("/health").json()["llm"]["ok"] is True
    assert "ECHO" in client.get("/").text


def test_websocket_chat_roundtrip(tmp_path):
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / 'm.sqlite')))
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "history"
        ws.send_json({"text": "hello"})
        assert ws.receive_json() == {"type": "state", "state": "thinking"}
        reply = ws.receive_json()
        while reply["type"] == "delta":
            reply = ws.receive_json()
        assert reply["type"] == "reply" and "hello" in reply["text"]
        assert ws.receive_json()["state"] == "idle"


class BrokenBackend(EchoBackend):
    async def chat(self, messages):
        raise ConnectionError("down")


def test_backend_failure_keeps_session_alive(tmp_path):
    client = TestClient(create_app(PersonaConfig(), BrokenBackend(), MemoryStore(tmp_path / 'm.sqlite')))
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "history"
        ws.send_json({"text": "hi"})
        ws.receive_json()  # thinking
        assert ws.receive_json()["type"] == "error"
        assert ws.receive_json()["state"] == "idle"


from core.schema import StreamParser


def feed_all(pieces):
    p = StreamParser()
    spoken = "".join(p.feed(x) for x in pieces)
    return spoken, p.finish()


def test_stream_parser_tag_split_across_pieces():
    spoken, r = feed_all(["[smi", "ling]", " Good mor", "ning, the loved one."])
    assert spoken.strip() == "Good morning, the loved one." and r.expression == "smiling" and r.text == "Good morning, the loved one."


def test_stream_parser_missing_or_bad_tag():
    assert feed_all(["Hello there."])[1].expression == "neutral"
    spoken, r = feed_all(["[furious] ", "Calm down."])
    assert r.expression == "neutral" and spoken.strip() == "Calm down."
    assert feed_all(["[a very long bracket that never closes and keeps going"])[1].text


def test_stream_parser_empty():
    assert feed_all([])[1].text  # fallback sentence, never empty


def test_websocket_streams_deltas(tmp_path):
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / 'm.sqlite')))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "hello there"})
        msgs = []
        while True:
            m = ws.receive_json(); msgs.append(m)
            if m["type"] == "reply":
                break
        deltas = "".join(m["text"] for m in msgs if m["type"] == "delta")
        assert deltas.strip() == m["text"] == "(echo) hello there" and m["expression"] == "attentive"


def test_new_window_tells_older_window_to_stand_down(tmp_path):
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / 'm.sqlite')))
    with client.websocket_connect("/ws") as first:
        first.receive_json()  # history
        with client.websocket_connect("/ws") as second:
            second.receive_json()
            assert first.receive_json() == {"type": "stand_down"}


@pytest.mark.parametrize("pieces,spoken,expr", [
    (["[neutral] Well, ", "*smiles warmly* it's good ", "to see you."], "Well, it's good to see you.", "smiling"),
    (["[smiles] Good morning, sweetheart."], "Good morning, sweetheart.", "smiling"),
    (["[smiles warmly] Hi there."], "Hi there.", "smiling"),
    (["That is *really* important."], "That is really important.", "neutral"),
    (["Let me think. *tries to look", " as if he's thinking* Okay."], "Let me think. Okay.", "thoughtful"),
    (["[attentive] I hear you. [sighs] ", "That's hard."], "I hear you. That's hard.", "concerned"),
    (["*nods* Go on."], "Go on.", "attentive"),
    (["Two * three = six"], "Two * three = six", "neutral"),     # a lone asterisk is just text
])
def test_stage_directions_become_expressions(pieces, spoken, expr):
    p = StreamParser()
    out = "".join(p.feed(x) for x in pieces)
    r = p.finish()
    out += p.leftover
    assert " ".join(out.split()) == spoken and r.text == spoken and r.expression == expr


@pytest.mark.parametrize("pieces,expected", [
    (["Goodnight, Sam. Sweet dreams."], "Goodnight. Sweet dreams."),
    (["Don't worry, Sa", "m, I'm here to help."], "Don't worry, I'm here to help."),
    (["It's my pleasure, Sam", "."], "It's my pleasure."),
    (["Sam, that's a lovely idea."], "That's a lovely idea."),
    (["Okay. Sam, ", "let's check the fridge."], "Okay. Let's check the fridge."),
    (["I love Sam's garden."], "I love Sam's garden."),
    (["Sam and I went to the lake."], "Sam and I went to the lake."),
    (["We can ask Sam later."], "We can ask Sam later."),
])
def test_name_only_dropped_when_addressing(pieces, expected):
    p = StreamParser(allow_name=False, name="Sam")
    out = "".join(p.feed(x) for x in pieces)
    p.finish(); out += p.leftover
    assert " ".join(out.split()) == expected


def test_name_kept_when_allowed():
    p = StreamParser(allow_name=True, name="Sam")
    assert "".join(p.feed(x) for x in ["Goodnight, Sam."]) + (p.finish() and p.leftover) == "Goodnight, Sam."


def test_no_name_set_keeps_everything():
    p = StreamParser(allow_name=False)
    assert "".join(p.feed(x) for x in ["Goodnight, Sam."]) + (p.finish() and p.leftover) == "Goodnight, Sam."


def test_status_report_shape(tmp_path):
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / 'm.sqlite')))
    r = client.get("/api/status").json()
    assert {"attention", "services", "mac", "data", "backups", "speed"} <= set(r)
    assert any(s["label"] == "com.echopersona.core" for s in r["services"])
    assert "ECHO STATUS" in client.get("/status.html").text
