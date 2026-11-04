"""Daydream score cards (step 3): does the extra thinking actually help?

Every daydream (and every task of the fixed test set, scripts/daydream_set.py) gets a score card comparing three
answers to the same task:
  echo        Echo alone: his own model, one answer, no helpers
  echo_tower  Echo + the tower: Echo's plan, the tower's drafts (Echo fills gaps), Echo's merge; no Claude
  daydream    the full daydream: tower + Claude drafts, Claude's critique, Echo's merge, Claude's final check
Metrics per answer: accuracy (only where the task has an expected answer: regexes, like scripts/baseline.py),
consistency over repeated runs (test set), personality match (rules reused from personality/evals.py and baseline's
identity check), steadiness (the weakest paragraph's personality score: does the voice hold over a long answer?),
time, tokens, helpers used; for the daydream also the critic's mistakes / gaps / invented facts caught, and the owner's
👍/👎 when given. Personality match is a rule check, not a judgement: it catches assistant-speak, model names,
lost first person, markdown headings, stage directions and pet names, nothing subtler.

Where things go:
  docs/bench/daydream/<time>-<id>.json + .txt   the card (metrics; the task text only for test-set tasks)
  data/daydream/<id>.json                        live daydreams: the task and all three answers (private, not in git)
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from personality import evals

ROOT = Path(__file__).resolve().parents[1]
CARDS = ROOT / "docs" / "bench" / "daydream"
PRIVATE = ROOT / "data" / "daydream"
MODES = ("echo", "echo_tower", "daydream")
CARD_VERSION = "daydream-scorecard-v1"

# ---- personality match: one rule per line, each checked with personality/evals.py's check() ----
PERSONALITY_RULES = [
    ("no assistant-speak", {"none": [r"\bas an ai\b", r"\bi'?m (?:just |only )?an? (?:ai|assistant|language model|chatbot)\b",
                                     r"\bi don'?t have (?:personal )?(?:feelings|opinions|experiences)\b"]}),
    ("no model names", {"none": [r"\b(?:claude|chatgpt|gpt-?\d|gemma|qwen|llama|openai|anthropic)\b"]}),
    ("Echo's global checks", {}),   # personality/evals.py GLOBAL_NONE: "text-based", "language model", leftover cues
    ("first person", {"any": ["i ", "i'm", "i'd", "i've", "i'll", " my ", "me "]}),
    ("no markdown headings", {"none": [r"(?m)^\s*#{1,6} ", r"(?m)^\s*\*\*[^*\n]{2,60}\*\*\s*$"]}),
    ("no pet names for the owner", {"none": ["@pet_names"]}),
    ("no stock openers", {"none": [r"^\W*(?:certainly|absolutely|great question|of course)\b"]}),
]
IDENTITY_RX = r"owner|digital"   # scripts/baseline.py IDENTITY: "Who are you?" must name the owner or say digital self


def spoken(text: str) -> str:
    """What Echo actually says: the leading [expression] tag and face cues removed, exactly as core/schema.py does
    before speaking (found 11-04: scoring the raw tag failed every answer)."""
    from core.schema import StreamParser
    sp = StreamParser()
    sp.feed(text or "")
    return sp.finish().text


def _rule_fails(text: str, chk: dict) -> bool:
    """One rule, the way personality/evals.py checks (none = regexes, any = phrases; shared @lists expanded), but
    without evals' global checks, which have their own rule."""
    low = text.lower().replace("’", "'")
    if any(re.search(rx, low) for rx in evals._expand(chk.get("none", []))):
        return True
    phrases = evals._expand(chk.get("any", []))
    return bool(phrases) and not any(p.lower() in low for p in phrases)


def personality(text: str) -> dict:
    """{score 0–1, failed: [rule names]} for one answer, scored on the spoken text. evals.check() adds its global
    checks to every rule, so they count once, under "no cue brackets or stage directions"."""
    t = " " + spoken(text) + " "
    failed = [name for name, chk in PERSONALITY_RULES if (_rule_fails(t, chk) if chk else evals.check(t, {}))]
    return {"score": round(1 - len(failed) / len(PERSONALITY_RULES), 3), "failed": failed}


def steadiness(text: str) -> float | None:
    """The lowest personality score of any paragraph with substance: a long answer that drifts into a different
    voice halfway through scores low here even when the whole looks fine."""
    paras = [p for p in re.split(r"\n\s*\n", spoken(text)) if len(p.split()) >= 12]
    if len(paras) < 2:
        return None
    return min(personality(p)["score"] for p in paras)


def accuracy(text: str, expect: list[str] | None) -> float | None:
    """1.0 if every expected regex matches (case-insensitive), the share that match otherwise; None without one."""
    if not expect:
        return None
    said = spoken(text)
    hits = sum(1 for rx in expect if re.search(rx, said, re.I))
    return round(hits / len(expect), 3)


def consistency(values: list) -> float | None:
    """Share of repeated runs that agree with the most common outcome (1.0 = every run the same)."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    return round(Counter(vals).most_common(1)[0][1] / len(vals), 3)


def measure(answers: list[str], expect: list[str] | None, ms: list[int], tokens: list[dict],
            helpers: dict | None = None, **extra) -> dict:
    """One mode's metrics from one or more runs of the same task."""
    accs = [accuracy(a, expect) for a in answers]
    pers = [personality(a) for a in answers]
    steady = [s for s in (steadiness(a) for a in answers) if s is not None]
    return {"runs": len(answers),
            "accuracy": round(statistics.mean(accs), 3) if expect and accs else None,
            "consistency": consistency(accs) if expect else None,
            "personality": round(statistics.mean(p["score"] for p in pers), 3) if pers else None,
            "personality_failed": sorted({f for p in pers for f in p["failed"]}),
            "steadiness": round(min(steady), 3) if steady else None,
            "ms": round(statistics.mean(ms)) if ms else None,
            "tokens": {k: round(statistics.mean(t.get(k, 0) for t in tokens)) for k in ("prompt", "reply")} if tokens else {},
            "helpers": helpers or {}, **extra}


def issues_count(issues: list[dict]) -> dict:
    out = {"mistake": 0, "gap": 0, "invented": 0}
    for x in issues or []:
        if x.get("kind") in out:
            out[x["kind"]] += 1
    return out


def summary_line(card: dict) -> str:
    def m(mode: str) -> str:
        d = card["modes"].get(mode)
        if not d:
            return f"{mode} —"
        acc = f"acc {d['accuracy']:.0%} " if d.get("accuracy") is not None else ""
        return f"{mode} {acc}pers {d['personality']:.2f} {d['ms'] / 1000:.0f}s" if d.get("ms") is not None else f"{mode} ({d.get('status', '?')})"
    vote = f" vote {card['vote']}" if card.get("vote") else ""
    who = card.get("task_id") or card["task_sha"]
    return (f"{card['at'][:16]} {card['kind']} {who}: " + " | ".join(m(x) for x in MODES) +
            f" | caught {card['invented_caught']} invented, {card['issues']['mistake']} mistakes{vote}")


def write(card: dict, cards_dir: Path | None = None) -> Path:
    d = cards_dir or CARDS
    d.mkdir(parents=True, exist_ok=True)
    card["summary"] = summary_line(card)
    stem = f"{card['at'][:19].replace(':', '').replace('-', '').replace('T', '-')}-{card['id']}"
    p = d / f"{stem}.json"
    p.write_text(json.dumps(card, indent=1, ensure_ascii=False) + "\n")
    (d / f"{stem}.txt").write_text(card["summary"] + "\n")
    return p


def new_card(text: str, kind: str, did: str, **extra) -> dict:
    """extra may include task=<text> (test-set tasks only; live task text never goes on a card)."""
    return {"card": CARD_VERSION, "id": did, "at": datetime.now().isoformat(timespec="seconds"), "kind": kind,
            "task_sha": hashlib.sha256(text.encode()).hexdigest()[:12], "modes": {}, "vote": None,
            "issues": {"mistake": 0, "gap": 0, "invented": 0}, "invented_caught": 0, "revised": False, **extra}


def vote(did: str, value: str, cards_dir: Path | None = None) -> bool:
    """the owner's 👍/👎 on a daydream answer, saved on its card (the card is written when the comparison is done; a vote
    that comes first is kept in data/daydream/votes.json and applied then)."""
    if value not in ("up", "down"):
        return False
    d = cards_dir or CARDS
    for p in d.glob(f"*-{did}.json"):
        card = json.loads(p.read_text())
        card["vote"] = value
        write(card, d)
        return True
    PRIVATE.mkdir(parents=True, exist_ok=True)
    vp = PRIVATE / "votes.json"
    votes = json.loads(vp.read_text()) if vp.exists() else {}
    votes[did] = value
    vp.write_text(json.dumps(votes))
    return True


def pending_vote(did: str) -> str | None:
    vp = PRIVATE / "votes.json"
    return (json.loads(vp.read_text()) if vp.exists() else {}).get(did)


# ---- live daydreams: compare with Echo alone and Echo + tower, after the owner already has his answer ----
async def compare_live(res, local, merge_prompt: Callable, quiet: Callable[[], Awaitable[None]],
                       cards_dir: Path | None = None) -> dict:
    """Builds the card for a live daydream. Reuses the daydream's plan and the tower's drafts (no second tower or
    Claude call); runs only Echo's own model, and waits for quiet before each call so a conversation never queues
    behind it."""
    card = new_card(res.task, "live", res.id, issues=issues_count(res.issues + res.final_issues),
                    invented_caught=issues_count(res.issues + res.final_issues)["invented"], revised=res.revised)
    card["modes"]["daydream"] = measure([res.answer], None, [res.ms.get("total", 0)], [res.tokens], dict(res.helpers),
                                        issues=card["issues"], revised=res.revised)
    answers = {"daydream": res.answer}

    async def timed(prompt: str, voice: bool = True) -> tuple[str, int, dict]:
        await quiet()
        before, t0 = dict(local.tokens), time.time()
        out = await local.ask(prompt, voice=voice)
        return out, round((time.time() - t0) * 1000), {k: local.tokens[k] - before.get(k, 0) for k in local.tokens}

    a, ms, tok = await timed(res.task)
    card["modes"]["echo"] = measure([a], None, [ms], [tok], {"echo": "used"})
    answers["echo"] = a
    tower = res.drafts.get("tower")
    if tower and any(tower):
        mine = list(res.drafts.get("echo") or [None] * len(res.parts))
        extra_ms, extra_tok = 0, {"prompt": 0, "reply": 0}
        for i, d in enumerate(tower):
            if not d and not (i < len(mine) and mine[i]):
                out, m2, t2 = await timed(f"Overall task: {res.task}\n\nWork on this part only, carefully and "
                                          f"correctly:\n{res.parts[i]}", voice=False)
                mine[i] = out
                extra_ms += m2
                extra_tok = {k: extra_tok[k] + t2.get(k, 0) for k in extra_tok}
        drafts = {"tower": tower} | ({"echo": mine} if any(mine) else {})
        a, ms, tok = await timed(merge_prompt(res.task, res.parts, drafts))
        plan_ms = res.ms.get("plan", 0) + res.ms.get("drafts", 0)   # same plan and tower drafts as the daydream
        card["modes"]["echo_tower"] = measure([a], None, [ms + extra_ms + plan_ms],
                                              [{k: tok[k] + extra_tok[k] for k in tok}], {"tower": "used", "echo": "used"})
        answers["echo_tower"] = a
    else:
        card["modes"]["echo_tower"] = {"status": "tower unavailable", "runs": 0}
    if (v := pending_vote(res.id)):
        card["vote"] = v
    PRIVATE.mkdir(parents=True, exist_ok=True)
    (PRIVATE / f"{res.id}.json").write_text(json.dumps({"task": res.task, "answers": answers, "parts": res.parts},
                                                       indent=1, ensure_ascii=False))
    write(card, cards_dir)
    return card


# ---- trends ----
def load_cards(cards_dir: Path | None = None) -> list[dict]:
    out = []
    for p in sorted((cards_dir or CARDS).glob("*.json")):
        try:
            c = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if c.get("card") == CARD_VERSION:
            out.append(c)
    return out


def report(cards_dir: Path | None = None) -> dict:
    cards = load_cards(cards_dir)

    def agg(cs: list[dict]) -> dict:
        res = {}
        for mode in MODES:
            ds = [c["modes"][mode] for c in cs if c["modes"].get(mode, {}).get("ms") is not None]

            def mean(k):
                v = [d[k] for d in ds if d.get(k) is not None]
                return round(statistics.mean(v), 3) if v else None
            res[mode] = {"n": len(ds), "accuracy": mean("accuracy"), "consistency": mean("consistency"),
                         "personality": mean("personality"), "steadiness": mean("steadiness"),
                         "seconds": round(mean("ms") / 1000, 1) if mean("ms") is not None else None,
                         "reply_tokens": round(statistics.mean(d["tokens"].get("reply", 0) for d in ds)) if ds else None}
        return res
    by_day: dict[str, list[dict]] = {}
    for c in cards:
        by_day.setdefault(c["at"][:10], []).append(c)
    votes = Counter(c["vote"] for c in cards if c.get("vote"))
    helpers = Counter(f"{h}:{s}" for c in cards for h, s in (c["modes"].get("daydream", {}).get("helpers") or {}).items())
    return {"cards": len(cards), "live": sum(c["kind"] == "live" for c in cards),
            "testset": sum(c["kind"] == "testset" for c in cards),
            "overall": agg(cards), "by_day": {d: agg(cs) for d, cs in sorted(by_day.items())},
            "invented_caught": sum(c.get("invented_caught", 0) for c in cards),
            "mistakes_caught": sum(c.get("issues", {}).get("mistake", 0) for c in cards),
            "revised": sum(bool(c.get("revised")) for c in cards),
            "votes": {"up": votes.get("up", 0), "down": votes.get("down", 0)}, "helpers": dict(helpers),
            "latest": [c.get("summary") or summary_line(c) for c in cards[-5:]]}


def report_text(r: dict) -> str:
    """What Echo says for "daydream report": short and spoken, numbers rounded."""
    if not r["cards"]:
        return "I haven't scored any daydreams yet. After the first one, I'll have numbers for you."
    o = r["overall"]

    def say(mode: str, label: str) -> str:
        d = o[mode]
        if not d["n"]:
            return f"{label}: no runs yet"
        acc = f"{d['accuracy']:.0%} right on checkable tasks, " if d["accuracy"] is not None else ""
        return f"{label}: {acc}personality {d['personality']:.0%}, about {d['seconds']:.0f} seconds"
    days = list(r["by_day"])
    trend = ""
    if len(days) >= 2:
        a, b = r["by_day"][days[0]]["daydream"], r["by_day"][days[-1]]["daydream"]
        if a["accuracy"] is not None and b["accuracy"] is not None:
            trend = f" Full daydream accuracy went from {a['accuracy']:.0%} on {days[0]} to {b['accuracy']:.0%} on {days[-1]}."
    return (f"I have {r['cards']} daydream score cards ({r['live']} live, {r['testset']} from the test set). "
            f"{say('daydream', 'Full daydream')}. {say('echo_tower', 'With the tower only')}. "
            f"{say('echo', 'Me alone')}. The checks caught {r['invented_caught']} invented facts and "
            f"{r['mistakes_caught']} mistakes, and I rewrote {r['revised']} answers.{trend} "
            f"Your votes: {r['votes']['up']} up, {r['votes']['down']} down.")
