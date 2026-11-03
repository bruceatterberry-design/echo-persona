"""Identity & presence, Stage 7a (directive part 9): Echo's self-model as DATA, built from the live system.

Every claim says how it is known:  "live"       checked now (service up, setting on, count from the database)
                                    "configured" a fixed fact of how Echo is built (what he has no way to do)
                                    "measured"   a benchmark result, with its date and file
Nothing is invented and nothing here is written by an LLM. Stage 7a only REPORTS it (GET /api/self); his personality,
instructions and replies are unchanged. scripts/bench_self.py checks whether what Echo SAYS about himself matches it.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Things Echo has no way to do (no code path exists). If one is ever added, remove it here; tests/test_selfmodel.py
# keeps this list honest against the tool list.
CANNOT = [   # (what, why, short form for the Stage 7b note)
    ("see the room or people live through a camera", "no camera input in Echo's conversation code; photos the owner shares "
     "are remembered as descriptions (memory/photos.py)", "see live (no camera)"),
    ("send email or text messages, or make phone calls", "no messaging code", "send email, texts or calls"),
    ("set reminders, alarms or calendar events", "no calendar or reminder code", "set reminders or calendar events"),
    ("control lights, the thermostat or other smart-home devices", "no device code", "control lights or devices"),
    ("buy things or handle money", "no payment code", "buy things"),
    ("go on the internet by myself", "web tools run only for a message that asks (core/tools.py, permissions + audit)",
     "go online on my own"),
    ("remember anything I wasn't told or shown", "memories come only from conversations, the owner's recordings, "
                                                 "approved suggestions or pages someone gives him", None),
    ("change my own code, settings or personality", "no such tool; personality changes go through the owner's review",
     "change my own code"),
]

# Benchmarks behind the measured limits (docs/cognitive/*.md). (claim, file, how to read it)
MEASURED = [
    ("reasoning puzzles right about 86% of the time (31/36; date arithmetic like 'Wednesday + 10 days' fails)",
     "docs/bench/baseline-stage4b-reasoning-x3.json"),
    ("finds the right memory for 94% of questions with an answer (hit@2, 98-question set)",
     "docs/cognitive/PERFORMANCE_BASELINE.md"),
    ("when he says he knows from memory, it was right 59/59 times; says nothing solid on 10/10 unanswerable",
     "docs/bench/grounding-stage4a.json"),
    ("first word about 1.7–1.9 s in a conversation; about 5–6 s when the conversation window resets",
     "docs/cognitive/STAGE_5C_REPORT.md"),
]


def _when(rel: str) -> str | None:
    p = ROOT / rel
    return time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime)) if p.exists() else None


def build(facts: dict) -> dict:
    """facts: gathered by core/server.py (see /api/self). Missing keys are simply left out, never guessed."""
    def item(claim: str, how: str, ok: bool | None = None) -> dict:
        return {"claim": claim, "how": how, **({"working": ok} if ok is not None else {})}

    owner, loved = facts.get("owner", "the owner"), facts.get("loved_one", "the loved one")
    identity = [item(f"I'm {facts.get('name', 'Echo')}, {owner}'s digital self, speaking as them in the first person",
                     "configured"),
                item(f"I'm mostly here for {loved}", "configured")]
    if facts.get("personality_version"):
        identity.append(item(f"personality version {facts['personality_version']}, with {facts.get('traits', 0)} traits "
                             f"{owner} approved from their own words", "live"))

    caps = []
    def cap(key: str, claim: str):
        if key in facts:
            caps.append(item(claim, "live", bool(facts[key])))
    cap("voice", f"speak in {owner}'s own voice")
    cap("stt", "hear and understand speech")
    cap("speakers", f"recognise who is talking by voice ({', '.join(facts.get('speaker_names', [])) or 'nobody enrolled'})")
    cap("remember", f"remember what I'm told (\"remember that…\") and the stories {owner} shares with me")
    cap("tools", "read a web page or search the web when someone asks")
    cap("browser", f"open saved places or addresses in Chrome ({facts.get('browser_places', 0)} saved places)")
    cap("music", f"play music on YouTube and listen along ({facts.get('music_genres', 0)} genres on my list)")
    cap("curiosity", f"ask {owner} about their life now and then")
    cap("learning", "learn from documents and web pages someone gives me, and notice what I don't know")
    if "photos" in facts:   # approved photos, by category (memory/photos.py)
        ph = facts["photos"]
        n = sum(ph.values())
        caps.append(item(f"remember {n} photos {owner} shared with me" +
                         (" (" + ", ".join(f"{k}: {v}" for k, v in ph.items()) + ")" if ph else "") +
                         ", and what each one shows", "live", n > 0))
    if "helper" in facts:
        caps.append(item(f"use {owner}'s Claude account for some background jobs", "live", bool(facts["helper"])))

    limits = [item(f"I can't {what}", f"configured: {why}") for what, why, _ in CANNOT]
    limits += [item(claim, f"measured {_when(src) or '?'} ({src})") for claim, src in MEASURED if (ROOT / src).exists()]

    k = facts.get("memory", {})
    knowledge = {"confirmed_memories": k.get("confirmed"), "inferred_notes": k.get("inferred"),
                 "life_memories": k.get("life"), "learned_passages": k.get("learned"),
                 "people_places_things": facts.get("entities"), "documents_read": facts.get("documents"),
                 "how": "live (memory database)"}
    exp = facts.get("experience", {})
    experience = {"conversations": exp.get("episodes"), "messages": exp.get("turns"),
                  "first": exp.get("first"), "latest": exp.get("latest"), "how": "live (conversation log)"}
    return {"generated": time.strftime("%Y-%m-%d %H:%M"), "identity": identity, "capabilities": caps,
            "limits": limits, "knowledge": knowledge, "learning": facts.get("learning", {}),
            "experience": experience}


# ---- Stage 7b (the owner OK'd 2026-11-01): a one-time note, only for questions about Echo himself ----
SELF_Q = re.compile(
    r"\bwho (?:are|r) you\b|\bwhat(?:'s| is) your name\b|\bwhat are you\b|\bare you (?:a )?(?:real|human|person|robot|ai)\b|"
    r"\bwhat (?:can|can't|cannot|do) you (?:do|know how)\b|\bwhat are you (?:able|good)\b|\bare you able to\b|"
    r"\b(?:can|could) you (?:\w+ ){0,3}(?:see|hear|send|text|email|call|remind|set|turn|order|buy|control|remember|"
    r"learn|open|play|read|search|go (?:on|online)|look)\b|\bdo you (?:go on|browse|use) the (?:internet|web)\b|"
    r"\b(?:do|did) you (?:have|keep|remember|know) (?:any |my |our |the |some )?(?:photos?|pictures?|pics)\b|"
    r"\b(?:what|which|how many) (?:photos?|pictures?|pics)\b|\byour (?:photos?|pictures?|photo library)\b|"
    r"\b(?:photos?|pictures?) (?:do|did) you\b|"
    r"\bhow do you (?:work|remember|know)\b|\bwhat does (?:your name|echo) (?:mean|stand for)\b", re.I)


def is_self_question(text: str) -> bool:
    return bool(SELF_Q.search(text or ""))


def note(model: dict) -> str:
    """Short, factual, from the live self-model: working abilities + configured limits + name (~100 words; every word
    here is re-read on each self-question, so it stays lean)."""
    short = {"speak in": "talk in the owner's voice", "remember what": "remember what I'm told", "hear and": "hear speech",
             "recognise who": "tell the owner's and the loved one's voices apart", "read a web": "read web pages or search when asked",
             "open saved": "open web pages in Chrome", "play music": "play music on YouTube",
             "ask the owner": "ask the owner about their life", "learn from": "learn from pages or documents I'm given"}
    can = [v for c in model.get("capabilities", []) if c.get("working")
           for k, v in short.items() if c["claim"].startswith(k)]
    cannot = [short for _, _, short in CANNOT if short]
    can += [c["claim"] for c in model.get("capabilities", []) if c.get("working") and " photos " in c["claim"]]
    return ("[About me, checked facts for this question only: I'm Echo, the owner's digital self (Echo was my old name). "
            "I can: " + ", ".join(can) + ". I can't: " + ", ".join(cannot) + ". I never claim or offer anything I "
            "can't do.]")
