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
    spoken, r = feed_all(["[furious] ", "Calm down."])   # an unknown tag that maps to a feeling
    assert r.expression == "angry" and spoken.strip() == "Calm down."
    spoken, r = feed_all(["[banana] ", "Calm down."])
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
        assert deltas.strip() == m["text"] == "hello there" and m["expression"] == "attentive"


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
    (["(smiles) Hi there."], "Hi there.", "smiling"),
    (["Well (laughs) that's ", "true."], "Well that's true.", "joy"),
    (["(puzzled) Wait, what", "? (attentive)"], "Wait, what?", "attentive"),
    (["(suprised) Really?"], "Really?", "surprised"),         # common misspelling
    (["Oh! (", "surprised", ") No way."], "Oh! No way.", "surprised"),
    (["Grab the jar (the blue one) please."], "Grab the jar (the blue one) please.", "neutral"),
    (["It took (about 2 hours) to get there."], "It took (about 2 hours) to get there.", "neutral"),
    (["I'm happy :) today"], "I'm happy :) today", "neutral"),
    (["Hey ", "(smiles). I can be sad (sad)."], "Hey. I can be sad.", "sad"),
    (["(blurts eyes shut, then opens them again) Ah, there we go!"], "Ah, there we go!", "neutral"),
    (["Okay (leaning in close) tell me."], "Okay tell me.", "attentive"),
    (["It's on (this side of the house) now."], "It's on (this side of the house) now.", "neutral"),
    (["I can be sad (sad)", ". I can be ", "excited (excited", ")", "."], "I can be sad. I can be excited.", "joy"),
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


def test_say_test_route_streams_cues_to_window(tmp_path):
    app = create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / 'm.sqlite'))
    assert TestClient(app).post("/api/say", json={"text": "hi"}).status_code == 403    # not from this Mac
    async def from_this_mac(scope, receive, send):
        await app({**scope, "client": ("127.0.0.1", 50000)}, receive, send)
    client = TestClient(from_this_mac)
    assert client.post("/api/say", json={"text": "hi"}).status_code == 409              # no window open
    with client.websocket_connect("/ws") as w:
        w.receive_json()
        assert client.post("/api/say", json={"text": "I can be sad (sad). Hey (smiles)."}).status_code == 200
        deltas = []
        while (m := w.receive_json())["type"] != "reply":
            deltas.append(m)
    assert "".join(m["text"] for m in deltas) == "I can be sad. Hey."
    assert {m["expression"] for m in deltas} >= {"sad", "smiling"}


@pytest.mark.parametrize("ask,pieces,spoken,expr,blinks", [
    ("Can you blink for me?", ["[smiling] Sure (blinks). There", " you go!"], "Sure. There you go!", "smiling", 2),
    ("Can you blink for me?", ["[neutral] There you go!"], "There you go!", "neutral", 1),   # forgot the cue: still blinks
    ("Smile for me, Echo", ["[neutral] Okay, here it is."], "Okay, here it is.", "smiling", 0),
    ("How are you?", ["[smiling] Good (winks), thanks. *blinks slowly* You?"], "Good, thanks. You?", "smiling", 2),
    ("How are you?", ["[blinks] Fine."], "Fine.", "neutral", 1),
])
def test_blink_and_smile_on_request(ask, pieces, spoken, expr, blinks):
    p = StreamParser()
    p.ask(ask)
    out = "".join(p.feed(x) for x in pieces)
    r = p.finish()
    out += p.leftover
    assert " ".join(out.split()) == spoken and r.expression == expr and p.blinks == blinks


@pytest.mark.parametrize("cue,expr", [("(joy)", "joy"), ("(surprised)", "surprised"), ("(sad)", "sad"), ("(angry)", "angry"),
                                      ("(bewildered)", "bewildered"), ("(confused)", "bewildered"), ("(laughs)", "joy"),
                                      ("(frowns)", "concerned"), ("(smiles)", "smiling"), ("(thoughtful)", "thoughtful")])
def test_new_feelings(cue, expr):
    spoken, r = feed_all([f"[neutral] Well {cue}."])
    assert spoken.strip() == "Well." and r.expression == expr
