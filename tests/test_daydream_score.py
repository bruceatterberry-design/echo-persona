"""Daydream step 3: score cards, personality match, report, votes, the fixed test set's answer checks."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from core import daydream
from core import daydream_score as score
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore
from tests.test_daydream import local, on, until


@pytest.fixture(autouse=True)
def private_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(score, "CARDS", tmp_path / "cards")
    monkeypatch.setattr(score, "PRIVATE", tmp_path / "private")


# ---- personality, steadiness, accuracy, consistency ----
def test_personality_clean_answer_scores_full():
    p = score.personality("I'd put $250 a month aside first; my guess is you'll get there by summer.")
    assert p == {"score": 1.0, "failed": []}


@pytest.mark.parametrize("text,rule", [
    ("As an AI, I can't have favourites, but here's a plan.", "no assistant-speak"),
    ("I asked Claude and I think the answer is 12.", "no model names"),
    ("## Plan\nI would start with rent.", "no markdown headings"),
    ("I think so, but I am only text-based.", "Echo's global checks"),
    ("That works, sweetheart. I think so.", "no pet names for the owner"),
    ("Certainly! I can help.", "no stock openers"),
    ("The total is 12. Rent comes first.", "first person")])
def test_personality_rules(text, rule):
    p = score.personality(text)
    assert rule in p["failed"] and p["score"] < 1.0


def test_steadiness_finds_the_paragraph_that_drifts():
    good = "I think we should start with the rent, because it's the biggest thing and I know it never moves."
    drift = "As an AI language model, the user should consider allocating funds across several budget categories."
    assert score.steadiness(good + "\n\n" + good) == 1.0
    assert score.steadiness(good + "\n\n" + drift) < 0.8
    assert score.steadiness(good) is None                               # one paragraph: nothing to compare


def test_accuracy_and_consistency():
    assert score.accuracy("It comes to 750 g and 5 eggs.", [r"\b750\b", r"\b5 eggs"]) == 1.0
    assert score.accuracy("It comes to 750 g.", [r"\b750\b", r"\b5 eggs"]) == 0.5
    assert score.accuracy("anything", None) is None
    assert score.consistency([1.0, 1.0, 0.5]) == round(2 / 3, 3)
    assert score.consistency([1.0]) is None


# ---- cards ----
def test_card_format_and_files(tmp_path):
    c = score.new_card("secret live task", "live", "abc123")
    c["modes"]["echo"] = score.measure(["I think 12."], None, [1000], [{"prompt": 10, "reply": 5}], {"echo": "used"})
    c["modes"]["daydream"] = score.measure(["I think 12."], None, [5000], [{"prompt": 50, "reply": 20}], {"claude": "used"})
    c["modes"]["echo_tower"] = {"status": "tower unavailable", "runs": 0}
    p = score.write(c)
    card = json.loads(p.read_text())
    assert card["card"] == score.CARD_VERSION and set(card["modes"]) == set(score.MODES)
    for k in ("id", "at", "kind", "task_sha", "vote", "issues", "invented_caught", "revised", "summary"):
        assert k in card
    assert "secret" not in p.read_text()                                 # live: no task text in docs/bench
    assert p.with_suffix(".txt").read_text().strip() == card["summary"]
    assert "echo_tower (tower unavailable)" in card["summary"]


def test_vote_before_and_after_the_card():
    assert score.vote("early", "up") and score.pending_vote("early") == "up"
    c = score.new_card("t", "live", "late")
    c["modes"]["echo"] = score.measure(["I think so."], None, [1], [{}])
    score.write(c)
    assert score.vote("late", "down")
    assert score.load_cards()[0]["vote"] == "down"
    assert not score.vote("late", "maybe")


def test_report_and_spoken_text():
    assert "haven't scored" in score.report_text(score.report())
    for i, (acc, kind) in enumerate([(0.5, "testset"), (1.0, "testset"), (None, "live")]):
        c = score.new_card(f"t{i}", kind, f"id{i}", invented_caught=1, issues={"mistake": 2, "gap": 0, "invented": 1})
        for m in score.MODES:
            c["modes"][m] = score.measure(["I think it's 750."], [r"750"] if acc else None, [2000], [{"reply": 9}])
            if acc:
                c["modes"][m]["accuracy"] = acc
        score.write(c)
    r = score.report()
    assert r["cards"] == 3 and r["testset"] == 2 and r["live"] == 1
    assert r["overall"]["daydream"]["accuracy"] == 0.75 and r["invented_caught"] == 3 and r["mistakes_caught"] == 6
    t = score.report_text(r)
    assert "3 daydream score cards" in t and "75% right" in t and "3 invented facts" in t


# ---- live comparison (after the answer was given) ----
def _res(tower: bool):
    r = daydream.Result(task="plan a trip", parts=["part A", "part B"], answer="Here's my plan.", ok=True)
    r.drafts = {"tower": ["tower A", None] if tower else None, "claude": ["c A", "c B"]}
    if not tower:
        r.drafts.pop("tower")
    r.helpers = {"tower": "used" if tower else "unavailable", "claude": "used", "critic": "used", "checker": "used"}
    r.issues = [{"kind": "invented"}, {"kind": "gap"}]
    r.final_issues = [{"kind": "mistake"}]
    r.revised, r.ms, r.tokens = True, {"total": 40000, "plan": 3000, "drafts": 9000}, {"prompt": 900, "reply": 300}
    return r


@pytest.mark.parametrize("tower", [True, False])
def test_compare_live(tower):
    log, waits = [], []

    async def quiet():
        waits.append(1)
    card = asyncio.run(score.compare_live(_res(tower), local(log), daydream.merge_prompt, quiet))
    assert card["modes"]["daydream"]["ms"] == 40000 and card["invented_caught"] == 1 and card["revised"]
    assert card["issues"] == {"mistake": 1, "gap": 1, "invented": 1}
    assert card["modes"]["echo"]["runs"] == 1 and card["modes"]["echo"]["ms"] is not None
    if tower:
        assert card["modes"]["echo_tower"]["helpers"] == {"tower": "used", "echo": "used"}
        merge = log[-1]["messages"][-1]["content"]
        assert "Draft (tower): tower A" in merge and "Draft (echo)" in merge and "c A" not in merge   # no Claude text
        assert len(waits) == 3                                         # waited for quiet before each model call
    else:
        assert card["modes"]["echo_tower"] == {"status": "tower unavailable", "runs": 0}
        assert len(waits) == 1
    private = json.loads((score.PRIVATE / f"{card['id']}.json").read_text())
    assert private["task"] == "plan a trip" and private["answers"]["daydream"] == "Here's my plan."


# ---- chat: "daydream report" and votes ----
@pytest.mark.parametrize("text", ["daydream report", "Echo, daydream report.", "give me the daydream report",
                                  "daydream scores", "what's the daydream report?"])
def test_report_words(text):
    assert daydream.is_report(text)


def test_report_is_not_a_task():
    assert not daydream.is_report("daydream: write a report about our budget")


def test_socket_report_and_vote(tmp_path, monkeypatch):
    on(tmp_path, monkeypatch)
    c = score.new_card("t", "live", "vote-me")
    c["modes"]["echo"] = score.measure(["I think so."], None, [1], [{}])
    score.write(c)
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "daydream report"})
        assert "1 daydream score cards" in until(ws, "reply")["text"]
        ws.send_json({"type": "daydream_vote", "ddid": "vote-me", "vote": "up"})
        ws.send_json({"text": "hello"})
        until(ws, "reply")
    assert score.load_cards()[0]["vote"] == "up"
    assert client.get("/api/daydream/report").json()["votes"] == {"up": 1, "down": 0}


def test_report_api_hidden_when_off(tmp_path):
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    assert client.get("/api/daydream/report").status_code == 404


def test_socket_answer_carries_ddid_and_starts_comparison(tmp_path, monkeypatch):
    on(tmp_path, monkeypatch)
    started = []

    async def fake_run(task, loc, cfg, *a, **k):
        return daydream.Result(task=task, answer="Thought it through.", ok=True, parts=[task], id="dd1")

    async def fake_compare(res, *a, **k):
        started.append(res.id)
        return {}
    monkeypatch.setattr(daydream, "run", fake_run)
    monkeypatch.setattr(score, "compare_live", fake_compare)
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "daydream: compare two phone plans"})
        until(ws, "reply")
        r = until(ws, "reply")
        assert r["ddid"] == "dd1" and r["text"].startswith("Thought it through.")
        until(ws, "daydream")
    assert started == ["dd1"]


# ---- the fixed test set: every task's checks accept a right answer and reject a wrong one ----
RIGHT = {"t01-recipe": "I'd use 750 g of flour and 5 eggs, so 1 box is enough.",
         "t02-budget": "You'd have $410 left each month, so it takes 8 months to reach $3,000.",
         "t03-drive": "I make it 12:30 PM.",
         "t04-cat-meds": "That's 28 tablets in all, so you need 3 packs.",
         "t05-workday": "Yes, you can: that leaves 195 minutes free.",
         "t06-ages": "Dan is the youngest, and Ben is the second oldest.",
         "t07-interest": "You'd have $1,157.63, and $157.63 of that is interest.",
         "t08-discount": "You pay $54, so no, it isn't the same as 35% off, which would be $52.",
         "t09-gift-identity": "The gift can cost $45. I'm the owner's digital self.",
         "t10-unknown": "I don't know your neighbor's dog's name. You could ask them, or chat on a walk.",
         "t11-flooring": "It's 180 square feet, so you need 9 boxes.",
         "t12-doses": "That's 9 doses, and the last one is at 10 PM on day three."}
WRONG = {"t01-recipe": "Use 600 g and 4 eggs, 2 boxes.", "t02-budget": "$510 left, 6 months.", "t03-drive": "11:40 AM.",
         "t04-cat-meds": "14 tablets, 2 packs.", "t05-workday": "No.", "t06-ages": "Cara is the youngest; Anna is second.",
         "t07-interest": "$1,150.00 with $150 interest.", "t08-discount": "You pay $52 and yes it's the same.",
         "t09-gift-identity": "The gift can be $50.", "t10-unknown": "Your neighbor's dog is called Max, a beagle.",
         "t11-flooring": "200 square feet, 10 boxes.", "t12-doses": "12 doses, last at 6 AM."}


def test_set_checks_right_and_wrong_answers():
    from scripts.daydream_set import TASKS
    assert [t["id"] for t in TASKS] == list(RIGHT)
    for t in TASKS:
        assert score.accuracy(RIGHT[t["id"]], t["expect"]) == 1.0, t["id"]
        assert score.accuracy(WRONG[t["id"]], t["expect"]) < 1.0, t["id"]


def test_set_one_task_builds_a_full_card(monkeypatch):
    from scripts import daydream_set as ds

    async def fake_run(task, loc, cfg, drafters=None, critic=None, checker=None, on_event=None, res=None):
        full = critic is not None
        r = daydream.Result(task=task, answer="I'd say $410 left, so 8 months.", ok=True, parts=[task])
        r.helpers = {"tower": "used"} | ({"claude": "used"} if full else {})
        r.issues = [{"kind": "invented"}] if full else []
        r.ms, r.tokens = {"total": 1000}, {"prompt": 5, "reply": 5}
        return r
    monkeypatch.setattr(daydream, "run", fake_run)
    t = next(x for x in ds.TASKS if x["id"] == "t02-budget")
    card = asyncio.run(ds.one_task(t, local([]), daydream.DEFAULTS, object(), repeat=2, full_repeat=1))
    assert card["task"] == t["task"] and card["task_id"] == "t02-budget" and card["suite"] == ds.SUITE_VERSION
    assert card["modes"]["echo_tower"]["accuracy"] == 1.0 and card["modes"]["echo_tower"]["runs"] == 2
    assert card["modes"]["daydream"]["accuracy"] == 1.0 and card["invented_caught"] == 1
    assert card["modes"]["echo"]["runs"] == 2 and card["modes"]["echo"]["accuracy"] == 0.0   # fake Echo says "love"
    assert (score.CARDS).glob("*.json")


def test_personality_scores_the_spoken_text_not_the_face_tag():
    p = score.personality("[smiling] I'd put $250 aside each month; my guess is summer.")
    assert p == {"score": 1.0, "failed": []}                              # 11-04: the tag failed all 7 rules
    p = score.personality("[smiling] As an AI, the plan is fine.")
    assert set(p["failed"]) == {"no assistant-speak", "Echo's global checks", "first person"}


def test_honest_unknown_wording_counts():
    from scripts.daydream_set import TASKS
    t = next(x for x in TASKS if x["id"] == "t10-unknown")
    assert score.accuracy("[neutral] My notes don't mention your neighbor, so I don't have that information yet.",
                          t["expect"]) == 1.0


def test_merge_prompt_keeps_drafts_private():
    m = daydream.merge_prompt("t", ["a"], {"tower": ["x"]})
    assert "never saw them" in m and "never say they wrote" in m
