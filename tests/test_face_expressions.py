import json

from face import expressions
from face.library import current_atlas


def _take(d, n, task, label):
    (d / f"take-{n:03d}.json").write_text(json.dumps({"task_id": task, "expression_label": label}))


def test_current_atlas_picks_newest_finished(tmp_path):
    face = tmp_path / "derived" / "face"
    for v in (5, 6, 10):
        (face / f"atlas-v{v}").mkdir(parents=True)
    for v in (5, 6):
        (face / f"atlas-v{v}" / "atlas.json").write_text("{}")
    (face / "atlas-v11-partial").mkdir()
    (face / "atlas-calib-v1").mkdir()
    assert current_atlas(tmp_path).name == "atlas-v6"      # v10 has no atlas.json yet, partial/calib never count


def test_sessions_and_status(tmp_path):
    phone = tmp_path / "raw" / "phone"
    a, b = phone / "20260101-120000", phone / "20260101-110000"
    a.mkdir(parents=True); b.mkdir()
    _take(a, 1, "photo-joy", "joy"); _take(a, 2, "face-joy", "joy")
    _take(b, 1, "photo-joy", "joy")                          # a photo alone isn't enough
    assert expressions.sessions(tmp_path) == ["20260101-120000"]
    face = tmp_path / "derived" / "face"
    pv = face / "expressions-v1"; pv.mkdir(parents=True)
    (pv / "expression-deltas.json").write_text(json.dumps({"session": str(a), "deltas": {}}))
    (pv / "expressions-sheet.jpg").write_bytes(b"jpg")
    (face / "atlas-v6").mkdir()
    (face / "atlas-v6" / "atlas.json").write_text(json.dumps({"expressions": ["neutral", "joy"], "carried_expressions":
                                                              {"from": str(pv / "expression-deltas.json")}}))
    st = expressions.status(tmp_path)
    assert st["atlas"] == "atlas-v6" and st["sessions"] == [{"session": "20260101-120000", "preview": True, "on_face": True}]
