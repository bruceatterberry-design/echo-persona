"""Google Photos picker → face staging → scan; faces stay pending until the owner approves. Google is faked."""
import contextlib
import io
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

import face.gphotos as gp
from tests.test_face import fl, img  # noqa: F401  (fixture + helper)


def jpeg(color):
    b = io.BytesIO()
    Image.new("RGB", (300, 300), color).save(b, "JPEG")
    return b.getvalue()


class FakeResp:
    def __init__(self, data=None, status=200, body=b""):
        self.data, self.status_code, self.body = data, status, body

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=None, response=None)

    def iter_bytes(self):
        yield self.body


class FakeGoogle:
    HTTPError = httpx.HTTPError

    def __init__(self):
        self.set, self.deleted, self.downloads = False, [], []
        self.items = [
            {"id": "AAA111", "type": "PHOTO", "mediaFile": {"baseUrl": "https://x/a", "mimeType": "image/jpeg", "filename": "me 1985.jpg"}},
            {"id": "BBB222", "type": "VIDEO", "mediaFile": {"baseUrl": "https://x/v", "mimeType": "video/mp4", "filename": "v.mp4"}},
            {"id": "CCC333", "type": "PHOTO", "mediaFile": {"baseUrl": "https://x/c", "mimeType": "image/jpeg", "filename": "../evil.jpg"}}]

    def post(self, url, **k):
        assert url.endswith("/sessions") and k["json"]["pickingConfig"]["maxItemCount"] == "500"
        return FakeResp({"id": "sess1", "pickerUri": "https://photos.google.com/picker/sess1"})

    def get(self, url, **k):
        if url.endswith("/sessions/sess1"):
            return FakeResp({"mediaItemsSet": self.set})
        if url.endswith("/mediaItems"):
            assert k["params"]["sessionId"] == "sess1"
            return FakeResp({"mediaItems": self.items})
        return FakeResp(status=404)

    def delete(self, url, **k):
        self.deleted.append(url)
        return FakeResp({})

    @contextlib.contextmanager
    def stream(self, method, url, **k):
        assert url.endswith("=d") and k["headers"]["Authorization"] == "Bearer t"
        self.downloads.append(url)
        yield FakeResp(body=jpeg((200, 150, 120) if "/a" in url else (90, 120, 200)))


@pytest.fixture
def g(fl, tmp_path, monkeypatch):  # noqa: F811
    fake = FakeGoogle()
    monkeypatch.setattr(gp, "httpx", fake)
    photos = gp.GooglePhotos(tmp_path / "client.json", tmp_path / "tok", "http://127.0.0.1/cb", fl)
    monkeypatch.setattr(photos, "_h", lambda email: {"Authorization": "Bearer t"})
    monkeypatch.setattr(photos, "accounts", lambda: ["me@x.com"])
    return photos, fake, fl


def wait(photos, sid):
    for _ in range(200):
        st = photos.status(sid)
        if st["state"] in ("done", "error"):
            return st
        time.sleep(0.05)
    raise AssertionError("import never finished")


def test_pick_younger_imports_photos_pending_review(g):
    photos, fake, fl = g  # noqa: F811
    s = photos.start("me@x.com", "younger", "20s")
    assert s == {"id": "sess1", "picker_uri": "https://photos.google.com/picker/sess1/autoclose"}
    assert photos.status("sess1")["state"] == "waiting"      # the owner hasn't pressed Done yet
    fake.set = True
    st = wait(photos, "sess1")
    assert st["error"] is None and st["state"] == "done"
    assert (st["done"], st["skipped"], st["total"]) == (2, 1, 3)   # the video is skipped
    files = sorted(p.name for p in (fl.staging / "photos-younger/20s/google").iterdir())
    assert files == ["evil-CCC333.jpg", "me_1985-AAA111.jpg"]     # no path tricks from Google's file names
    assert st["result"]["new_photos"] == 2 and fake.deleted == ["https://photospicker.googleapis.com/v1/sessions/sess1"]
    lib = fl.summary()
    assert {p["group"] for p in lib["photos"]} == {"younger"} and {p["age_label"] for p in lib["photos"]} == {"20s"}
    # approval gate: nothing is in the library (raw/face) until the owner says "This is me"
    assert lib["counts"]["approved_younger"] == 0 and lib["counts"]["pending"] == 2
    assert not fl.raw.exists()


def test_pick_again_skips_duplicates(g):
    photos, fake, fl = g  # noqa: F811
    fake.set = True
    photos.start("me@x.com", "current")
    assert wait(photos, "sess1")["result"]["new_photos"] == 2
    photos.start("me@x.com", "current")
    st = wait(photos, "sess1")
    assert st["result"]["new_photos"] == 0 and len(fake.downloads) == 2   # already-downloaded files aren't fetched again


def test_rejects_unknown_account_and_bad_age(g):
    photos, _, _ = g
    with pytest.raises(RuntimeError, match="connect"):
        photos.start("other@x.com", "current")
    with pytest.raises(ValueError):
        photos.start("me@x.com", "younger", "../../x")


def test_api_not_set_up(fl, tmp_path):  # noqa: F811
    from fastapi.testclient import TestClient
    from core.config import PersonaConfig
    from core.llm import EchoBackend
    from core.server import create_app
    from memory.store import MemoryStore
    from voice.stt import FakeSTT
    c = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite"), FakeSTT(), None, fl))
    assert set(c.get("/api/face/google").json()) == {"set_up", "accounts"}
    assert c.post("/api/face/google/pick", json={"email": "nobody@x.com", "group": "current"}).status_code == 400
    assert c.post("/api/face/google/pick", json={"email": "a", "group": "evil"}).status_code == 422
    assert c.get("/api/face/google/pick/nope").status_code == 404
    assert "Google Photos" in c.get("/face.html").text
