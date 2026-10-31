"""Echo asks the owner about their life, now and then, to fill gaps in its memories.

Rules: only when the voice is recognised as the owner's (never the loved one's or an unknown voice), at most
once every `every_minutes` and after `min_turns` of their messages, or right away if they say "ask me something"
(typing "ask me something" works too; typed messages carry no voice).
Questions come from the same bank as the phone's "Tell me a memory" (collector-android/.../memories.json), from the
time of life Echo knows least about, never repeating one already asked or answered on the phone. The owner's answer
(their next message within 10 minutes) becomes *suggestions* on the My memories page; nothing is kept without review.
State lives in the memory DB's meta table, so it survives restarts.
"""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

from memory.store import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
BANK = ROOT / "collector-android" / "app" / "src" / "main" / "assets" / "memories.json"
ASK_ME = re.compile(r"\bask me (?:a |another |some )?(?:question|questions|something)\b", re.I)
DECLINE = re.compile(r"^\s*(?:no|nope|not now|skip( it)?|pass|i'?d rather not|never ?mind|maybe later)\b", re.I)
ANSWER_WINDOW_S = 600


class Curiosity:
    def __init__(self, store: MemoryStore, enabled: bool = True, every_minutes: float = 30, min_turns: int = 4,
                 bank: Path = BANK, answered: set[str] | None = None):
        self.store, self.enabled = store, enabled
        self.every_s, self.min_turns = every_minutes * 60, min_turns
        self.questions = json.loads(bank.read_text()) if bank.exists() else []
        self.answered = answered or set()   # question ids already answered on the phone

    # ---- state ----
    def state(self) -> dict:
        return json.loads(self.store.get_meta("curiosity", "{}") or "{}")

    def _save(self, st: dict) -> None:
        self.store.set_meta("curiosity", json.dumps(st))

    def pending(self, now: float | None = None) -> dict | None:
        p = self.state().get("pending")
        return p if p and (now or time.time()) - p["at"] < ANSWER_WINDOW_S else None

    def scrub(self, reply: str) -> str:
        """My old reply without the life question I ended it with: the model copied that habit onto unrelated replies
        ("Tell me any memory you'd like me to keep…" after a YouTube request). Only what the model sees changes."""
        parts = re.split(r"(?<=[.?!])\s+", reply.strip())
        drop = {i for i, p in enumerate(parts) if any(asked_in(p, q) for q in self.questions)}
        for i in range(len(parts) - 1):   # questions written as two sentences ("Tell me any memory… Start with when…")
            if i in drop or i + 1 in drop:
                continue
            for q in self.questions:
                if asked_in(parts[i] + " " + parts[i + 1], q) and _shares(parts[i], q) and _shares(parts[i + 1], q):
                    drop |= {i, i + 1}
                    break
        keep = [p for i, p in enumerate(parts) if i not in drop]
        return " ".join(keep) if keep else reply

    # ---- choosing ----
    def next_question(self) -> dict | None:
        st = self.state()
        skip = set(st.get("asked", [])) | self.answered
        left = [q for q in self.questions if q["id"] not in skip]
        if not left:
            return None
        known: dict[str, int] = {}
        for m in self.store.all():
            if m.kind == "memory":
                known[m.period or "unknown"] = known.get(m.period or "unknown", 0) + 1
        for c in self.store.candidates("pending"):
            known[c["period"] or "unknown"] = known.get(c["period"] or "unknown", 0) + 1
        fewest = min(known.get(q["period"], 0) for q in left)
        return random.choice([q for q in left if known.get(q["period"], 0) == fewest])

    def maybe_ask(self, speaker: str | None, text: str, now: float | None = None) -> dict | None:
        """Called for each message. Returns a question to ask after this reply, or None."""
        now = now or time.time()
        forced = bool(ASK_ME.search(text))
        # the owner's voice; or typed (no voice) only when explicitly asked. Never the loved one's or an unknown voice.
        if not self.enabled or not (speaker == "owner" or (speaker is None and forced)):
            return None
        st = self.state()
        st["turns"] = st.get("turns", 0) + 1
        self._save(st)
        if self.pending(now):
            return None
        if not forced and (st["turns"] < self.min_turns or now - st.get("last_asked", 0) < self.every_s):
            return None
        return self.next_question()

    def asked(self, q: dict, now: float | None = None) -> None:
        """The reply really contained the question: wait for the owner's answer."""
        now = now or time.time()
        st = self.state()
        st.update(last_asked=now, turns=0, pending=q | {"at": now})
        st["asked"] = sorted(set(st.get("asked", [])) | {q["id"]})
        self._save(st)

    def take_answer(self, speaker: str | None, text: str, now: float | None = None) -> dict | None:
        """If this message answers my pending question (the owner's voice, or typed), return the question; clears it.
        The loved one's voice or an unknown voice doesn't count as the owner's answer (the question stays open)."""
        q = self.pending(now)
        if not q or speaker not in ("owner", None):
            return None
        st = self.state(); st.pop("pending", None); self._save(st)
        return None if DECLINE.match(text) else q


    # ---- answers wait here until a quiet moment (turning them into suggestions takes the model 15–30 s) ----
    def queue_answer(self, q: dict, text: str, now: float | None = None) -> None:
        st = self.state()
        st.setdefault("answers", []).append({"question": q["text"], "period": q.get("period"), "text": text[:6000],
                                             "source": "chat:" + time.strftime("%Y-%m-%d %H:%M", time.localtime(now or time.time()))})
        self._save(st)

    def pop_answers(self) -> list[dict]:
        st = self.state()
        out = st.pop("answers", [])
        self._save(st)
        return out


def hint(q: dict) -> str:
    return (f"[I'm the owner's digital self, and my memories of their life come from them. I answer them briefly, then END my "
            f"reply by asking them this exact question (I may reword it slightly): \"{q['text']}\" "
            f"No other questions. I never say I have no experiences of my own.]")


STOP_WORDS = set("what was your you the and did tell about how who where when a an of to in it me is are were do "
                 "that this with for on at from".split())


def _shares(sentence: str, q: dict) -> bool:
    """At least 2 of the question's meaningful words, or half of this sentence's: it's part of the question."""
    want = {w for w in re.findall(r"[a-z']+", q["text"].lower()) if w not in STOP_WORDS and len(w) > 2}
    own = {w for w in re.findall(r"[a-z']+", sentence.lower()) if w not in STOP_WORDS and len(w) > 2}
    hit = len(want & own)
    return hit >= 2 or (own and hit / len(own) >= 0.5)


def asked_in(reply: str, q: dict) -> bool:
    """Did the reply really ask this question (not just any question)? ≥ half of its meaningful words (at least 2) with a '?', or ≥ 3/4 without one."""
    want = {w for w in re.findall(r"[a-z']+", q["text"].lower()) if w not in STOP_WORDS and len(w) > 2}
    got = set(re.findall(r"[a-z']+", reply.lower()))
    hit = len(want & got)
    need = 0.5 if "?" in reply else 0.75   # "Tell me about a trip…" asks too, without a question mark
    return bool(want) and hit >= min(2, len(want)) and hit / len(want) >= need
