"""EDIM, the Echo Dialogue Intelligence Manager (Phase 2, ISOLATED: nothing in server.py calls this yet).

Keeps track of the conversation itself: which topics (threads) are open, paused, finished or dropped; what was said as
fact and what was only a guess; who said it; which questions are still open; what "it" / "those" points at; and what
kind of reply fits (an advisory plan: it can add one short note, it never writes the reply). Design:
docs/cognitive/edim/PHASE1_DESIGN.md.

Rules only, no model call, no network, no memory-database access: the memory DB stays the only store of what Echo
knows. Reuses Echo's existing detectors (controller questions, executive follow-ups / small talk, metacog challenges,
social brush-offs). State is a small dict saved in the `dialogue` section of data/state.json (core/state.py). Every
inferred item carries a confidence and the rule that produced it; time alone never closes or drops a thread.
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field

from core import controller, executive

VERSION = 1
CAPS = {"threads": 20, "facts": 30, "questions": 20, "refs": 10, "history": 10, "keys": 24}
TEXT_MAX = 80
KEEP_S = 7 * 86400              # session phrases are deleted after this (Phase 1 decision Q4)
SESSION_GAP_S = 20 * 60         # same gap as memory/archive.py episodes
RETURN_MIN, RETURN_MARGIN = 0.5, 0.2
STATES = ("active", "suspended", "completed", "abandoned", "evicted")

# ---------- words ----------
_STOP = set("""a an the and or but if so of to in on at by for with from into about as is are was were be been being am
do does did done have has had having i me my mine we us our you your he him his she her it its they them their this that
these those there here what which who whom whose when where why how not no yes just really very quite mostly also too
can could would should will shall may might must one ones thing things some any all more most much many lot lots get got
gets getting go going went make made like well okay ok oh um uh yeah yep now then than still even again way kind sort
i'm i've i'll i'd it's that's what's there's let's don't doesn't didn't can't won't isn't aren't wasn't you're we're
seem seems seemed keep keeps kept sorry meant mean sound sounds good great nice cool fine thanks thank""".split())
_WORD = re.compile(r"[a-z0-9][a-z0-9'+.-]*")


def stem(w: str) -> str:
    w = w.lower().strip("'.-+").replace("’", "'")
    if w.endswith("'s"):
        w = w[:-2]
    for suf in ("ing", "ed", "es", "s", "ly"):
        if len(w) > len(suf) + 2 and w.endswith(suf) and not w.endswith("ss"):
            w = w[: -len(suf)]
            break
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


def keys(text: str) -> list[str]:
    out: list[str] = []
    for w in _WORD.findall((text or "").lower().replace("’", "'")):
        if w in _STOP or len(w) < 3 and not w.isdigit():
            continue
        s = stem(w)
        if s and s not in _STOP and s not in out:
            out.append(s)
    return out


# things "it" can point at: a word after a determiner (skipping one adjective), or a Capitalised word mid-sentence
_DET = re.compile(r"\b(?:a|an|the|my|our|your|his|her|their|this|that|those|these)\s+(?:(?:new|old|other|big|small|"
                  r"little|second|first|last)\s+)?([A-Za-z][A-Za-z0-9-]+)", re.I)
_CAP = re.compile(r"(?<=[a-z,] )([A-Z][A-Za-z0-9-]+)")
_NOT_THING = set("way time lot bit day week weekend morning night moment while idea reason problem monday tuesday "
                 "wednesday thursday friday saturday sunday january february march april june july august september "
                 "october november december".split())
_JOINED = re.compile(r"\b(?:a|an|the|my|our)\s+(?:\w+\s+)?\w+\s+(?:and|or)\s+(?:a|an|the|my|our)\b", re.I)
THANKS = re.compile(r"^\W*(?:thanks|thank\s+you|thx|much\s+appreciated|i\s+appreciate\s+(?:it|that|the))\b", re.I)


def things(text: str) -> list[str]:
    found: list[str] = []
    for m in list(_DET.finditer(text or "")) + list(_CAP.finditer(text or "")):
        w = m.group(1)
        s = stem(w)
        if s in _STOP or w.lower() in _NOT_THING or s in [stem(x) for x in found]:
            continue
        found.append(w)
    return found


# ---------- intents ----------
_R = lambda p: re.compile(p, re.I)  # noqa: E731
CORRECTION = _R(r"^\W*(?:sorry,?\s+)?(?:i\s+meant|what\s+i\s+meant|i\s+mean\b|actually,?\s+(?:it'?s|it\s+is|it\s+was|i|we|"
                r"that)|no,?\s+(?:it'?s|it\s+is|wait|i\s+meant)|correction|let\s+me\s+correct|scratch\s+that)")
CLARIFY = _R(r"^\W*(?:i\s+mean\b|what\s+i'?m\s+saying|to\s+be\s+clear|i\s+was\s+talking\s+about)")
CONFIRM = _R(r"^\W*(?:yes|yeah|yep|right|exactly|correct|that'?s\s+right|true|sure|okay|ok|uh-?huh)\b[\s.!]*$")
DISAGREE = _R(r"^\W*(?:no\b(?!,?\s+(?:it'?s|wait|i\s+meant))|nope|i\s+don'?t\s+think\s+so|that'?s\s+not|i\s+disagree|not\s+really)")
EMOTION = _R(r"\b(?:i(?:\s+feel|\s+felt|\s+am|'m)\s+(?:so\s+|really\s+|very\s+)?(?:sad|happy|worried|scared|angry|upset|lonely|"
             r"tired|anxious|excited|proud|frustrated|stressed|grateful)|passed\s+away|died|miss\s+(?:him|her|them)|"
             r"broke\s+my\s+heart|i'?m\s+(?:so\s+)?(?:sad|worried|scared|upset))\b")
HUMOR = _R(r"\b(?:lol|haha+|ha ha|lmao|just\s+kidding|jk)\b|😂|🤣")
SARCASM = _R(r"^\W*(?:well,?\s+)?(?:that|this)\s+(?:worked\s+out|went)\s+(?:great|well|perfectly)|^\W*oh\s+(?:great|perfect)\b")
SPECULATE = _R(r"\b(?:maybe|perhaps|i\s+(?:think|guess|suspect|wonder)|might\s+be|could\s+be|probably|what\s+if)\b")
CHANGE = _R(r"^\W*(?:by\s+the\s+way|anyway|anyhow|on\s+another\s+note|changing\s+the\s+subject|different\s+(?:topic|"
            r"question)|unrelated|quick\s+question|oh,?\s+and|also,?\s+(?:i|what|can|do))")
RETURN = _R(r"\b(?:back\s+to|getting\s+back|about\s+(?:that|the)\s+\w+\s+(?:from\s+)?(?:earlier|before)|as\s+i\s+(?:said|"
            r"mentioned)|(?:what|the\s+thing)\s+we\s+(?:discussed|talked\s+about)|earlier\s+you|i\s+checked\s+(?:those|"
            r"that|the|it))\b")
REQUEST = _R(r"^\W*(?:please\b|can\s+you|could\s+you|would\s+you|will\s+you|let'?s\b|open|play|show|tell\s+me|read|"
             r"remember|look\s+up|find|help\s+me)")
CLOSING = _R(r"\b(?:that'?s\s+all|goodbye|good\s*bye|bye|good\s*night|talk\s+(?:to\s+you\s+)?later|stop\s+listening|"
             r"see\s+you)\b[\s.!]*$")
DONE = _R(r"\b(?:that\s+(?:fixed|solved|did)\s+it|(?:it'?s|it\s+is)\s+(?:fixed|working(?:\s+now)?|solved|sorted)|"
          r"(?:works|working)\s+(?:now|again|fine)|problem\s+solved|all\s+(?:good|set|fixed)\s+now|figured\s+it\s+out)\b")
REOPEN = _R(r"\b(?:again|it'?s\s+back|came\s+back|started\s+(?:up\s+)?again|still\s+(?:doing|happening)|didn'?t\s+fix)\b")
DROP = _R(r"\b(?:forget\s+(?:it|that|about\s+(?:it|that))|drop\s+(?:it|that)|never\s*mind\s+(?:that|the|about)|"
          r"let'?s\s+not\s+talk\s+about)\b")
_NUM_UNIT = re.compile(r"\b(\d+(?:\.\d+)?)\s*([a-zA-Z%°]+)\b")


@dataclass
class Intent:
    type: str
    conf: float
    why: list[str] = field(default_factory=list)


def intents(text: str, echo_asked: bool = False) -> list[Intent]:
    """Every conversational intent the message shows, with confidence; never empty."""
    t = (text or "").strip()
    out: list[Intent] = []

    def add(kind: str, conf: float, why: str):
        out.append(Intent(kind, conf, [why]))

    question = controller._is_question(t)
    if CORRECTION.search(t):
        add("correction", 0.85, "correction_phrase")
    if CLOSING.search(t) and len(t.split()) <= 8:
        add("closing", 0.8, "closing_phrase")
    if question:
        add("question", 0.9 if "?" in t else 0.7, "question_mark" if "?" in t else "question_word")
    if REQUEST.search(t) and not question:
        add("request", 0.75, "request_opening")
    elif REQUEST.search(t):
        add("request", 0.6, "polite_question_request")
    if CONFIRM.search(t):
        add("confirmation", 0.8 if echo_asked else 0.6, "short_yes")
    if DISAGREE.search(t):
        add("disagreement", 0.7, "disagree_phrase")
    if CLARIFY.search(t):
        add("clarification", 0.7, "clarify_phrase")
    if EMOTION.search(t):
        add("emotional_disclosure", 0.75, "feeling_words")
    if HUMOR.search(t):
        add("humor", 0.7, "laugh_marker")
    if SARCASM.search(t):
        add("humor", 0.4, "possible_sarcasm")      # sincere or sarcastic: context decides; reported as uncertain
    if SPECULATE.search(t):
        add("speculation", 0.7, "hedge_word")
    if CHANGE.search(t):
        add("topic_change", 0.8, "change_phrase")
    if RETURN.search(t):
        add("topic_return", 0.7, "return_phrase")
    is_ref, why = executive.referential(t)
    if is_ref:
        add("follow_up", 0.6, why)
    if not question and not any(i.type in ("request", "confirmation", "closing", "humor") for i in out) \
            and len(keys(t)) >= 2:
        add("information_sharing", 0.8, "statement")
    if executive.small_talk(t) or (THANKS.search(t) and len(t.split()) <= 8) or (len(keys(t)) == 0 and len(t.split()) <= 4 and not question):
        out = [i for i in out if i.type != "information_sharing"]
        if not any(i.type in ("confirmation", "closing", "humor") for i in out):
            add("acknowledgement", 0.7, "small_talk")
    if not out:
        add("information_sharing", 0.5, "fallback")
    return out


# ---------- the state ----------
@dataclass
class Update:
    """What one observed message changed (ids and rule names; for the turn record and the planner)."""
    turn: int
    intents: list[dict]
    thread: str | None = None
    transitions: list[list] = field(default_factory=list)   # [thread, from, to, why]
    ref: dict | None = None
    ambiguous: list[str] = field(default_factory=list)
    facts_added: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)
    plan: dict = field(default_factory=dict)
    ms: float = 0.0

    def record(self) -> dict:
        """Turn-record form: no words from the conversation."""
        d = asdict(self)
        d["ref"] = {"target": self.ref["target"], "conf": self.ref["conf"]} if self.ref else None
        d["ambiguous"] = len(self.ambiguous)
        d["plan"] = {k: v for k, v in self.plan.items() if k != "note"}
        return d


def _empty(now: float) -> dict:
    return {"v": VERSION, "at": now, "session_id": f"s-{int(now)}", "turn": 0, "speaker": None, "mode": "conversation",
            "threads": [], "facts": [], "questions": [], "refs": [], "last_action": None, "echo_q_streak": 0,
            "next_id": 1, "recent": []}


class Dialogue:
    def __init__(self, data: dict | None = None, clock=time.time):
        self.clock = clock
        self.problems: list[str] = []
        self.d = validate(data, clock(), self.problems) if data else _empty(clock())

    # ----- helpers -----
    def _id(self, prefix: str) -> str:
        n = self.d["next_id"]
        self.d["next_id"] = n + 1
        return f"{prefix}{n}"

    def thread(self, tid: str | None) -> dict | None:
        return next((t for t in self.d["threads"] if t["id"] == tid), None)

    def active(self) -> dict | None:
        return next((t for t in reversed(self.d["threads"]) if t["state"] == "active"), None)

    def _move(self, th: dict, to: str, why: str, up: Update) -> None:
        if th["state"] == to:
            return
        up.transitions.append([th["id"], th["state"], to, why])
        th["state"] = to
        th["history"] = (th["history"] + [[to, self.clock(), why]])[-CAPS["history"]:]

    def _new_thread(self, text: str, ks: list[str], speaker: str, up: Update, parent: str | None = None) -> dict:
        label = " ".join(ks[:6])[:40] or (text or "")[:40]
        th = {"id": self._id("t"), "label": label, "state": "active", "parent": parent, "speaker": speaker,
              "opened": self.clock(), "last": self.clock(), "turns": [self.d["turn"], self.d["turn"]],
              "keys": ks[: CAPS["keys"]], "things": [], "salience": 0.6, "history": [["active", self.clock(), "new_topic"]]}
        self.d["threads"].append(th)
        up.transitions.append([th["id"], None, "active", "new_topic"])
        return th

    def _open_questions(self, tid: str, by: str | None = None) -> list[dict]:
        return [q for q in self.d["questions"] if q["thread"] == tid and q["state"] == "open"
                and (by is None or q["asked_by"] == by)]

    def _score(self, th: dict, ks: list[str], text: str) -> float:
        """How strongly this message points back at a (not active) thread."""
        kset = set(ks)
        s = 0.25 * min(len(kset & set(th["keys"])), 3)
        if any(kset & set(keys(q["text"])) for q in self._open_questions(th["id"])):
            s += 0.5
        if RETURN.search(text) and kset & (set(th["keys"]) | {k for q in self._open_questions(th["id"]) for k in keys(q["text"])}):
            s += 0.3
        return s + 0.1 * th.get("salience", 0)

    # ----- reference resolution -----
    def resolve(self, text: str, speaker: str) -> tuple[dict | None, list[str]]:
        """What "it"/"that" points at: the newest earlier message that names exactly one thing (same speaker first).
        Returns (ref, ambiguous candidates)."""
        named = things(text)
        if named and RETURN.search(text) or (named and re.search(r"\b(?:those|these|that|the)\s+" + re.escape(named[0]),
                                                                    text, re.I) and len(keys(text)) <= 3):
            # "those temperatures": the phrase names its own subject → the thread whose words or open questions have it
            want = {stem(n) for n in named}
            hits = [t for t in reversed(self.d["threads"]) if t["state"] in ("active", "suspended") and
                    want & (set(t["keys"]) | {k for q in self._open_questions(t["id"]) for k in keys(q["text"])})]
            if len(hits) == 1:
                return {"phrase": named[0], "thing": named[0], "target": {"kind": "thread", "id": hits[0]["id"]},
                        "conf": 0.8, "turn": self.d["turn"]}, []
            if len(hits) > 1:
                return None, [h["label"] for h in hits[:3]]
        is_ref, _ = executive.referential(text)
        if not is_ref:
            return None, []
        recent = self.d["recent"]
        for pass_speaker in (speaker, None):
            for r in reversed(recent):
                if pass_speaker and r["speaker"] != pass_speaker:
                    continue
                cands = [c for c in r["things"] if stem(c) not in keys(text)]
                if len(cands) == 1:
                    return {"phrase": "it", "thing": cands[0], "target": {"kind": "thread", "id": r["thread"]},
                            "conf": 0.8 if pass_speaker else 0.6, "turn": self.d["turn"]}, []
                if len(cands) > 1 and r.get("joined"):
                    return None, cands                       # "a keyboard and a mouse … it": really two candidates
                if len(cands) > 1 and r.get("thread"):
                    return {"phrase": "it", "thing": None, "target": {"kind": "thread", "id": r["thread"]},
                            "conf": 0.5, "turn": self.d["turn"]}, []
        return None, []

    # ----- the main entry -----
    def observe_user(self, text: str, speaker: str | None = None, speaker_conf: float = 1.0, route: str = "llm",
                     how: str = "voice") -> Update:
        t0 = time.perf_counter()
        now = self.clock()
        if now - self.d["at"] > SESSION_GAP_S and self.d["turn"]:
            self.end_session(now)
        d = self.d
        d["turn"] += 1
        d["at"] = now
        spk = speaker or "unknown"
        d["speaker"] = {"name": spk, "conf": round(speaker_conf, 2), "how": how if speaker else "unknown"}
        its = intents(text, echo_asked=d["echo_q_streak"] > 0)
        kinds = {i.type for i in its}
        up = Update(turn=d["turn"], intents=[asdict(i) for i in its])
        ks = keys(text)
        act = self.active()

        ref, amb = self.resolve(text, spk)
        up.ambiguous = amb
        target = None
        if ref:
            up.ref = ref
            d["refs"] = (d["refs"] + [ref])[-CAPS["refs"]:]
            target = self.thread(ref["target"]["id"])

        # which thread is this message part of?
        th = act
        if "closing" in kinds or ("correction" in kinds and act is not None):
            pass
        elif DROP.search(text) and act:
            self._move(act, "abandoned", "dropped_by_person", up)
            th = None
        elif REOPEN.search(text) and (done := [x for x in d["threads"] if x["state"] == "completed"
                                                and (set(ks) & set(x["keys"]) or (target is x))]):
            th = done[-1]
            self._move(th, "active", "reopened", up)
        else:
            cands = [x for x in d["threads"] if x["state"] == "suspended" and x is not act]
            scored = sorted(((self._score(x, ks, text) + (0.5 if target is x else 0), x) for x in cands),
                            key=lambda p: p[0], reverse=True)
            best = scored[0] if scored else (0, None)
            nxt = scored[1][0] if len(scored) > 1 else 0
            act_overlap = len(set(ks) & set(act["keys"])) if act else 0
            if best[1] is not None and best[0] >= RETURN_MIN and best[0] - nxt >= RETURN_MARGIN \
                    and best[0] > 0.25 * act_overlap + (0.5 if target is act else 0):
                th = best[1]
                self._move(th, "active", "topic_return", up)
            elif target is not None and target is act:
                th = act
            elif target is not None and target["state"] == "completed":
                th = act or target      # "it" points at a finished topic: no reopening without a reopen cue
            elif act is None or ("topic_change" in kinds and act_overlap == 0):
                if len(ks) >= 1 and not kinds <= {"confirmation", "acknowledgement", "humor"}:
                    th = self._new_thread(text, ks, spk, up)
            elif act_overlap == 0 and len(ks) >= 2 and "follow_up" not in kinds \
                    and not (act and self._open_questions(act["id"], "echo") and "question" not in kinds) \
                    and not ({"information_sharing"} >= kinds - {"speculation"} and act["speaker"] == spk
                             and act["turns"][1] >= d["turn"] - 1 and len(ks) < 3):
                th = self._new_thread(text, ks, spk, up)
            elif act and act["speaker"] != spk and act_overlap == 0 and len(ks) >= 2:
                th = self._new_thread(text, ks, spk, up)
        if th is not None and act is not None and th is not act and act["state"] == "active":
            self._move(act, "suspended", "other_topic", up)

        if th is not None:
            up.thread = th["id"]
            th["last"], th["turns"][1] = now, d["turn"]
            th["keys"] = (th["keys"] + [k for k in ks if k not in th["keys"]])[-CAPS["keys"]:]
            th["salience"] = min(1.0, round(th["salience"] * 0.9 + 0.2, 3))
            # Echo's open question in this thread is answered by a statement
            if "question" not in kinds and (oq := self._open_questions(th["id"], "echo")):
                oq[-1].update(state="answered", answered_turn=d["turn"])
            if DONE.search(text):
                self._move(th, "completed", "resolved_by_person", up)
                for q in self._open_questions(th["id"]):
                    q["state"] = "dropped"
        for x in d["threads"]:
            if x is not th and x["state"] != "active":
                x["salience"] = round(x["salience"] * 0.95, 3)

        # facts, guesses, corrections, questions
        tid = th["id"] if th else None
        if "correction" in kinds:
            self._correct(text, spk, speaker_conf, tid, up)
        elif "information_sharing" in kinds and "question" not in kinds:
            kind = "assumption" if "speculation" in kinds else "fact"
            self._add_fact(text, spk, speaker_conf, tid, kind, up)
        if "question" in kinds:
            d["questions"].append({"id": self._id("q"), "text": _short(text), "asked_by": spk, "thread": tid,
                                   "state": "open", "asked_turn": d["turn"], "answered_turn": None, "at": now})
        d["recent"] = (d["recent"] + [{"turn": d["turn"], "speaker": spk, "thread": tid, "things": things(text),
                                     "joined": bool(_JOINED.search(text))}])[-8:]
        if th is not None:
            th["things"] = (th["things"] + [x for x in things(text) if x not in th["things"]])[-8:]
        self._cap()
        if "emotional_disclosure" in kinds:
            d["emotion_turn"] = d["turn"]
        up.plan = self.plan(up, kinds, th)
        d["last_action"] = up.plan["action"]
        up.ms = round((time.perf_counter() - t0) * 1000, 3)
        return up

    def observe_reply(self, text: str) -> None:
        """Echo's reply: questions he asked are now open; the person's open question in the thread is answered."""
        d, th = self.d, self.active()
        tid = th["id"] if th else None
        asked = [s.strip() for s in re.findall(r"[^.!?]*\?", text or "") if len(s.strip()) > 3]
        for q in asked[-2:]:
            d["questions"].append({"id": self._id("q"), "text": _short(q), "asked_by": "echo", "thread": tid,
                                   "state": "open", "asked_turn": d["turn"], "answered_turn": None, "at": self.clock()})
        if th is not None and asked:
            th["keys"] = (th["keys"] + [k for q in asked for k in keys(q) if k not in th["keys"]])[-CAPS["keys"]:]
        d["echo_q_streak"] = d["echo_q_streak"] + 1 if asked else 0
        d["echo_q_recent"] = (d.get("echo_q_recent", []) + [bool(asked)])[-3:]
        stated = re.sub(r"[^.!?]*\?", "", text or "").strip()
        if stated:
            for q in d["questions"]:
                if q["state"] == "open" and q["asked_by"] != "echo" and q["asked_turn"] == d["turn"]:
                    q.update(state="answered", answered_turn=d["turn"])
        self._cap()

    def _add_fact(self, text, spk, sconf, tid, kind, up, supersedes=None) -> dict:
        f = {"id": self._id("f"), "text": _short(text), "speaker": spk, "speaker_conf": round(sconf, 2),
             "conf": 0.9 if kind == "fact" else 0.5, "source": "said" if kind == "fact" else "inferred", "kind": kind,
             "turn": self.d["turn"], "thread": tid, "status": "current", "supersedes": supersedes,
             "superseded_by": None, "at": self.clock()}
        self.d["facts"].append(f)
        up.facts_added.append(f["id"])
        return f

    def _correct(self, text, spk, sconf, tid, up) -> None:
        """'Sorry, I meant 32 GB': the newest matching fact by the same person is superseded, never overwritten."""
        mine = [f for f in self.d["facts"] if f["speaker"] == spk and f["status"] == "current"][-3:]
        body = CORRECTION.sub("", text, count=1).strip(" ,.-")
        new_val = _NUM_UNIT.search(body)
        old = None
        for f in reversed(mine):
            m = _NUM_UNIT.search(f["text"])
            if new_val and m and m.group(2).lower() == new_val.group(2).lower():
                old, new_text = f, f["text"][: m.start()] + new_val.group(0) + f["text"][m.end():]
                break
            if set(keys(body)) & set(keys(f["text"])):
                old, new_text = f, body
                break
        if old is None and mine and len(keys(body)) <= 3:
            old, new_text = mine[-1], body
        if old is None:
            if len(keys(body)) >= 2:
                self._add_fact(body, spk, sconf, tid, "fact", up)
            return
        nf = self._add_fact(new_text, spk, sconf, old["thread"] or tid, "fact", up, supersedes=old["id"])
        old.update(status="superseded", superseded_by=nf["id"])
        up.superseded.append(old["id"])

    # ----- planner (advisory) -----
    def plan(self, up: Update, kinds: set[str], th: dict | None) -> dict:
        def p(action: str, why: str, note: str | None = None) -> dict:
            return {"action": action, "why": why, "note": note}
        if "closing" in kinds:
            return p("CLOSE_CONVERSATION", "closing_phrase")
        if up.ambiguous:
            opts = " or ".join(up.ambiguous[:3])
            return p("ASK_CLARIFICATION", "ambiguous_reference",
                     f"[Conversation note: \"it\" could mean {opts}. I ask which one, briefly, instead of guessing.]")
        if any(tr[3] == "topic_return" for tr in up.transitions) and th:
            return p("RESUME_THREAD", "topic_return", self._thread_note(th, "This goes back to what we were talking "
                                                                             "about earlier"))
        if any(tr[3] == "reopened" for tr in up.transitions) and th:
            return p("RESUME_THREAD", "reopened", self._thread_note(th, "This problem is back after it was solved"))
        if up.superseded:
            old = next((f for f in self.d["facts"] if f["id"] == up.superseded[-1]), None)
            new = next((f for f in self.d["facts"] if f["id"] == up.facts_added[-1]), None) if up.facts_added else None
            if old and new:
                return p("RESPOND", "correction", f"[Conversation note: they corrected themselves: \"{new['text']}\" "
                                                  f"replaces \"{old['text']}\". I use the corrected version.]")
        if any(i["type"] == "humor" and i["conf"] < 0.5 for i in up.intents):
            return p("RESPOND", "possible_sarcasm", "[Conversation note: this may be meant sarcastically; I don't take "
                                                    "it as plain good news.]")
        if "topic_change" in kinds:
            return p("CHANGE_TOPIC", "change_phrase")
        if "question" in kinds and (said := self.said_before(up)):
            return p("RESPOND", "answer_said_earlier", "[Conversation note: earlier in this conversation they said: "
                     + "; ".join(said) + ". I answer from that.]")
        if "question" in kinds or "request" in kinds:
            return p("RESPOND", "question_or_request")
        if self.d["echo_q_streak"] >= 2:
            return p("ACKNOWLEDGE", "asked_twice_already", "[Conversation note: I've asked questions in my last replies; "
                                                          "this time I respond without asking another.]")
        if kinds & {"confirmation", "acknowledgement"} and not kinds & {"information_sharing", "correction"}:
            return p("ACKNOWLEDGE", "short_reply")
        if any(tr[2] == "completed" for tr in up.transitions):
            return p("ACKNOWLEDGE", "thread_completed")
        if th and self._open_questions(th["id"], "echo"):
            return p("ASK_FOLLOWUP", "open_question_remains")
        return p("CONTINUE", "in_thread" if th else "no_thread")

    # ----- Echo's own curiosity question (memory/curiosity.py): is this a good moment for it? -----
    HOLD_ACTIONS = {"ASK_CLARIFICATION": "clarifying", "RESUME_THREAD": "topic_return", "CLOSE_CONVERSATION": "closing",
                    "ASK_FOLLOWUP": "open_question_in_topic"}

    def may_ask_own(self, up: Update) -> tuple[bool, str]:
        """(ok, reason). Curiosity already waits for a short, non-question message (curiosity.lull); this adds what the
        conversation itself shows. Rules only; the question is held, not dropped: it comes at a later pause."""
        kinds = {i["type"] for i in up.intents}
        act = up.plan.get("action")
        if act in self.HOLD_ACTIONS:
            return False, self.HOLD_ACTIONS[act]
        if up.plan.get("why") in ("correction", "answer_said_earlier", "possible_sarcasm"):
            return False, up.plan["why"]
        for k in ("emotional_disclosure", "correction", "disagreement", "topic_return", "clarification"):
            if k in kinds:
                return False, k
        if self.d["turn"] - self.d.get("emotion_turn", -99) <= 6:
            return False, "recent_feelings"
        recent = self.d.get("echo_q_recent", [])
        if self.d["echo_q_streak"] >= 1:
            return False, "echo_just_asked"
        if sum(recent) >= 2:
            return False, "echo_asked_2_of_3"
        th = self.thread(up.thread)
        if th and th["state"] == "active":
            if self._open_questions(th["id"]):
                return False, "open_question_in_topic"
            if th["turns"][1] - th["turns"][0] < 2 and not kinds <= {"acknowledgement", "confirmation", "humor"}:
                return False, "topic_just_started"
        return True, "pause"

    def said_before(self, up: Update) -> list[str]:
        """Facts said in this session that a question asks about (≥ 2 shared words), newest first; corrections shown."""
        q = self.d["questions"][-1] if self.d["questions"] else None
        qk = set(keys(q["text"])) if q and q["asked_turn"] == self.d["turn"] else set()
        out = []
        for f in reversed(self.d["facts"]):
            if f["status"] != "current" or f["kind"] != "fact" or f["turn"] == self.d["turn"]:
                continue
            if len(qk & set(keys(f["text"]))) >= 2 or (f["supersedes"] and qk & set(keys(f["text"]))):
                old = next((o for o in self.d["facts"] if o["id"] == f["supersedes"]), None)
                out.append(f"\"{f['text']}\"" + (f" (corrected from \"{old['text']}\")" if old else ""))
            if len(out) == 2:
                break
        return out

    def _thread_note(self, th: dict, lead: str) -> str:
        known = [f["text"] for f in self.d["facts"] if f["thread"] == th["id"] and f["status"] == "current"
                 and f["kind"] == "fact"][-2:]
        open_q = [q["text"] for q in self._open_questions(th["id"])][-2:]
        parts = [f"{lead} ({th['label']})."]
        if known:
            parts.append("Known: " + "; ".join(known) + ".")
        if open_q:
            parts.append("Still open: " + "; ".join(open_q))
        return "[Conversation note: " + " ".join(parts) + "]"

    # ----- session, bounds, output -----
    def end_session(self, now: float | None = None) -> None:
        now = now or self.clock()
        up = Update(turn=self.d["turn"], intents=[])
        for th in self.d["threads"]:
            if th["state"] == "active":
                self._move(th, "suspended", "session_end", up)
        self.d["session_id"] = f"s-{int(now)}"
        self.d["recent"], self.d["echo_q_streak"] = [], 0
        for k in ("facts", "questions", "refs"):
            self.d[k] = [x for x in self.d[k] if now - x.get("at", now) <= KEEP_S]
        self.d["threads"] = [t for t in self.d["threads"] if now - t["last"] <= KEEP_S]

    def _cap(self) -> None:
        d = self.d
        while len(d["threads"]) > CAPS["threads"]:
            order = [t for s in ("abandoned", "completed", "evicted", "suspended") for t in d["threads"] if t["state"] == s]
            victim = order[0] if order else d["threads"][0]
            d["threads"].remove(victim)
        for k in ("facts", "questions"):
            items = d[k]
            while len(items) > CAPS[k]:
                old = next((x for x in items if x.get("status") == "superseded" or x.get("state") in ("answered", "dropped")),
                           items[0])
                items.remove(old)

    def to_dict(self) -> dict:
        return self.d

    def snapshot(self) -> dict:
        """Developer view (/api/dialogue): states and counts; phrases only for local display."""
        d = self.d
        return {"v": d["v"], "turn": d["turn"], "speaker": d["speaker"], "last_action": d["last_action"],
                "active": (a := self.active()) and {"id": a["id"], "label": a["label"]},
                "threads": [{k: t[k] for k in ("id", "label", "state", "speaker", "salience")} for t in d["threads"]],
                "open_questions": sum(q["state"] == "open" for q in d["questions"]),
                "facts": sum(f["status"] == "current" for f in d["facts"]), "problems": self.problems}

    def bids(self, now: float | None = None) -> list:
        """Awareness bids from the dialogue (core/awareness.py): the active thread, open questions, paused threads."""
        from core.awareness import Bid
        now = now or self.clock()
        out = []
        for th in self.d["threads"]:
            if th["state"] in ("active", "suspended"):
                pressure = len(self._open_questions(th["id"]))
                out.append(Bid(f"thread:{th['id']}", "edim", min(1.0, th["salience"] + 0.1 * pressure), 0.8,
                               f"thread_{th['state']}", now, now + 3600))
        return out


def _short(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if len(t) <= TEXT_MAX else t[: TEXT_MAX - 1].rsplit(" ", 1)[0] + "…"


# ---------- persistence ----------
def migrate(data: dict) -> dict:
    """Versioned state migration. v1 is the first; unknown future versions are not guessed at."""
    if data.get("v") == VERSION:
        return data
    raise ValueError(f"unknown dialogue state version {data.get('v')!r}")


def validate(data, now: float, problems: list[str] | None = None) -> dict:
    """A usable state from whatever was saved: bad items are dropped one by one; an unusable section starts fresh.
    Never raises: corrupted dialogue state must never stop Echo from answering."""
    problems = problems if problems is not None else []
    base = _empty(now)
    try:
        if not isinstance(data, dict):
            raise ValueError("not a dict")
        data = migrate(data)
    except Exception as e:  # noqa: BLE001
        problems.append(f"reset: {type(e).__name__}")
        return base
    out = dict(base)
    if isinstance(data.get("echo_q_recent"), list):
        out["echo_q_recent"] = [bool(x) for x in data["echo_q_recent"][-3:]]
    for k in ("at", "turn", "next_id", "echo_q_streak", "emotion_turn"):
        if isinstance(data.get(k), (int, float)):
            out[k] = data[k]
    for k in ("session_id", "mode", "last_action"):
        if isinstance(data.get(k), str):
            out[k] = data[k]
    if isinstance(data.get("speaker"), dict):
        out["speaker"] = data["speaker"]
    need = {"threads": ("id", "label", "state", "keys", "turns", "history", "last"),
            "facts": ("id", "text", "speaker", "status", "kind"),
            "questions": ("id", "text", "asked_by", "state"), "refs": ("target", "conf"),
            "recent": ("turn", "speaker", "things")}
    for k, fields in need.items():
        good = []
        for item in data.get(k) or []:
            if isinstance(item, dict) and all(f in item for f in fields) and \
                    (k != "threads" or item["state"] in STATES):
                if k == "threads":
                    item.setdefault("things", []), item.setdefault("salience", 0.5), item.setdefault("parent", None)
                good.append(item)
            else:
                problems.append(f"dropped bad {k[:-1]}")
        out[k] = good
    ids = [int(x["id"][1:]) for k in ("threads", "facts", "questions") for x in out[k] if str(x["id"])[1:].isdigit()]
    out["next_id"] = max([out["next_id"], *[i + 1 for i in ids]])
    return out


def load(cstate) -> Dialogue:
    """From core/state.py CognitiveState (section 'dialogue')."""
    try:
        return Dialogue(cstate.data.get("dialogue"))
    except Exception:  # noqa: BLE001
        return Dialogue()


def save(cstate, dlg: Dialogue) -> None:
    try:
        cstate.set("dialogue", dlg.to_dict())
    except Exception:  # noqa: BLE001
        pass
