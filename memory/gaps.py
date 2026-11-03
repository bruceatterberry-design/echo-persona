"""Autonomous learning, Stage 5a (approved by the owner 2026-11-01): Echo notices what he doesn't know and PROPOSES what to
learn. Nothing is researched, saved to memory or asked without a person deciding.

A knowledge gap is recorded after a reply when:
  personal  the controller called it a memory question (core/controller.py) and memory found nothing (grounding
            "unknown", memory/grounding.py) or the reply says he doesn't know / remember
  general   any other question where the reply says he doesn't know
Repeats of the same question merge (word overlap), counting how often and who asked.

Each open gap is shown as a learning objective (what, why, what he'd gain, suggested way: ask the owner for personal
gaps, look it up for general ones). the owner decides: ask_owner / research / dismiss / done.
Stage 5b: ask_owner → approved_questions() feeds memory/curiosity.py, which asks it first (same rules: the owner's voice,
at most every 30 min); their answer becomes memory *suggestions* to review and the objective is marked done.
research → only with a web address the owner gives, read by memory/learn.py (the safe page reader), as "learned" material.
Stage 5c: check_learning() re-asks each done/research objective against memory (retrieval + grounding label, no LLM)
in quiet time: "helped" = a memory saved AFTER the owner's decision now clearly answers it (known/believed); "not_yet" =
re-checked every 10 min; "not_helped" after 14 days. Each check is in the audit log.

Kept in its own database (data/learning.sqlite, beside the memory DB; never in git). It holds the question text,
like the conversation log. Every decision is kept with who and when (audit trail).
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path

from core import people

DENY = re.compile(r"\b(?:i )?(?:don'?t|do not|can'?t|cannot) (?:remember|recall|know)\b|\bdon'?t have (?:any )?(?:notes|"
                  r"memor\w+|details|information|record|anything)\b|\b(?:wasn'?t|haven'?t been|have not been) told\b|"
                  r"\bno (?:notes|memor\w+|record)\b|\bnot sure\b|\bno idea\b", re.I)
STOP = set("a an the is are was were do does did i you me my your we our of to in on at for about what who whom whose "
           "when where why how which tell can could would will please echo it that this there be have has had".split())
DECISIONS = ("ask_owner", "research", "dismiss", "done")
MERGE = 0.6


def key_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w not in STOP and len(w) > 2}


def similar(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


# CA 2.0 Phase 9: all 4 open objectives on 11-03 were false — a statement about poems (twice), garbled speech ("Kill
# create"), and my own test question whose reply hedged ("six photos… the others might show him too, but I don't know
# for sure"). A gap now needs a real question, of at least 3 words, that isn't a test turn, and a reply that says it
# doesn't know — not one that answers and hedges about the rest.
ASKS = re.compile(r"\?\s*$|^\W*(?:(?:hey|hi|ok(?:ay)?|so|and|well)[, ]+)?(?:echo[, ]+)?(?:who|what|when|where|why|how|which|"
                  r"whose|do|does|did|is|are|was|were|can|could|will|would|have|has|had|tell me|remind me)\b", re.I)
HEDGE = re.compile(r"\b(?:don'?t|do not) know (?:for sure|exactly|which (?:one|ones)|whether the others|if the others)\b|"
                   r"\bnot sure (?:which|whether|if) (?:the )?others\b", re.I)
TEST = re.compile(r"\btesting\b", re.I)


def is_question(text: str | None) -> bool:
    t = re.sub(r"\[[^\]]*\]\s*", "", text or "").strip()
    return len(t.split()) >= 3 and bool(ASKS.search(t)) and not TEST.search(t)


def detect(kind: str | None, label: str | None, reply: str, text: str | None = None) -> str | None:
    """'personal' / 'general' / None. Commands, errors, confident answers, statements and test turns are not gaps.
    `text` = what the person said (None: old callers, no question check)."""
    if kind in (None, "command"):
        return None
    if text is not None and not is_question(text):
        return None
    says_no = bool(DENY.search(HEDGE.sub("", reply or "")))
    if kind == "memory" and (label == "unknown" or says_no):
        return "personal"
    if kind != "memory" and says_no:
        return "general"
    return None


class Gaps:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        with self._db() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS gaps (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, question TEXT NOT NULL,
                         words TEXT NOT NULL, first REAL NOT NULL, last REAL NOT NULL, count INTEGER NOT NULL DEFAULT 1,
                         askers TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open', decided_by TEXT,
                         decided_at REAL, note TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS gap_log (id INTEGER PRIMARY KEY, gap_id INTEGER, at REAL, what TEXT,
                         by TEXT)""")
            cols = {r[1] for r in c.execute("PRAGMA table_info(gaps)")}
            for col, typ in (("outcome", "TEXT"), ("checked_at", "REAL"), ("outcome_detail", "TEXT")):   # Stage 5c
                if col not in cols:
                    c.execute(f"ALTER TABLE gaps ADD COLUMN {col} {typ}")

    def _db(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=2)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def note(self, kind: str, question: str, speaker: str | None, now: float | None = None) -> int:
        """Record a gap, or count a repeat of an open one. Returns the gap id."""
        now = now or time.time()
        q = (question or "").strip()[:300]
        w = key_words(q)
        who = speaker or "typed"
        with self.lock, self._db() as c:
            best, best_s = None, 0.0
            for r in c.execute("SELECT id, words, kind FROM gaps WHERE status='open'"):
                s = similar(w, set(r["words"].split()))
                if r["kind"] == kind and s > best_s:
                    best, best_s = r["id"], s
            if best is not None and best_s >= MERGE:
                askers = set(filter(None, c.execute("SELECT askers FROM gaps WHERE id=?", (best,)).fetchone()[0].split(",")))
                c.execute("UPDATE gaps SET count=count+1, last=?, askers=? WHERE id=?",
                          (now, ",".join(sorted(askers | {who})), best))
                c.execute("INSERT INTO gap_log(gap_id, at, what, by) VALUES (?,?,?,?)", (best, now, "asked again", who))
                return best
            gid = c.execute("INSERT INTO gaps(kind, question, words, first, last, askers) VALUES (?,?,?,?,?,?)",
                            (kind, q, " ".join(sorted(w)), now, now, who)).lastrowid
            c.execute("INSERT INTO gap_log(gap_id, at, what, by) VALUES (?,?,?,?)", (gid, now, "noticed", who))
            return gid

    def objectives(self, status: str = "open") -> list[dict]:
        """Open gaps as proposed learning objectives, most asked first."""
        with self._db() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM gaps WHERE status=? ORDER BY count DESC, last DESC",
                                               (status,))]
        out = []
        for r in rows:
            who = ", ".join(people.display(x) for x in r["askers"].split(",") if x)
            times = "once" if r["count"] == 1 else f"{r['count']} times"
            if r["kind"] == "personal":
                why = f"Asked {times} ({who}); I found nothing about it in my memories."
                how, gain = "ask_owner", "Next time I can answer from the owner's own words instead of saying I don't remember."
            else:
                why = f"Asked {times} ({who}); I said I didn't know."
                how, gain = "research", "I could answer it, from a source you approve, instead of not knowing."
            out.append({"id": r["id"], "kind": r["kind"], "question": r["question"], "why": why, "suggested": how,
                        "gain": gain, "count": r["count"], "first": r["first"], "last": r["last"], "status": r["status"],
                        "decided_by": r["decided_by"], "decided_at": r["decided_at"],
                        "outcome": r.get("outcome"), "outcome_detail": r.get("outcome_detail")})
        return out

    def decide(self, gid: int, decision: str, by: str = "owner:panel", note: str = "") -> bool:
        if decision not in DECISIONS:
            raise ValueError(decision)
        now = time.time()
        with self.lock, self._db() as c:
            n = c.execute("UPDATE gaps SET status=?, decided_by=?, decided_at=?, note=? WHERE id=?",
                          (decision, by, now, note[:500] or None, gid)).rowcount
            if n:
                c.execute("INSERT INTO gap_log(gap_id, at, what, by) VALUES (?,?,?,?)", (gid, now, decision, by))
            return bool(n)

    def history(self, gid: int) -> list[dict]:
        with self._db() as c:
            return [dict(r) for r in c.execute("SELECT at, what, by FROM gap_log WHERE gap_id=? ORDER BY id", (gid,))]


def ask_text(question: str, askers: str) -> str:
    """How Echo asks the owner about a gap: honest about where it came from, no LLM."""
    who = [people.display(x) for x in askers.split(",") if x and x not in ("owner", "typed", "unknown")]
    lead = f"{' and '.join(who)} asked me" if who else "I was asked"
    q = question.strip().rstrip("?.!")
    return f"{lead} \"{q}?\" and I didn't know the answer. Will you tell me about it?"


def _gaps_extra(self) -> list[dict]:
    """Objectives the owner approved for asking, oldest decision first, in curiosity's question shape."""
    with self._db() as c:
        rows = c.execute("SELECT id, question, askers FROM gaps WHERE status='ask_owner' ORDER BY decided_at").fetchall()
    return [{"id": f"gap-{r['id']}", "period": None, "text": ask_text(r["question"], r["askers"])} for r in rows]


def _gaps_event(self, gid: int, what: str, by: str) -> None:
    with self.lock, self._db() as c:
        c.execute("INSERT INTO gap_log(gap_id, at, what, by) VALUES (?,?,?,?)", (gid, time.time(), what, by))


Gaps.approved_questions = _gaps_extra
Gaps.event = _gaps_event


def gap_id(q: dict | None) -> int | None:
    """The objective id behind a curiosity question, if it came from one."""
    qid = str((q or {}).get("id", ""))
    return int(qid[4:]) if qid.startswith("gap-") and qid[4:].isdigit() else None


CHECK_EVERY_S = 600
GIVE_UP_S = 14 * 86400


def check_learning(g: "Gaps", ask, now: float | None = None) -> list[dict]:
    """Re-ask due objectives. `ask(question) -> (label, [(memory_id, created, hit_label), …])` from retrieval
    (core/server.py). "helped" if ANY memory saved after the decision is a strong hit (known/believed), not only the
    top one: an older loosely related memory can outrank the owner's new answer (2026-11-01, birthday test).
    Returns what was checked: [{"id", "outcome", "detail"}]."""
    now = now or time.time()
    with g._db() as c:
        due = [dict(r) for r in c.execute(
            "SELECT id, question, decided_at FROM gaps WHERE status IN ('done','research') "
            "AND (outcome IS NULL OR outcome='not_yet') AND (checked_at IS NULL OR checked_at < ?)",
            (now - CHECK_EVERY_S,))]
    out = []
    for r in due:
        label, hits = ask(r["question"])
        fresh = [(mid, hl) for mid, created, hl in hits
                 if created is not None and r["decided_at"] is not None and created >= r["decided_at"]
                 and hl in ("known", "believed")]
        if fresh:
            outcome, detail = "helped", f"{fresh[0][1]}, memory {fresh[0][0]}"
        elif r["decided_at"] and now - r["decided_at"] > GIVE_UP_S:
            outcome, detail = "not_helped", f"after 14 days still {label}"
        else:
            outcome, detail = "not_yet", f"{label}" + (" (older memory only)" if label in ("known", "believed") else "")
        with g.lock, g._db() as c:
            c.execute("UPDATE gaps SET outcome=?, outcome_detail=?, checked_at=? WHERE id=?", (outcome, detail, now, r["id"]))
            prev = c.execute("SELECT what FROM gap_log WHERE gap_id=? ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
            what = f"check: {outcome}"
            if not prev or prev["what"] != what:   # log changes, not every repeat of "not yet"
                c.execute("INSERT INTO gap_log(gap_id, at, what, by) VALUES (?,?,?,?)", (r["id"], now, what, "echo:check"))
        out.append({"id": r["id"], "outcome": outcome, "detail": detail})
    return out


def learning_stats(g: "Gaps") -> dict:
    """For the dashboard: how many learning objectives were decided, and how many helped (measured, not claimed)."""
    with g._db() as c:
        rows = c.execute("SELECT status, outcome FROM gaps").fetchall()
    st: dict = {"noticed": len(rows)}
    for r in rows:
        st[r["status"]] = st.get(r["status"], 0) + 1
        if r["outcome"]:
            st["outcome_" + r["outcome"]] = st.get("outcome_" + r["outcome"], 0) + 1
    return st
