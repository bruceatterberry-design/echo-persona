"""CA 2.0 Phase 4: photo memories get camera dates (kept apart from the day they were shared), same-day links and a
collection overview; all through versioned updates, nothing deleted."""
import json

from PIL import Image

from memory.photos import PhotoLibrary, camera_date, nice_date, short_label
from memory.store import MemoryStore


def shot(path, color, taken=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (64, 48), color)
    ex = im.getexif()
    if taken:
        ex.get_ifd(0x8769)[36867] = taken
    im.save(path, "JPEG", exif=ex)


def lib_with(tmp_path, photos):
    """photos: name -> (camera date or None, description). Scans, describes (fake) and approves them."""
    root = tmp_path / "ds"; root.mkdir()
    descs = {}
    for i, (name, (taken, desc)) in enumerate(photos.items()):
        shot(root / "staging/photos/places" / name, (i * 40 % 255, 90, 120), taken)
        descs[name] = desc
    lib = PhotoLibrary(root, MemoryStore(tmp_path / "m.sqlite"), lambda img, label: "")
    lib.scan()
    for sha, p in lib.load()["photos"].items():
        idx = lib.load(); idx["photos"][sha]["description"] = descs[p["name"]]; lib.save(idx)
        lib.review(sha, status="approved")
    return lib


def test_camera_date_and_labels(tmp_path):
    shot(tmp_path / "a.jpg", (1, 2, 3), "2022:01:15 10:17:11")
    shot(tmp_path / "b.jpg", (1, 2, 3))
    assert camera_date(tmp_path / "a.jpg") == "2022-01-15 10:17" and camera_date(tmp_path / "b.jpg") is None
    shot(tmp_path / "c.jpg", (1, 2, 3), "0000:00:00 00:00:00")              # cameras without a clock
    assert camera_date(tmp_path / "c.jpg") is None and camera_date(tmp_path / "missing.jpg") is None
    assert nice_date("2022-01-15 10:17") == "15 January 2022"
    assert short_label("This nighttime photo shows a large steel truss bridge, lit up.") == "a large steel truss bridge"


def test_taken_and_shared_kept_apart_and_same_day_links(tmp_path):
    lib = lib_with(tmp_path, {"dam.jpg": ("2022:01:15 10:17:00", "This photo shows a concrete dam with RIVER on it."),
                              "mtn.jpg": ("2022:01:15 10:08:00", "This photo shows a misty mountain landscape."),
                              "old.jpg": ("2009:03:30 19:08:00", "This photo shows three young people on a couch."),
                              "nodate.jpg": (None, "This photo shows a patio with a blue umbrella.")})
    r = lib.consolidate()
    st, idx = lib.store, lib.load()
    by = {p["name"]: p for p in idx["photos"].values()}
    dam = st.get(by["dam.jpg"]["memory_id"])
    assert "Taken on 15 January 2022 (date from the camera)." in dam.content and "Shared with me on" in dam.content
    assert "Taken the same day: a misty mountain landscape (10:08)." in dam.content and "concrete dam (" not in dam.content
    assert (dam.year, dam.when_text) == (2022, "15 January 2022")
    assert "Date taken: not known" in st.get(by["nodate.jpg"]["memory_id"]).content
    ov = st.get(idx["overview_memory_id"])
    assert ov.kind == "knowledge" and "4 photos" in ov.content and "oldest photo, taken 30 March 2009, shows three young people" in ov.content
    assert "(1 photos have no camera date)" in ov.content
    assert lib.consolidate()["updated"] == 0                               # nothing changes twice
    h = [v for v in st.history(dam.id) if v["action"] == "consolidate"]   # versioned: old wording kept, restorable
    assert len(h) == 1 and "Taken the same day" not in h[0]["before"]["content"]
    assert "Taken the same day" in h[0]["after"]["content"]
    assert h[0]["by"] == "consolidation:photos"


def test_overview_follows_approvals_and_nothing_is_deleted(tmp_path):
    lib = lib_with(tmp_path, {"a.jpg": ("2020:05:01 08:00:00", "This photo shows a red barn."),
                              "b.jpg": ("2021:05:01 08:00:00", "This photo shows a blue lake.")})
    lib.consolidate()
    n_before = lib.store.db.execute("SELECT count(*) FROM memories").fetchone()[0]
    sha_b = next(s for s, p in lib.load()["photos"].items() if p["name"] == "b.jpg")
    lib.review(sha_b, status="rejected")
    lib.consolidate()
    ov = lib.store.get(lib.load()["overview_memory_id"])
    assert "1 photos" in ov.content and "blue lake" not in ov.content
    assert lib.store.db.execute("SELECT count(*) FROM memories").fetchone()[0] == n_before   # hidden, never deleted
