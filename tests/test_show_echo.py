"""Show Echo: pictures from the owner's tablet go to the model with that one message only, never into history or memory."""
import base64
import io
import json

from fastapi.testclient import TestClient
from PIL import Image

from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import camera_note, camera_pictures, create_app
from echolink.app import from_phone
from memory.store import MemoryStore


def jpeg(color=(200, 30, 30)) -> str:
    b = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(b, "JPEG")
    return base64.b64encode(b.getvalue()).decode()


class Seeing(EchoBackend):
    def __init__(self):
        super().__init__()
        self.seen = []

    async def chat_stream(self, messages):
        self.seen.append(json.loads(json.dumps(messages)))
        async for p in super().chat_stream(messages):
            yield p


def talk(c, msg):
    with c.websocket_connect("/ws?client=phone&user=owner") as ws:
        ws.receive_json()
        ws.send_json(msg)
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            pass


def test_pictures_checked():
    good = jpeg()
    assert camera_pictures({"images": [good], "camera": "front"}) == {"images": [good], "camera": "front"}
    assert camera_pictures({"images": [good] * 5})["images"] == [good] * 3          # 3 at most, newest kept
    assert camera_pictures({"images": [base64.b64encode(b"GIF89a" * 40).decode()]}) is None   # not a JPEG
    assert camera_pictures({"images": "nope"}) is None and camera_pictures({}) is None
    assert camera_pictures({"images": [good], "camera": "weird"})["camera"] == "back"
    n = camera_note({"images": [good], "camera": "back"}, "owner")
    assert n.startswith("[Camera: the owner is showing you what the owner is looking at") and n.endswith("]")


def test_pictures_reach_the_model_once_and_are_never_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHO_EDIM", "off")
    store, be = MemoryStore(tmp_path / "m.sqlite"), Seeing()
    c = TestClient(create_app(PersonaConfig(), be, store))
    pic = jpeg()
    talk(c, {"text": "What is this part?", "images": [pic], "camera": "back"})
    talk(c, {"text": "And what colour was it?"})
    first, second = be.seen[0][-1], be.seen[1]
    assert first["images"] == [pic] and "[Camera: the owner is showing you" in first["content"]
    assert all("images" not in m for m in second)                                  # not carried into the next turn
    assert not any("[Camera:" in (m.get("content") or "") for m in second[:-1])    # note stripped from history
    dump = "\n".join(str(tuple(r)) for r in store.db.execute("SELECT * FROM turns"))
    assert pic[:200] not in dump and "[Camera:" not in dump
    assert not any(pic[:200] in p.read_bytes().decode("latin-1") for p in tmp_path.rglob("*") if p.is_file())


def test_echolink_passes_pictures_but_limits_them():
    pic = jpeg()
    out = json.loads(from_phone(json.dumps({"text": "look", "images": [pic] * 4, "camera": "front"})))
    assert out["images"] == [pic] * 3 and out["camera"] == "front"
    assert "images" not in json.loads(from_phone(json.dumps({"text": "x", "images": ["a" * 2_000_001]})))
