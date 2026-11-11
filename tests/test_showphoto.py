"""Showing a photo I remember (2026-11-03): the requests, my offers, and which photo is picked."""
import sqlite3
from types import SimpleNamespace

from core import showphoto as sp


def test_asks_to_show():
    for t in ["Can you show me the picture?", "Show me that photo of Anna.", "Can I see it?", "Let me see that one, please.",
              "I'd love to see it.", "Open the photo gallery.", "Could we look at the pictures from 2017?", "show it"]:
        assert sp.asks_to_show(t), t
    for t in ["Let me see, that was in 1990.", "Show me that you love me", "I took a picture today.",
              "Do you want to talk about something today?", "Close the picture.", "Yes.", "I'd like to see Anna again."]:
        assert not sp.asks_to_show(t), t


def test_offer_from_the_real_conversation():
    assert sp.offered("I'd love to show you that picture, the owner; would you like me to open your photo gallery now so we can take a look?")
    assert sp.offered("Would you like to see it?")
    assert not sp.offered("Fixing that trellis must have felt great.")


def test_close():
    assert sp.asks_to_close("Close the picture")
    assert sp.asks_to_close("put it away", showing=True)
    assert not sp.asks_to_close("put it away")        # nothing on screen: not about a photo
    assert not sp.asks_to_close("That's a lovely photo")


class Store:
    def __init__(self, rows):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, source TEXT, hidden INTEGER)")
        self.db.executemany("INSERT INTO memories (content, source, hidden) VALUES (?, ?, ?)", rows)


FAMILY, CHAIR, GONE = "a" * 16, "b" * 16, "c" * 16
STORE = Store([("The owner is holding their daughters Anna and Emma. Anna wears a blue dress.", f"photo:{FAMILY}", None),
               ("A man sits in a blue office chair.", f"photo:{CHAIR}", None),
               ("the loved one by the bridge.", f"photo:{GONE}", 1),            # removed: never shown
               ("the owner likes Steely Dan.", "owner:chat", None)])


def test_pick():
    assert sp.pick("Can you show me the picture?", STORE, CHAIR) == CHAIR          # the one we were talking about
    assert sp.pick("Show me the picture of Anna and Emma", STORE, CHAIR) == FAMILY  # the words name another
    assert sp.pick("Yes.", STORE, None) is None                                    # don't know which: I ask
    assert sp.pick("Show me the bridge photo", STORE, GONE) is None                # hidden photos never come back
    assert sp.pick("show me the blue one", STORE, FAMILY) == FAMILY                    # tie (dress / chair): keep the last
    assert sp.pick("show me the blue one", STORE, None) is None                    # tie, nothing talked about: ask
    assert sp.pick("Show me the picture of Daisy", STORE, FAMILY) is None             # no photo of her: don't show another


def test_top_photo_and_sha():
    hits = [SimpleNamespace(memory=SimpleNamespace(source="owner:chat")),
            SimpleNamespace(memory=SimpleNamespace(source=f"photo:{FAMILY}"))]
    assert sp.top_photo(hits) == FAMILY
    assert sp.sha_of("photo:../../etc") is None


# ---- end to end through the chat socket (the 2026-11-03 bug: "Yes" kept getting "open your photo gallery?") ----
from fastapi.testclient import TestClient  # noqa: E402

from core.config import PersonaConfig  # noqa: E402
from core.llm import EchoBackend  # noqa: E402
from core.server import create_app  # noqa: E402
from memory.photos import PhotoLibrary  # noqa: E402
from memory.store import Memory, MemoryStore  # noqa: E402


def _until(ws, kind):
    m = ws.receive_json()
    seen = [m]
    while m["type"] != kind:
        m = ws.receive_json()
        seen.append(m)
    return seen


def test_show_and_close_through_chat(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite")
    store.add(Memory(kind="memory", content="The owner is holding their daughters Anna and Emma.", source=f"photo:{FAMILY}",
                     told_by="owner"))
    root = tmp_path / "ds"
    (root / "derived" / "photos" / "view-v1").mkdir(parents=True)
    (root / "derived" / "photos" / "view-v1" / f"{FAMILY}.jpg").write_bytes(b"\xff\xd8jpg")
    store.add_turn("assistant", "Would you like me to open your photo gallery now so we can take a look?")
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), store, photos=PhotoLibrary(root, store)))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "Yes."})   # no photo talked about yet: I ask which one instead of offering again
        assert _until(ws, "reply")[-1]["text"].startswith("Which picture")
        ws.send_json({"text": "Can you show me the picture of Anna and Emma?"})
        seen = _until(ws, "reply")
        assert {"type": "photo", "url": f"/api/photos/view/{FAMILY}.jpg"} in seen and seen[-1]["text"] == "Here it is."
        assert client.get(f"/api/photos/view/{FAMILY}.jpg").content == b"\xff\xd8jpg"
        ws.send_json({"text": "Okay, put it away."})
        seen = _until(ws, "reply")
        assert {"type": "photo_close"} in seen and seen[-1]["text"] == "Okay, I've put it away."


# ---- 2026-11-05: photos I talk about go on screen, and close when the talk moves on ----
SHIRT = "This photo shows a man in a green jacket, smiling in a kitchen. Taken in October 2026."


def test_my_photo_question_shows_it():   # the owner's real conversation, 11-05 12:14
    q = "I have a photo from October 2026 of a man in a green jacket. Is that you?"
    assert sp.reply_shows(q, SHIRT, asked=False)
    assert sp.reply_shows("Is that you, the owner?", SHIRT, asked=True)   # my photo question: always shown
    assert not sp.reply_shows("I love that blue shirt idea.", SHIRT, asked=False)    # no photo talk
    assert not sp.reply_shows("I have a photo of Daisy on the couch.", SHIRT, asked=False)   # a different photo
    assert not sp.reply_shows("Is that you?", "", asked=True)        # no description: nothing to show


def test_stays_open_while_we_talk_about_it():
    asked = "I have a photo from October 2026 of a man in a green jacket. Is that you?"
    assert not sp.moved_on("Yes, it was me, but I don't know what was happening that day.", SHIRT, asked, True)
    assert not sp.moved_on("That kitchen was in our old house.", SHIRT, "Thanks, the owner.", False)
    assert not sp.moved_on("I like that picture.", SHIRT, "Okay.", False)
    assert not sp.moved_on("No problem.", SHIRT, "Yes, that photo shows you, the owner.", False)   # short, right after it


def test_closes_on_a_new_topic():
    assert sp.moved_on("I would like to make an app for you, Echo, so that you can see the world.", SHIRT,
                       "Yes, that photo shows you, the owner.", False)
    assert sp.moved_on("Yes, it would.", SHIRT, "Maybe we can find a sticker later.", False)
    assert sp.moved_on("What's the weather like tomorrow?", SHIRT, "", False)


def test_description():
    assert "Anna" in sp.description(STORE, FAMILY)
    assert sp.description(STORE, GONE) == "" and sp.description(STORE, None) == ""
