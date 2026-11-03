"""Photo collections: scan → Echo describes → the owner captions + approves → memory. Rejected/pending never learned."""
import stat

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.photos import PhotoError, PhotoLibrary
from memory.store import MemoryStore
from voice.stt import FakeSTT


def photo(path, color, size=(640, 480), orientation=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.full((*size[::-1], 3), color, np.uint8)
    im = Image.fromarray(arr)
    exif = im.getexif()
    if orientation:
        exif[0x0112] = orientation
    im.save(path, "JPEG", exif=exif)


@pytest.fixture
def lib(tmp_path):
    seen = []

    def describer(image, label):
        seen.append((image.name, label))
        return f"A grey cat on a sofa ({label})."
    root = tmp_path / "ds"
    root.mkdir()
    lib = PhotoLibrary(root, MemoryStore(tmp_path / "m.sqlite"), describer)
    lib.seen = seen
    return lib


def test_default_categories_scan_and_dedup(lib):
    s = lib.summary()
    assert [c["id"] for c in s["categories"]] == ["cats", "places", "nature", "home"]
    photo(lib.staging / "cats/milo.jpg", (120, 120, 120))
    photo(lib.staging / "cats/google/milo-copy.jpg", (120, 120, 120))   # same bytes → duplicate
    photo(lib.staging / "home/porch.jpg", (10, 90, 30))
    photo(lib.staging / "unknown-cat/x.jpg", (1, 2, 3))                 # not a category → ignored
    r = lib.scan()
    assert (r["new_photos"], r["duplicates"]) == (2, 1)
    cats = {p["category"] for p in lib.summary()["photos"]}
    assert cats == {"cats", "home"} and lib.scan()["new_photos"] == 0


def test_rotation_applied_for_echo(lib):
    photo(lib.staging / "nature/tall.jpg", (50, 50, 50), size=(640, 480), orientation=6)   # camera said "rotate 90°"
    lib.scan()
    p = lib.summary()["photos"][0]
    with Image.open(lib.view / f"{p['sha256'][:16]}.jpg") as im:
        assert im.size[1] > im.size[0]


def test_nothing_learned_until_approved(lib):
    photo(lib.staging / "cats/daisy.jpg", (200, 200, 200))
    lib.scan()
    sha = lib.summary()["photos"][0]["sha256"]
    with pytest.raises(PhotoError, match="caption"):
        lib.review(sha, status="approved")            # nothing to learn yet
    assert "grey cat" in lib.describe(sha) and lib.seen[0][1] == "Cats"
    lib.review(sha, caption="Daisy on the back porch, summer 2021")
    assert not [m for m in lib.store.all() if m.source.startswith("photo:")]   # captioned but not approved: not learned
    p = lib.review(sha, status="approved")
    m = lib.store.get(p["memory_id"])
    assert "Daisy on the back porch" in m.content and "grey cat" in m.content and m.source.startswith("photo:")
    raw = lib.root / p["preserved"]
    assert raw.exists() and not raw.stat().st_mode & stat.S_IWUSR
    # caption change → old memory hidden, new one saved
    p2 = lib.review(sha, caption="Daisy, 2022")
    assert p2["memory_id"] != m.id and lib.store.get(m.id) is None   # hidden = Echo no longer uses it
    # rejected → Echo stops using it
    p3 = lib.review(sha, status="rejected")
    assert p3["memory_id"] is None and lib.store.get(p2["memory_id"]) is None


def test_add_category(lib):
    c = lib.add_category("Family Holidays")
    assert c == {"id": "family-holidays", "label": "Family Holidays"} and (lib.staging / "family-holidays").is_dir()
    with pytest.raises(PhotoError):
        lib.add_category("Family Holidays")
    with pytest.raises(PhotoError):
        lib.add_category("../..")
    with pytest.raises(PhotoError):
        lib.folder("nope")


def test_describe_waiting_background(lib):
    for i, c in enumerate([(10, 10, 10), (20, 20, 20)]):
        photo(lib.staging / f"places/p{i}.jpg", c)
    lib.scan()
    lib.describe_waiting()
    import time
    for _ in range(100):
        if not lib.describing["running"]:
            break
        time.sleep(0.02)
    assert lib.describing["done"] == 2 and all(p["description"] for p in lib.summary()["photos"])


def test_photos_api(lib, tmp_path):
    photo(lib.staging / "cats/a.jpg", (90, 90, 90))
    c = TestClient(create_app(PersonaConfig(), EchoBackend(), lib.store, FakeSTT(), None, None, photos=lib))
    assert c.post("/api/photos/scan").json()["new_photos"] == 1
    p = c.get("/api/photos").json()["photos"][0]
    assert c.get(f"/api/photos/view/{p['sha256'][:16]}.jpg").headers["content-type"] == "image/jpeg"
    assert c.get("/api/photos/view/..%2Fx.jpg").status_code in (400, 404)
    assert c.patch(f"/api/photos/{p['sha256']}", json={"status": "approved"}).status_code == 400
    assert c.post("/api/photos/describe", json={"sha256": p["sha256"]}).json()["description"]
    assert c.patch(f"/api/photos/{p['sha256']}", json={"status": "approved", "caption": "Milo"}).json()["memory_id"]
    assert c.post("/api/photos/categories", json={"label": "Family"}).json()["id"] == "family"
    assert c.post("/api/photos/google/pick", json={"email": "x@y.z", "category": "nope"}).status_code == 400
    assert "PHOTOS" in c.get("/photos.html").text and "/photos.html" in c.get("/").text


def test_loved_one_photo_removed_everywhere_and_never_reimported(lib):
    photo(lib.staging / "home/kitchen.jpg", (70, 60, 50))
    lib.scan()
    sha = lib.summary()["photos"][0]["sha256"]
    lib.describe(sha)
    p = lib.review(sha, caption="kitchen", status="approved")
    raw, mid = lib.root / p["preserved"], p["memory_id"]
    lib.remove_loved_one(sha)
    assert lib.store.get(mid) is None and not raw.exists() and not (lib.staging / "home/kitchen.jpg").exists()
    assert not (lib.view / f"{sha[:16]}.jpg").exists() and lib.summary()["photos"] == []
    with pytest.raises(PhotoError):
        lib.describe(sha)
    with pytest.raises(PhotoError):
        lib.review(sha, status="approved")
    photo(lib.staging / "home/kitchen-again.jpg", (70, 60, 50))   # same photo picked again
    assert lib.scan()["new_photos"] == 0 and lib.summary()["photos"] == []
