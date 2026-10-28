import json
import os
import stat
import sys
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import face.library as lib
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from face.library import FaceError, FaceLibrary
from memory.store import MemoryStore
from voice.stt import FakeSTT

FAKE = '''#!{py}
import json, sys, os
from PIL import Image
out = sys.argv[1]; os.makedirs(out, exist_ok=True)
for fi, p in enumerate(sys.argv[2:]):
    im = Image.open(p); w, h = im.size
    # pretend: one face per image, two if the file name contains "group"; feature = mean colour
    n = 2 if "group" in p else 1
    faces = []
    for i in range(n):
        name = f"img{{fi:04d}}-face{{i:02d}}.jpg"; im.convert("RGB").resize((512, 512)).save(os.path.join(out, name))
        c = [float(x) for x in im.convert("RGB").resize((1, 1)).getpixel((0, 0))]
        faces.append({{"crop": name, "box": [0, 0, w, h], "face_px": min(w, h), "roll": 0, "yaw": 0.1,
                      "quality": 0.5, "feature": c if i == 0 else [c[2], c[1], c[0]]}})
    print(json.dumps({{"image": p, "width": w, "height": h, "faces": faces}}))
'''


@pytest.fixture
def fl(tmp_path, monkeypatch):
    fake = tmp_path / "facefind"
    fake.write_text(FAKE.format(py=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setattr(lib, "FACEFIND", fake)
    root = tmp_path / "ds"
    for d in ("photos-current", "photos-younger/30s"):
        (root / "staging/face" / d).mkdir(parents=True)
    return FaceLibrary(root)


def img(path, color, size=(400, 400)):
    rng = np.random.default_rng(sum(color))
    arr = np.clip(np.array(color) + rng.normal(0, 40, (*size[::-1], 3)), 0, 255).astype(np.uint8)  # textured, not blurry
    Image.fromarray(arr).save(path)


def test_scan_dedup_links_and_groups(fl):
    s = fl.staging
    img(s / "photos-current/a.jpg", (200, 150, 120))
    img(s / "photos-current/group.jpg", (90, 120, 200))
    (s / "photos-current/b-copy.jpg").write_bytes((s / "photos-current/a.jpg").read_bytes())  # duplicate
    (s / "photos-current/link.webloc").write_text("<plist/>")
    img(s / "photos-younger/30s/y.jpg", (180, 140, 110), (120, 160))
    r = fl.scan()
    assert r == {"new_photos": 3, "duplicates": 1, "unzipped": 0, "skipped_links": 1, "faces_found": 4}
    sm = fl.summary()
    assert sm["counts"]["faces"] == 4 and sm["links_waiting"] == 1
    young = next(p for p in sm["photos"] if p["group"] == "younger")
    assert young["age_label"] == "30s" and "small / low resolution" in young["faces"][0]["issues"]
    assert "feature" not in sm["photos"][0]["faces"][0]
    assert fl.scan()["new_photos"] == 0  # idempotent


def test_zip_unpacked_and_zip_slip_blocked(fl, tmp_path):
    src = tmp_path / "p.jpg"; img(src, (100, 100, 100))
    z = fl.staging / "photos-current/takeout.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(src, "Takeout/Google Photos/p.jpg")
        zf.writestr("../../evil.jpg", b"x")
        zf.writestr("notes.txt", b"x")
    r = fl.scan()
    assert r["unzipped"] == 1 and r["new_photos"] == 1
    assert not (fl.root / "staging/evil.jpg").exists() and not (fl.root / "evil.jpg").exists()


def test_review_preserves_original_read_only(fl):
    img(fl.staging / "photos-current/a.jpg", (200, 150, 120))
    fl.scan()
    sha = next(iter(fl.load()["photos"]))
    f = fl.review(sha, 0, status="owner", preferred=True)
    assert f["status"] == "owner" and f["preferred"]
    raw = next(fl.raw.glob("*.jpg"))
    assert raw.read_bytes() == (fl.staging / "photos-current/a.jpg").read_bytes()
    assert not os.access(raw, os.W_OK)
    assert json.loads(raw.with_suffix(".jpg.meta.json").read_text())["sha256"] == sha
    assert fl.review(sha, 0, status="not_owner")["preferred"] is False  # star cleared
    with pytest.raises(FaceError):
        fl.review(sha, 5, status="owner")
    with pytest.raises(FaceError):
        fl.review(sha, 0, status="maybe")
    with pytest.raises(FaceError):
        fl.review(sha, 0, age_label="../../x")


def test_hint_follows_confirmed_faces(fl):
    img(fl.staging / "photos-current/me1.jpg", (200, 150, 120))
    img(fl.staging / "photos-current/me2.jpg", (205, 152, 118))
    img(fl.staging / "photos-current/other.jpg", (40, 90, 230))
    fl.scan()
    photos = {p["name"]: p["sha256"] for p in fl.summary()["photos"]}
    fl.review(photos["me1.jpg"], 0, status="owner")
    fl.review(photos["other.jpg"], 0, status="not_owner")
    hints = {p["name"]: p["faces"][0]["hint"] for p in fl.summary()["photos"]}
    assert hints["me2.jpg"] > 0


def test_crop_name_validation(fl):
    with pytest.raises(FaceError):
        fl.crop_path("../../raw/face/x.jpg")
    assert fl.crop_path("0123456789abcdef-face00.jpg").name == "0123456789abcdef-face00.jpg"


def test_missing_drive(tmp_path):
    with pytest.raises(FaceError, match="plugged in"):
        FaceLibrary(tmp_path / "nope").scan()


def test_face_api(fl, tmp_path):
    img(fl.staging / "photos-current/a.jpg", (200, 150, 120))
    c = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite"), FakeSTT(), None, fl))
    assert c.post("/api/face/scan").json()["new_photos"] == 1
    p = c.get("/api/face/library").json()["photos"][0]
    assert c.get(f"/api/face/crop/{p['faces'][0]['crop']}").headers["content-type"] == "image/jpeg"
    assert c.get("/api/face/crop/..%2F..%2Fsecret.jpg").status_code in (400, 404)
    assert c.patch(f"/api/face/{p['sha256']}/0", json={"status": "owner"}).json()["status"] == "owner"
    assert "FACE LIBRARY" in c.get("/face.html").text


def test_atlas_endpoints(fl, tmp_path):
    c = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite"), FakeSTT(), None, fl))
    assert c.get("/api/face/atlas").json() == {"available": False}   # not rendered yet → window keeps the simple face
    (fl.atlas / "frames").mkdir(parents=True)
    img(fl.atlas / "frames/neutral-m0-e0.jpg", (120, 100, 90))
    (fl.atlas / "atlas.json").write_text(json.dumps({"frames": {"neutral/0/0": "frames/neutral-m0-e0.jpg"},
                                                     "lip_levels": [0.0], "expressions": ["neutral"]}))
    assert c.get("/api/face/atlas").json()["frames"]
    assert c.get("/api/face/atlas/frames/neutral-m0-e0.jpg").headers["content-type"] == "image/jpeg"
    assert c.get("/api/face/atlas/frames/smiling-m0-e0.jpg").status_code == 404
    assert c.get("/api/face/atlas/frames/..%2Fatlas.json").status_code in (400, 404)
    img(fl.atlas / "frames/L-neutral-m12-e0.jpg", (120, 100, 90))   # head-pose frames (v5 names)
    assert c.get("/api/face/atlas/frames/L-neutral-m12-e0.jpg").status_code == 200
    assert c.get("/api/face/atlas/frames/LL-neutral-m1-e0.jpg").status_code == 400


def test_motion_endpoints(fl, tmp_path):
    c = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite"), FakeSTT(), None, fl))
    assert c.get("/api/face/motion").json() == {"clips": []}      # none yet → window keeps preset head movement
    fl.motion.mkdir(parents=True)
    (fl.motion / "idle-head.mp4").write_bytes(b"\0\0\0 ftypisom" + b"\0" * 100)
    (fl.motion / "motion.json").write_text(json.dumps({"clips": [{"name": "head", "file": "idle-head.mp4"}]}))
    assert c.get("/api/face/motion").json()["clips"][0]["file"] == "idle-head.mp4"
    assert c.get("/api/face/motion/idle-head.mp4").headers["content-type"] == "video/mp4"
    assert c.get("/api/face/motion/..%2Fmotion.json").status_code in (400, 404)
    assert c.get("/api/face/motion/driving.mp4").status_code == 400
