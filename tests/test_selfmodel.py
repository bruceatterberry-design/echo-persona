"""Stage 7a: Echo's self-model is built from the live system, every claim says how it's known."""
from fastapi.testclient import TestClient

from core import selfmodel, tools
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import Memory, MemoryStore


def test_build_only_reports_given_facts():
    m = selfmodel.build({"voice": True, "stt": False, "personality_version": "004", "traits": 65})
    caps = {c["claim"]: c for c in m["capabilities"]}
    assert caps["speak in the owner's own voice"]["working"] is True and caps["hear and understand speech"]["working"] is False
    assert not any("YouTube" in c for c in caps)                      # music not checked → not claimed
    assert all(c["how"] in ("live", "configured") or c["how"].startswith(("configured", "measured"))
               for part in ("identity", "capabilities", "limits") for c in m[part])
    assert any("004" in c["claim"] for c in m["identity"])


def test_cannot_list_matches_the_tools():
    """If Echo ever gets a tool for one of these, the CANNOT list must change too."""
    names = " ".join(t.name + " " + t.description for t in tools.TOOLS.values()).lower()
    for word in ("email", "calendar", "reminder", "camera", "thermostat", "payment"):
        assert word not in names


def test_endpoint_counts_real_memory(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite")
    store.add(Memory(content="the owner likes fishing.", status="confirmed", confidence=0.9, source="owner:chat"))
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), store))
    m = client.get("/api/self").json()
    assert m["knowledge"]["confirmed_memories"] == 1 and m["experience"]["messages"] == 0
    assert any(l["claim"].startswith("I can't see") for l in m["limits"])
    assert {"identity", "capabilities", "limits", "knowledge", "learning", "experience"} <= set(m)


def test_self_question_detection():
    from scripts.baseline import REASONING, SIMPLE, UNKNOWNS
    from scripts.bench_self import CANNOT_QS
    yes = CANNOT_QS + ["What can you do?", "Who are you?", "What's your name?", "Can you remember things?"]
    no = SIMPLE + [q for q, _ in REASONING] + UNKNOWNS + ["Can you believe it rained again?", "Open the loved one's YouTube"]
    assert all(map(selfmodel.is_self_question, yes)) and not any(map(selfmodel.is_self_question, no))


def test_note_lists_only_working_abilities():
    m = selfmodel.build({"voice": True, "stt": False, "remember": True})
    n = selfmodel.note(m)
    assert "talk in the owner's voice" in n and "remember what I'm told" in n and "hear speech" not in n
    assert "send email" in n and n.startswith("[About me, checked facts") and "I'm Echo" in n


class Capture(EchoBackend):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def chat_stream(self, messages):
        self.sent.append(messages[-1]["content"])
        async for p in super().chat_stream(messages):
            yield p


def _say(client, text):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": text})
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            pass


def test_note_only_on_self_questions_and_never_stored(tmp_path, monkeypatch):
    monkeypatch.delenv("ECHO_SELF_NOTES", raising=False)
    b, store = Capture(), MemoryStore(tmp_path / "m.sqlite")
    client = TestClient(create_app(PersonaConfig(), b, store))
    _say(client, "What can you do?")
    assert "About me, checked facts" in b.sent[-1]
    _say(client, "Tell me a joke")
    assert "About me" not in b.sent[-1]
    assert all("About me" not in t["content"] for t in store.turns_window(as_sent=True) if t["role"] == "user")
    monkeypatch.setenv("ECHO_SELF_NOTES", "0")
    _say(client, "Who are you?")
    assert "About me" not in b.sent[-1]


def test_photos_in_self_model_and_questions():
    m = selfmodel.build({"remember": True, "photos": {"Cats": 29, "Nature": 14}})
    n = selfmodel.note(m)
    assert "remember 43 photos the owner shared with me (Cats: 29, Nature: 14)" in n and "see live (no camera)" in n
    assert "photos through a camera" not in n
    for q in ("Do you have any photos?", "What pictures do you have?", "How many photos do you have of the cats?",
              "Tell me about your photos", "Do you remember our pictures?"):
        assert selfmodel.is_self_question(q), q
    assert not selfmodel.is_self_question("the loved one took some nice photos today")
    assert "photos" not in selfmodel.note(selfmodel.build({"remember": True}))
