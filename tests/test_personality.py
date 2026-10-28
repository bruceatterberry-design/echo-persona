import json

import pytest
from fastapi.testclient import TestClient

import personality.build as pb
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore
from voice.stt import FakeSTT

ANSWER = ("Describe a typical good day in your life. Well, a good day starts with coffee on the porch with the loved one. "
          "You know, I like to fix something in the garage, and when I explain it I lay it out in steps.")


@pytest.fixture
def ds(tmp_path):
    q = tmp_path / "ds/derived/voice/qc-v1/20261027-065602"; q.mkdir(parents=True)
    (q / "take-0001.json").write_text(json.dumps({"status": "kept", "prompt_id": "about_me-09", "transcript": ANSWER}))
    return tmp_path / "ds"


def fake_llm(host, model, ex, helper=None):
    return {"traits": [
        {"category": "explaining", "trait": "Explain things step by step", "evidence": "I lay it out in steps", "confidence": 1},
        {"category": "values", "trait": "Invented trait", "evidence": "I love skydiving every weekend", "confidence": 1},   # not said
        {"category": "storytelling", "trait": "Quotes the question", "evidence": "Describe a typical good day in your life", "confidence": 1},
    ], "facts": [{"fact": "The owner enjoys coffee on the porch with the loved one", "evidence": "coffee on the porch with the loved one"},
                 {"fact": "The owner's work", "evidence": "fix something in the garage"}]}   # too vague


def test_question_is_stripped():
    assert pb.strip_question(ANSWER, "Describe a typical good day in your life.").startswith("Well, a good day")


def test_only_grounded_traits_survive(ds, tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "ask_llm", fake_llm)
    d = pb.build(ds, tmp_path / "p")
    assert [t["trait"] for t in d["traits"]] == ["Explain things step by step"]
    assert [f["fact"] for f in d["facts"]] == ["The owner enjoys coffee on the porch with the loved one"]
    assert d["dropped_ungrounded"] == 3 and d["traits"][0]["status"] == "pending"
    assert "you know" in d["style"]["fillers"]


def test_traits_block_is_stable_and_has_examples():
    prof = {"traits": [{"trait": "Explain things step by step"}], "style": {"avg_sentence_words": 15.9}, "quotes": [ANSWER]}
    a, b = pb.traits_block(prof), pb.traits_block(prof)
    assert a == b and "step by step" in a and "porch" in a and pb.examples_block(prof, "garage") == ""


def test_prompt_block_uses_only_approved(tmp_path):
    assert pb.prompt_block(None, "hi") == "" and pb.prompt_block({"traits": []}, "hi") == ""
    block = pb.prompt_block({"traits": [{"trait": "Explain things step by step"}], "style": {"avg_sentence_words": 15.9},
                             "quotes": [ANSWER]}, "how do you fix things in the garage?")
    assert "Explain things step by step" in block and "garage" in block and "16 words" in block


def test_review_api(ds, tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "ask_llm", fake_llm)
    cfg = PersonaConfig(); cfg.personality_dir = str(tmp_path / "p")
    pb.build(ds, tmp_path / "p")
    store = MemoryStore(tmp_path / "m.sqlite")
    c = TestClient(create_app(cfg, EchoBackend(), store, FakeSTT()))
    t = c.get("/api/personality").json()["traits"][0]
    assert "quotes" not in c.get("/api/personality").json()
    c.patch(f"/api/personality/trait/{t['id']}", json={"trait": "Explain it in steps, like I do", "status": "approved"})
    prof = json.loads((tmp_path / "p/profile.json").read_text())
    assert prof["traits"] == [{"category": "explaining", "trait": "Explain it in steps, like I do"}]
    f = c.get("/api/personality").json()["facts"][0]
    assert c.patch(f"/api/personality/fact/{f['id']}", json={"status": "saved"}).json()["memory_id"]
    assert store.all()[0].source == "owner:personality-review" and store.all()[0].status == "confirmed"
    assert c.post("/api/personality/try", json={"question": "hello"}).json()["answer"]
    assert c.post("/api/personality/rate", json={"question": "q", "answer": "a", "rating": "like_me"}).json()["ok"]
    assert c.patch("/api/personality/trait/nope", json={"status": "approved"}).status_code == 400


def test_condensed_summary_is_used_and_editable(ds, tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "ask_llm", fake_llm)
    cfg = PersonaConfig(); cfg.personality_dir = str(tmp_path / "p")
    pb.build(ds, tmp_path / "p")
    c = TestClient(create_app(cfg, EchoBackend(), MemoryStore(tmp_path / "m.sqlite"), FakeSTT()))
    t = c.get("/api/personality").json()["traits"][0]
    c.patch(f"/api/personality/trait/{t['id']}", json={"status": "approved"})
    monkeypatch.setattr(pb, "_condense_group", lambda h, m, cat, items, most, helper=None: [{"text": "Explain in steps. (t000)", "from": list(items), "category": cat}])
    r = c.post("/api/personality/condense").json()
    assert r["use_condensed"] and r["condensed"][0]["text"].startswith("Explain in steps")
    prof = json.loads((tmp_path / "p/profile.json").read_text())
    assert prof["condensed"] and "Explain in steps" in pb.traits_block(prof)
    c.patch("/api/personality/condensed", json={"lines": ["Keep it plain.", "", "Tell a story."]})
    assert json.loads((tmp_path / "p/profile.json").read_text())["condensed"] == ["Keep it plain.", "Tell a story."]
    c.patch("/api/personality/condensed", json={"use": False})
    assert "condensed" not in json.loads((tmp_path / "p/profile.json").read_text())


def test_incremental_rebuild_keeps_decisions(ds, tmp_path, monkeypatch):
    calls = []
    def llm(host, model, ex, helper=None):
        calls.append(ex["source"]); return fake_llm(host, model, ex)
    monkeypatch.setattr(pb, "ask_llm", llm)
    out = tmp_path / "p"
    d = pb.build(ds, out)
    d["traits"][0] |= {"status": "approved", "trait": "Lay it out in steps, like I do", "edited_by_owner": True,
                       "trait_original": d["traits"][0]["trait"]}
    d["condensed"], d["use_condensed"] = [{"text": "Keep it plain."}], True
    (out / "draft.json").write_text(json.dumps(d))
    calls.clear()
    assert pb.build(ds, out)["last_update"]["new_recordings"] == 0 and calls == []   # nothing new → no model calls
    q = ds / "derived/voice/qc-v1/20261028-090000"; q.mkdir(parents=True)
    (q / "take-0001.json").write_text(json.dumps({"status": "kept", "prompt_id": "about_me-09", "transcript":
        "Well, I lay it out in steps every time. And I really love fishing on the river with my brother on Saturdays."}))
    d2 = pb.build(ds, out)
    assert len(calls) == 1 and d2["last_update"]["new_recordings"] == 1
    t = d2["traits"][0]
    assert t["status"] == "approved" and t["trait"] == "Lay it out in steps, like I do" and len(t["evidence"]) == 2
    assert d2["condensed"] == [{"text": "Keep it plain."}] and d2["use_condensed"] is True
    assert d2["examples"] == 2
