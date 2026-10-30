"""Personality versions + checks (Phase 3)."""
import json
import time

from fastapi.testclient import TestClient

from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore
from personality import evals as pe
from personality.versions import Versions


def setup(tmp_path):
    pdir = tmp_path / "p"; pdir.mkdir()
    prompt = tmp_path / "personality.yaml"; prompt.write_text("system_prompt: |\n  Be kind.\n")
    (pdir / "profile.json").write_text(json.dumps({"traits": [{"category": "humor", "trait": "dry humor"}], "condensed": ["A"]}))
    (pdir / "draft.json").write_text("{}")
    return Versions(pdir, prompt), pdir, prompt


def test_snapshot_dedup_diff_and_restore_is_undoable(tmp_path):
    v, pdir, prompt = setup(tmp_path)
    a = v.snapshot("first")
    assert v.snapshot("same again")["id"] == a["id"] and v.current_id() == a["id"]
    (pdir / "profile.json").write_text(json.dumps({"traits": [{"category": "humor", "trait": "warm humor"}], "condensed": ["A", "B"]}))
    prompt.write_text("system_prompt: |\n  Be kind. No pet names for the owner.\n")
    d = v.diff(a["id"])
    assert d["traits_added"] == ["warm humor"] and d["traits_removed"] == ["dry humor"] and d["summary_added"] == ["B"]
    assert any("No pet names" in l for l in d["prompt"])
    v.restore(a["id"])
    assert "dry humor" in (pdir / "profile.json").read_text() and "pet names" not in prompt.read_text()
    vs = v.list()
    assert len(vs) == 2 and "before restoring" in vs[-1]["note"]           # the undone state was kept
    v.restore(vs[-1]["id"])
    assert "pet names" in prompt.read_text()


def test_snapshot_if_stale_once_per_session(tmp_path):
    v, pdir, _ = setup(tmp_path)
    assert v.snapshot_if_stale() is not None
    (pdir / "draft.json").write_text('{"x": 1}')
    assert v.snapshot_if_stale() is None                                   # same editing session
    assert v.snapshot_if_stale(min_age_s=0) is not None


def test_bad_version_ids(tmp_path):
    v, *_ = setup(tmp_path)
    for bad in ("../../etc", "nope"):
        try:
            v.diff(bad); assert False
        except KeyError:
            pass


def test_checks():
    assert pe.check("They're Max and Ruby.", {"all": ["max", "ruby"]}) == []
    assert pe.check("I don’t remember that, tell me?", {"any": pe.DONT_KNOW}) == []      # curly apostrophe
    assert pe.check("Paris was lovely, the Eiffel Tower!", {"any": pe.DONT_KNOW, "none": ["eiffel"]})
    assert pe.check("Good morning, hon.", {"none": [pe.PET_NAMES]}) and not pe.check("I love that idea.", {"none": [pe.PET_NAMES]})
    assert pe.check("I'm just a text-based program.", {})                            # global check
    assert pe.check(" ".join(["word"] * 90), {"max_words": 80})


def test_summary_marks_inconsistent_cases():
    s = pe.summarize([{"id": "a", "category": "x", "passed": True}, {"id": "a", "category": "x", "passed": False},
                      {"id": "b", "category": "y", "passed": True}])
    assert s["passed"] == 2 and s["by_category"] == {"x": "1/2", "y": "1/1"} and s["inconsistent"] == ["a"]


def test_eval_and_version_api(tmp_path):
    pdir = tmp_path / "p"; pdir.mkdir()
    (pdir / "profile.json").write_text(json.dumps({"traits": [], "style": {}, "quotes": []}))
    cfg = PersonaConfig(memory_db=str(tmp_path / "m.sqlite"), personality_dir=str(pdir))
    with TestClient(create_app(cfg, EchoBackend(), MemoryStore(tmp_path / "m.sqlite"))) as c:
        cases = c.get("/api/personality/evals").json()["cases"]
        assert len(cases) == len(pe.DEFAULT_CASES) and not any(x["approved"] for x in cases)
        assert c.patch("/api/personality/evals/unknown-teacher", json={"approved": True}).status_code == 200
        assert c.patch("/api/personality/evals/short", json={"remove": True}).status_code == 200
        c.post("/api/personality/evals/run", json={"note": "test"})
        for _ in range(100):
            j = c.get("/api/personality/evals").json()
            if j["last"]:
                break
            time.sleep(0.05)
        assert j["last"]["summary"]["total"] == 2 * (len(pe.DEFAULT_CASES) - 1) and j["last"]["version"]
        assert [x for x in j["cases"] if x["id"] == "unknown-teacher"][0]["approved"]
        v = c.post("/api/personality/versions", json={"note": "mine"}).json()
        assert c.get("/api/personality/versions").json()["current"] == v["id"]
        assert c.get(f"/api/personality/versions/{v['id']}/diff").json()["traits_added"] == []
        assert c.post("/api/personality/versions/nope/restore").status_code == 404
