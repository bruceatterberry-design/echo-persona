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
                 bank: Path = BANK, answered: set[str] | None = None, extra=None, photos=None, dreams=None):
        self.store, self.enabled = store, enabled
        self.every_s, self.min_turns = every_minutes * 60, min_turns
        self.questions = json.loads(bank.read_text()) if bank.exists() else []
        self.answered = answered or set()   # question ids already answered on the phone
        self.extra = extra or (lambda: [])  # questions the owner approved first (memory/gaps.py, Stage 5b)
        self.photos = photos or (lambda: [])  # CA 2.0 Phase 9: uncaptioned photos, best first (core/initiative.py)
        self.dreams = dreams or (lambda: [])  # unknowns my dreams found and I agreed to ask about (memory/dreamq.py)

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
        try:
            first = [q for q in self.extra() if q["id"] not in skip]
        except Exception:  # noqa: BLE001  (learning DB unreadable: just use the bank)
            first = []
        if first:
            return first[0]
        if self.next_kind() != "dream":   # at most every other question is a dream one, so they never crowd out the rest
            try:
                dq = [q for q in self.dreams() if q["id"] not in skip]
            except Exception:  # noqa: BLE001  (dream bridge DB unreadable: just carry on)
                dq = []
            if dq:
                return dq[0]
        left = [q for q in self.questions if q["id"] not in skip]
        if self.next_kind() == "photo" or not left:   # Phase 9: a photo question and a bank question in turn
            try:
                pics = [q for q in self.photos() if q["id"] not in skip]
            except Exception:  # noqa: BLE001  (photo memories unreadable: just use the bank)
                pics = []
            if pics:
                return pics[0]
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

    def next_kind(self) -> str:
        """"photo" if the last question I asked was from the bank (or none yet), else "bank"; "dream" if it was a dream one."""
        last = (self.state().get("last_id") or "")
        if last.startswith("dream:"):
            return "dream"
        return "bank" if last.startswith("photo:") else "photo"

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
        if not forced and not lull(text):   # 2026-11-05: wait for a pause, don't cut across what he just brought up
            return None
        return self.next_question()

    def asked(self, q: dict, now: float | None = None) -> None:
        """The reply really contained the question: wait for the owner's answer."""
        now = now or time.time()
        st = self.state()
        st.update(last_asked=now, turns=0, pending=q | {"at": now}, last_id=q["id"])
        st["asked"] = sorted(set(st.get("asked", [])) | {q["id"]})
        self._save(st)

    def take_answer(self, speaker: str | None, text: str, now: float | None = None) -> dict | None:
        """If this message really answers my pending question, return the question. Either way the question is closed:
        a reply that isn't an answer ("How are you today?", "later") drops it quietly. It's never asked again or saved
        (2026-10-31: "How are you today?" had been taken as the answer, and Echo kept repeating the question)."""
        q = self.pending(now)
        if not q or speaker not in ("owner", None):   # my life questions are for the owner; the loved one's voice leaves it open
            return None
        st = self.state(); st.pop("pending", None)
        ok = looks_like_answer(text) or (is_self_photo(q) and bool(SELF_YES.match(text) or SELF_NO.match(text)))
        if ok:   # CA 2.0 Phase 10: which questions got answers (ids only), for the long-term answer rate
            st["answered_ids"] = sorted(set(st.get("answered_ids", [])) | {q["id"]})
        self._save(st)
        return q if ok else None

    # ---- story threads (the owner, 2026-10-31): after an answer, thoughtful follow-ups about the same story ----
    def start_thread(self, question: str, speaker: str | None, period: str | None = None, now: float | None = None,
                     photo: bool = False, answer: str = "") -> str:
        st = self.state()
        if not photo:   # a photo answer gets one warm reply, no thread of follow-up questions
            st["thread"] = {"question": question, "depth": 1, "who": speaker or "owner", "period": period,
                            "at": now or time.time()}
        self._save(st)
        if photo and SELF_Q.search(question) and SELF_YES.match(answer or "") and speaker in ("owner", None):
            from core import people
            return SELF_PHOTO.format(question=question, who=people.owner() or "the owner")
        return follow_hint(speaker, question, closing=False, photo=photo)

    def follow(self, speaker: str | None, text: str, last_reply: str, now: float | None = None) -> tuple[str, dict] | None:
        """the owner or the loved one answering my last question with a story → (hint for this reply, answer to keep), else None.
        Inside a thread: up to MAX_FOLLOW follow-ups, then a warm close. Outside one: a story-like answer to any
        question I asked starts a thread."""
        now = now or time.time()
        st = self.state()
        th = st.get("thread")
        asked = last_question(last_reply)
        if not asked or speaker not in ("owner", "loved_one", None) or not looks_like_answer(text):
            if th:
                st.pop("thread", None); self._save(st)      # changed the subject: the thread ends
            return None
        if th and now - th["at"] < ANSWER_WINDOW_S:
            th.update(depth=th["depth"] + 1, at=now)
            closing = th["depth"] > MAX_FOLLOW
            if closing:
                st.pop("thread", None)
            else:
                st["thread"] = th
            self._save(st)
            return follow_hint(speaker, asked, closing), {"question": asked, "period": th.get("period")}
        if not STORY.search(text) or len(text.split()) < 10:
            return None
        st["thread"] = {"question": asked, "depth": 1, "who": speaker or "owner", "period": None, "at": now}
        self._save(st)
        return follow_hint(speaker, asked, False), {"question": asked, "period": None}

    def in_thread(self, now: float | None = None) -> bool:
        th = self.state().get("thread")
        return bool(th and (now or time.time()) - th["at"] < ANSWER_WINDOW_S)

    # ---- answers wait here until a quiet moment (turning them into suggestions takes the model 15–30 s) ----
    def queue_answer(self, q: dict, text: str, now: float | None = None, teller: str | None = None) -> None:
        st = self.state()
        who = "loved_one:" if teller == "loved_one" else ""
        st.setdefault("answers", []).append({"question": q.get("text") or q.get("question", ""), "period": q.get("period"),
                                             "text": text[:6000], "source": who + "chat:" +
                                             time.strftime("%Y-%m-%d %H:%M", time.localtime(now or time.time()))})
        self._save(st)

    def requeue(self, answers: list[dict]) -> None:
        """Answers taken by pop_answers() but not processed (the background job was interrupted): put them back."""
        if answers:
            st = self.state()
            st["answers"] = answers + st.get("answers", [])
            self._save(st)

    def pop_answers(self) -> list[dict]:
        st = self.state()
        out = st.pop("answers", [])
        self._save(st)
        return out


MAX_FOLLOW = 2   # follow-up questions per story, then a warm close
STORY = re.compile(r"\b(?:i|we|my|our|me|us)\b.{0,80}\b(?:went|was|were|had|used to|remember|grew|took|drove|met|lived|got|"
                   r"saw|visited|played|worked|caught|stayed|loved|did)\b", re.I | re.S)
_NOT_ANSWER = re.compile(r"^\W*(?:(?:well|so|um|uh|oh|yeah|okay|ok)[,.]?\s+)?(?:how are you|what(?:'s| is) up|hello|hi|hey|okay|ok|thanks?|thank you|later|not now|"
                         r"we'?ll do that|let'?s do that|maybe later|i'?m (?:just )?testing|never ?mind|stop|wait)\b", re.I)


def looks_like_answer(text: str) -> bool:
    """A real reply to my question, not a greeting, a deflection or a question back."""
    t = (text or "").strip()
    if DECLINE.match(t) or _NOT_ANSWER.match(t):
        return False
    if t.endswith("?") and len(t.split()) < 12:
        return False
    return len(t.split()) >= 5


def last_question(reply: str) -> str | None:
    """The question my last reply ended with, if it did."""
    parts = [p.strip() for p in re.split(r"(?<=[.?!])\s+", (reply or "").strip()) if p.strip()]
    for p in reversed(parts[-2:]):
        if p.endswith("?") or re.match(r"(?i)^(?:tell me|describe|share|walk me through|talk to me about)\b", p):
            return p      # "Tell me about a trip…" asks too, without a question mark
    return None


# CA 2.0 Phase 9 (scripts/eval_initiative.py, 4 runs each): after the owner answered a PHOTO question, the follow-up
# question brought in things neither he nor the photo mentioned ("the apple pie your mom made for us", "the
# music we listened to", "that grilled steak we talked about earlier") — 3/4 runs, still 2/4 with a stricter note. So
# for photos: no follow-up question; a warm, specific reply to what he said, and the answer is kept for review.
PHOTO_CLOSE = ("[{who} just answered my question about a photo (\"{question}\"). I respond warmly and specifically to "
               "what {who} actually said, using only {who}'s words and what the photo shows, and thank {who} for telling "
               "me. I don't ask another question this time, I don't bring in anything else, and I never say \"we\" or "
               "\"us\" as if I had been there.]")


# 2026-11-03: when the owner confirms a photo is of them, Echo must understand that it shows the owner AND, because
# Echo is the owner's digital self, it is also Echo. A plain "Yes, that's me" counts as an answer too.
SELF_Q = re.compile(r"\bis that you\b", re.I)
SELF_YES = re.compile(r"^\W*(?:yes|yeah|yep|yup|sure is|it sure is|that'?s me|that is me|it'?s me|it is me|that was me|"
                      r"i am|uh-?huh)\b", re.I)
SELF_NO = re.compile(r"^\W*(?:no|nope|not me|that'?s not me|that isn'?t me)\b", re.I)
SELF_PHOTO = ("[{who} just told me the photo is of them (my question: \"{question}\"). So the photo shows {who} — and "
              "because I'm {who}'s digital self, it's a picture of me too, in that sense. I say both clearly and warmly, "
              "in my own words: it's {who} in the photo, and as their digital self that's me as well. {who} is the one "
              "who lived that moment; I know it only from what they tell me, so I don't claim to remember it. I use only "
              "their words and what the photo shows, thank them, and don't ask another question.]")


def lull(text: str) -> bool:
    """A pause in the conversation where my own question fits: a short message (≤ 8 words) that isn't a question and
    doesn't start something ("I would like to make an app for you, Echo, so that you can see the world." got a photo
    question about a man in a green jacket on 11-05)."""
    t = (text or "").strip()
    return "?" not in t and len(t.split()) <= 8 and not re.search(
        r"\b(?:i(?:'d| would) like|i want|i'm going to|i am going to|we(?:'re| are) going to|let's|i'm thinking|"
        r"i was thinking|my plan|i plan|tell me|can you|could you|will you|would you)\b", t, re.I)


def is_self_photo(q: dict) -> bool:
    return str(q.get("id", "")).startswith("photo:") and bool(SELF_Q.search(q.get("text", "")))


def follow_hint(speaker: str | None, question: str, closing: bool, photo: bool = False) -> str:
    who = "the loved one" if speaker == "loved_one" else "the owner"
    if photo:
        return PHOTO_CLOSE.format(who=who, question=question)
    return _follow_hint(who, question, closing)


def _follow_hint(who: str, question: str, closing: bool) -> str:
    if closing:
        return (f"[{who} told me more about this story. I respond warmly and specifically to what {who} just said, add one "
                f"genuine thought about the topic, and thank {who} for sharing it. I don't ask another question this time.]")
    return (f"[{who} just answered my question \"{question}\" with a real story. I respond warmly to what {who} actually said: "
            f"I pick up one or two specific details from the answer and add something genuine I know about that topic (the "
            f"place, the era, the activity), in a sentence or two. I never invent details about {who}'s life or say I was "
            f"there. Then I END with ONE thoughtful follow-up question about this same story, as specific as I can make it "
            f"from what I know about the topic (a ride, a landmark, a dish, the weather, a person who was there, how it "
            f"felt) rather than a general \"what do you remember most\". I don't ask about a different trip or memory, "
            f"and I don't repeat my earlier question.]")


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
    # a photo question: Echo retells the description and the question in his own words (CA 2.0 Phase 9,
    # eval_initiative: 1 of 6 missed by word overlap) — asked = the reply mentions the photo and asks something
    if str(q.get("id", "")).startswith("photo:"):
        return "?" in reply and bool(re.search(r"\b(?:photo|picture|pic|snapshot)\b", reply, re.I))
    want = {w for w in re.findall(r"[a-z']+", q["text"].lower()) if w not in STOP_WORDS and len(w) > 2}
    got = set(re.findall(r"[a-z']+", reply.lower()))
    hit = len(want & got)
    need = 0.5 if "?" in reply else 0.75   # "Tell me about a trip…" asks too, without a question mark
    return bool(want) and hit >= min(2, len(want)) and hit / len(want) >= need
