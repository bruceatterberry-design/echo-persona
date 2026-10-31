"""Train Echo: the owner marks replies 👍/👎, then Claude (his Pro account) helps fix the 👎 ones, with the owner deciding each step.

The flow the owner asked for (2026-10-31):
  "use Claude to evaluate Echo"  → testing on: every reply in the window gets 👍/👎 (and "what was wrong?") and is logged.
  "Train Echo"                   → "are you sure?" (says exactly what is sent) → yes →
     for each 👎 reply: Claude diagnoses it and offers 2-3 fixes → the owner picks one → Claude evaluates that fix →
     the owner says yes/no → applied → the same words are sent again → Echo's new answer is shown with 👍/👎 →
     👍: done, next one; 👎: Claude looks again (knowing what was tried) until 👍, skip, or stop.
  When every 👎 is tested: testing off and Claude disconnected (the "training" job in config/claude.json switched off).

What a fix can be:
  lesson  a short rule added to Echo's instructions (config/lessons.json → system prompt). Applied here, undoable
          ("undo the last lesson"), at most MAX_LESSONS so the model's 8k window isn't crowded.
  code    a change to Echo's program. Claude runs with no tools from an empty folder (core/claude_helper.py), so it can't
          edit files from inside Echo: the request is written to docs/TRAINING_REQUESTS.md for a Claude Code session.

What goes to Claude, only after "Train Echo" + yes: for each 👎, the owner's words, Echo's reply and the owner's note, plus Echo's
base instructions and lessons. Never the memories, the conversation history, or anything the loved one said in other turns.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MAX_LESSONS = 30
SELF = "echo"   # speaker of things Echo said on his own (after a song…): fixable, but there are no words to replay
LESSON_CHARS = 400
START = re.compile(r"\b(?:use|ask|get)\s+claude\s+to\s+(?:evaluate|test|check|grade)\s+(?:echo|ecco|eco|you|echo)\b", re.I)
TRAIN = re.compile(r"^\W*(?:(?:hey|ok|okay)\s+)?(?:echo[,.]?\s+)?train\s+(?:echo|ecco|eco|yourself|echo)\W*$", re.I)
STOP_TRAIN = re.compile(r"^\W*(?:stop|end|cancel|quit)\s+training\W*$", re.I)
STOP_TEST = re.compile(r"^\W*(?:stop|end|turn off)\s+(?:the\s+)?testing\W*$", re.I)
LOGIN = re.compile(r"\b(?:log\s*in(?:to)?|sign\s*in(?:\s+to)?|connect(?:\s+to)?)\s+(?:to\s+)?(?:my\s+)?claude(?:\s+pro)?\b", re.I)
UNDO = re.compile(r"^\W*undo\s+(?:the\s+)?last\s+lesson\W*$", re.I)
YES = re.compile(r"^\W*(?:yes|yeah|yep|yup|sure|i'?m sure|ok(?:ay)?|do it|go ahead|apply it|please do)\b", re.I)
NO = re.compile(r"^\W*(?:no|nope|not now|don'?t|cancel|wait)\b", re.I)
SKIP = re.compile(r"^\W*(?:skip|next|pass)(?:\s+(?:it|this|that)(?:\s+one)?)?\W*$", re.I)
ORDINAL = {"1": 0, "one": 0, "first": 0, "2": 1, "two": 1, "second": 1, "3": 2, "three": 2, "third": 2}

DIAGNOSE_SCHEMA = {"type": "object", "properties": {
    "diagnosis": {"type": "string"},
    "options": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "kind": {"type": "string", "enum": ["lesson", "code"]},
        "change": {"type": "string"}, "why": {"type": "string"}}, "required": ["title", "kind", "change", "why"]}}},
    "required": ["diagnosis", "options"]}
EVALUATE_SCHEMA = {"type": "object", "properties": {
    "verdict": {"type": "string", "enum": ["good", "risky"]}, "evaluation": {"type": "string"},
    "final_change": {"type": "string"}, "side_effects": {"type": "string"}},
    "required": ["verdict", "evaluation", "final_change", "side_effects"]}

ABOUT_ECHO = """Echo is the owner's digital self: a local AI on a Mac mini (Qwen 3.5 9B through Ollama, 8,192-token
window, replies streamed and spoken in the owner's cloned voice, with an animated face). He speaks in the first person as
the owner, mostly with the loved one, the owner's wife. He has memories (not shown here), can read web pages the owner gives him, can open
and play things on YouTube, and listens to approved music. Each reply starts with an expression tag like [smiling].
Fixes are of two kinds: "lesson" = one short rule (under 400 characters, plain first-person wording like the
instructions below) added to his instructions; "code" = a change to his program that a developer must make (describe
it precisely). Prefer a lesson when instructions can fix it; choose code when the fault is in what the program does
(e.g. a command not recognised, a tool missing). Never suggest anything that weakens privacy or safety rules."""


@dataclass
class Step:
    """What the window should do next."""
    say: str = ""
    choices: list[str] = field(default_factory=list)   # buttons; clicking sends that text
    replay: dict | None = None                          # {"text", "entry"}: send these words again as the owner
    work: Callable[[], "Step"] | None = None            # slow part (Claude): run in a thread, then show its Step
    expression: str = "attentive"


class Trainer:
    def __init__(self, db_path: Path, lessons_path: Path, requests_path: Path, helper, instructions: Callable[[], str],
                 on_lessons: Callable[[], None] = lambda: None):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.lessons_path, self.requests_path = lessons_path, requests_path
        self.helper, self.instructions, self.on_lessons = helper, instructions, on_lessons
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY, at REAL NOT NULL, input TEXT NOT NULL,
            reply TEXT NOT NULL, speaker TEXT, vote TEXT, note TEXT, replay_of INTEGER,
            status TEXT NOT NULL DEFAULT 'logged',          -- logged | wrong | fixed | skipped | needs_code
            attempts TEXT NOT NULL DEFAULT '[]');           -- what was tried: [{change, kind, new_reply, note}]
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);""")
        self.db.commit()
        self.state: dict = {"mode": "idle"}

    # ---------- testing on/off ----------
    @property
    def testing(self) -> bool:
        r = self.db.execute("SELECT v FROM meta WHERE k='testing'").fetchone()
        return bool(r and r[0] == "1")

    def _set_testing(self, on: bool) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('testing',?)", ("1" if on else "0",))

    @property
    def busy(self) -> bool:
        """In the middle of a training conversation (yes/no, pick a fix…)."""
        return self.state["mode"] != "idle"

    # ---------- logging replies + votes ----------
    def record(self, text: str, reply: str, speaker: str | None, replay_of: int | None = None) -> int | None:
        if not self.testing:
            return None
        with self.lock, self.db:
            cur = self.db.execute("INSERT INTO feedback(at,input,reply,speaker,replay_of) VALUES (?,?,?,?,?)",
                                  (time.time(), text[:4000], reply[:4000], speaker, replay_of))
        return cur.lastrowid

    def vote(self, fid: int, vote: str, note: str = "") -> Step | None:
        if vote not in ("up", "down"):
            return None
        note = note.strip()[:1000]
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM feedback WHERE id=?", (fid,)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE feedback SET vote=?, note=?, status=? WHERE id=?",
                            (vote, note, "wrong" if vote == "down" and not row["replay_of"] else row["status"], fid))
        st = self.state
        if row["replay_of"] and st["mode"] == "replay" and st.get("entry") == row["replay_of"]:
            return self._after_retest(row["replay_of"], vote, row["reply"], note)
        return None

    def pending(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM feedback WHERE status='wrong' AND replay_of IS NULL ORDER BY id").fetchall()

    # ---------- commands + the training conversation ----------
    def handle(self, text: str) -> Step | None:
        """A Step if this message is for the trainer, else None (normal conversation)."""
        t = text.strip()
        st = self.state
        if START.search(t):
            self._set_testing(True)
            n = len(self.pending())
            return Step("Testing is on. Each of my replies will have a thumbs up and a thumbs down. If I get something "
                        "wrong, press thumbs down and tell me what was wrong. When you're ready, say \"Train Echo\"."
                        + (f" I already have {n} marked wrong." if n else ""))
        if STOP_TEST.match(t):
            self._finish(keep_testing=False)
            return Step("Testing is off, and Claude is disconnected.")
        if LOGIN.search(t):
            return self._login_step(force=True)
        if UNDO.match(t):
            return Step(self.undo_lesson())
        if TRAIN.match(t):
            n = len(self.pending())
            if not n:
                return Step("There's nothing marked wrong yet. Say \"use Claude to evaluate Echo\", then press thumbs down "
                            "on any reply that isn't right." if not self.testing else
                            "Nothing is marked wrong yet. Press thumbs down on any reply that isn't right, then say "
                            "\"Train Echo\" again.")
            self.state = {"mode": "confirm"}
            return Step(f"I have {n} {'reply' if n == 1 else 'replies'} you marked wrong. Training sends Claude only those: "
                        "your words, my reply and your note for each, plus my basic instructions. Not my memories and "
                        "not the rest of our conversations. Are you sure?", ["Yes", "No"])
        if st["mode"] == "idle":
            return None
        if STOP_TRAIN.match(t):
            self._finish(keep_testing=True)
            return Step("Okay, training stopped. Claude is disconnected; testing is still on, and anything not fixed yet "
                        "is kept for next time.")
        if st["mode"] == "confirm":
            if YES.match(t):
                if (need := self._login_step()):
                    self.state = {"mode": "idle"}
                    return need
                self._connect(True)
                return self._next_entry()
            if NO.match(t):
                self.state = {"mode": "idle"}
                return Step("Okay, not now. Say \"Train Echo\" whenever you're ready.")
            return Step("Should I start training with Claude? Yes or no?", ["Yes", "No"])
        if st["mode"] == "choose":
            if SKIP.match(t):
                return self._skip(st["entry"])
            i = self._pick(t, len(st["options"]))
            if i is None:
                return Step("Which fix should I try? Say its number, \"skip\" or \"stop training\".",
                            self._option_buttons(st["options"]))
            opt = st["options"][i]
            self.state = {**st, "mode": "evaluating", "picked": opt}
            return Step(f"Okay, option {i + 1}. I'm asking Claude to check it.", work=lambda: self._evaluate(st["entry"], opt))
        if st["mode"] == "approve":
            if YES.match(t):
                return self._apply(st["entry"], st["picked"], st["final"])
            if NO.match(t):
                self.state = {**st, "mode": "choose"}
                return Step("Okay, I won't make that change. Pick another fix, or say \"skip\".",
                            self._option_buttons(st["options"]))
            return Step("Should I make that change? Yes or no?", ["Yes", "No"])
        if st["mode"] == "replay":
            if SKIP.match(t):
                return self._skip(st["entry"])
            return None   # anything else is normal talk; the 👍/👎 on the retest decides
        return None   # "evaluating"/"diagnosing": Claude is working

    @staticmethod
    def _pick(t: str, n: int) -> int | None:
        for w in re.findall(r"[a-z0-9]+", t.lower()):
            if w in ORDINAL and ORDINAL[w] < n:
                return ORDINAL[w]
        return None

    @staticmethod
    def _option_buttons(options: list[dict]) -> list[str]:
        return [str(i + 1) for i in range(len(options))] + ["Skip", "Stop training"]

    def _login_step(self, force: bool = False) -> Step | None:
        """None if Claude is ready; otherwise open the sign-in and say what to do."""
        ok = self.helper.logged_in()
        if ok and not force:
            return None
        if ok:
            return Step("I'm already signed in to your Claude account, so I'm ready when you are.", expression="smiling")
        if ok is None and not self.helper.binary():
            return Step("I can't find the Claude program on this Mac, so I can't sign in. Training needs it.", expression="neutral")
        opened = self.helper.open_login()
        return Step(("I need you to sign in to your Claude account. I opened the sign-in window; finish it in the browser, "
                     "then say \"Train Echo\" again." if opened else
                     "I need you to sign in to your Claude account, but I couldn't open the sign-in window. In Terminal, "
                     "type: claude auth login"), expression="neutral")

    def _connect(self, on: bool) -> None:
        """The "training" job in config/claude.json: on only while training (the owner: disconnect Claude afterwards)."""
        c = self.helper.config()
        c["jobs"] = {**c.get("jobs", {}), "training": on}
        if on:
            c["enabled"] = True
        self.helper.save_config(c)

    def _finish(self, keep_testing: bool) -> None:
        self.state = {"mode": "idle"}
        self._connect(False)
        if not keep_testing:
            self._set_testing(False)

    def _next_entry(self) -> Step:
        rows = self.pending()
        if not rows:
            self._finish(keep_testing=False)
            done = self.db.execute("SELECT status, COUNT(*) FROM feedback WHERE replay_of IS NULL AND status IN "
                                   "('fixed','skipped','needs_code') GROUP BY status").fetchall()
            summary = ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in done) or "nothing changed"
            return Step(f"That's all of them ({summary}). Testing is off, and Claude is disconnected.", expression="smiling")
        row = rows[0]
        self.state = {"mode": "diagnosing", "entry": row["id"]}
        return Step(f"Looking at: \"{row['input'][:160]}\". I'm asking Claude what went wrong.",
                    work=lambda: self._diagnose(row["id"]))

    def _entry(self, eid: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM feedback WHERE id=?", (eid,)).fetchone()

    def _brief(self, row: sqlite3.Row) -> str:
        tries = json.loads(row["attempts"])
        said = ("(Nobody spoke: " + row["input"].strip("()") + ")") if row["speaker"] == SELF else row["input"]
        out = (f"THE OWNER SAID: {said}\nECHO REPLIED: {row['reply']}\nTHE OWNER'S NOTE ON WHAT WAS WRONG: "
               f"{row['note'] or '(none)'}")
        for k, a in enumerate(tries, 1):
            out += (f"\n\nATTEMPT {k}: {a['kind']} change: {a['change']}\nECHO'S NEW REPLY: {a.get('new_reply', '')}\n"
                    f"THE OWNER STILL SAID WRONG: {a.get('note') or '(no note)'}")
        return out

    def _diagnose(self, eid: int) -> Step:
        row = self._entry(eid)
        prompt = (ABOUT_ECHO + "\n\nECHO'S CURRENT INSTRUCTIONS AND LESSONS:\n" + self.instructions()[:12000] +
                  "\n\nTHE REPLY THE OWNER MARKED WRONG:\n" + self._brief(row) +
                  "\n\nSay briefly what went wrong (diagnosis, 1-2 plain sentences the owner will hear spoken) and give 2 or 3 "
                  "different fixes. Each option: a short title, kind (lesson or code), the exact change (for a lesson, "
                  "the exact rule text), and why it should work. If earlier attempts failed, don't repeat them.")
        res = self.helper.ask("training", prompt, DIAGNOSE_SCHEMA, timeout=240)
        if not isinstance(res, dict) or not res.get("options"):
            self.state = {"mode": "idle"}
            self._connect(False)
            if self.helper.logged_in() is False:   # the sign-in expired mid-way
                return self._login_step()
            return Step("I couldn't reach Claude just now (no internet, or the plan's limit), so training is paused. "
                        "Say \"Train Echo\" to try again.", expression="neutral")
        opts = [o for o in res["options"] if o.get("change")][:3]
        self.state = {"mode": "choose", "entry": eid, "options": opts}
        lines = [f"{i + 1}. {o['title']} ({'a new rule for me' if o['kind'] == 'lesson' else 'a program change'}): {o['why']}"
                 for i, o in enumerate(opts)]
        return Step(f"Claude says: {res['diagnosis']}\n\n" + "\n".join(lines) + "\n\nWhich one should I try?",
                    self._option_buttons(opts))

    def _evaluate(self, eid: int, opt: dict) -> Step:
        row = self._entry(eid)
        prompt = (ABOUT_ECHO + "\n\nECHO'S CURRENT INSTRUCTIONS AND LESSONS:\n" + self.instructions()[:12000] +
                  "\n\nTHE PROBLEM:\n" + self._brief(row) + "\n\nTHE FIX THE OWNER CHOSE (" + opt["kind"] + "): " +
                  opt["change"] + "\n\nEvaluate this fix honestly: will it fix this reply, could it break other "
                  "conversations (especially everyday talk with the loved one), and does it conflict with any current instruction? "
                  "Give final_change: for a lesson, the exact final rule text (first person, under 400 characters); for "
                  "code, a precise description for a developer. evaluation and side_effects: 1-2 plain sentences each.")
        res = self.helper.ask("training", prompt, EVALUATE_SCHEMA, timeout=240)
        st = self.state
        if not isinstance(res, dict) or not res.get("final_change"):
            self.state = {**st, "mode": "choose"}
            return Step("I couldn't get Claude's check just now. Pick a fix again, or say \"skip\".",
                        self._option_buttons(st.get("options", [])), expression="neutral")
        final = res["final_change"].strip()
        if opt["kind"] == "lesson":
            final = final[:LESSON_CHARS]
        self.state = {**st, "mode": "approve", "final": final}
        what = (f"The new rule would be: \"{final}\"" if opt["kind"] == "lesson" else
                f"The program change would be: {final}\n(I can't change my own program; I'd write it down for the owner's "
                "next Claude Code session.)")
        warn = " Claude thinks it's risky." if res["verdict"] == "risky" else ""
        return Step(f"Claude's check: {res['evaluation']}{warn} Possible side effects: {res['side_effects']}\n\n{what}\n\n"
                    "Should I make this change?", ["Yes", "No"])

    def _apply(self, eid: int, opt: dict, final: str) -> Step:
        row = self._entry(eid)
        if opt["kind"] == "code":
            self.requests_path.parent.mkdir(parents=True, exist_ok=True)
            with self.requests_path.open("a") as f:
                f.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')}: training entry {eid}\n- the owner said: {row['input']}\n"
                        f"- Echo replied: {row['reply']}\n- the owner's note: {row['note'] or '(none)'}\n"
                        f"- Requested change: {final}\n")
            self._set_status(eid, "needs_code")
            nxt = self._next_entry()
            return Step("I've written that program change down in docs/TRAINING_REQUESTS.md for your next Claude Code "
                        "session. " + nxt.say, nxt.choices, nxt.replay, nxt.work)
        if len(self.lessons(active=True)) >= MAX_LESSONS:
            self.state = {**self.state, "mode": "choose"}
            return Step(f"I already have {MAX_LESSONS} lessons, which is as many as fit. Say \"undo the last lesson\" or "
                        "skip this one.", ["Skip", "Stop training"])
        self.add_lesson(final, eid)
        tries = json.loads(row["attempts"]) + [{"kind": "lesson", "change": final}]
        with self.lock, self.db:
            self.db.execute("UPDATE feedback SET attempts=? WHERE id=?", (json.dumps(tries), eid))
        if row["speaker"] == SELF:   # said on my own: nothing to send again; the rule applies from now on
            self._set_status(eid, "fixed")
            nxt = self._next_entry()
            return Step("Done. I said that one on my own, so there's nothing to replay; I'll follow the new rule from now "
                        "on. " + nxt.say, nxt.choices, nxt.replay, nxt.work, "smiling")
        self.state = {"mode": "replay", "entry": eid}
        return Step("Done. Here's the same thing again, so you can see my new answer. Press thumbs up if it's right now.",
                    replay={"text": row["input"], "entry": eid}, expression="smiling")

    def _after_retest(self, eid: int, vote: str, new_reply: str, note: str) -> Step:
        row = self._entry(eid)
        tries = json.loads(row["attempts"])
        if tries:
            tries[-1].update(new_reply=new_reply[:2000], note=note)
        with self.lock, self.db:
            self.db.execute("UPDATE feedback SET attempts=? WHERE id=?", (json.dumps(tries), eid))
        if vote == "up":
            self._set_status(eid, "fixed")
            nxt = self._next_entry()
            return Step("Good, that one's fixed. " + nxt.say, nxt.choices, nxt.replay, nxt.work, "smiling")
        self.state = {"mode": "diagnosing", "entry": eid}
        return Step("Still not right. I'm asking Claude to look again, knowing what we tried.",
                    work=lambda: self._diagnose(eid))

    def _skip(self, eid: int) -> Step:
        self._set_status(eid, "skipped")
        nxt = self._next_entry()
        return Step("Skipped. " + nxt.say, nxt.choices, nxt.replay, nxt.work)

    def _set_status(self, eid: int, status: str) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE feedback SET status=? WHERE id=?", (status, eid))

    # ---------- lessons ----------
    def lessons(self, active: bool = False) -> list[dict]:
        try:
            items = json.loads(self.lessons_path.read_text()).get("lessons", [])
        except (OSError, json.JSONDecodeError):
            items = []
        return [x for x in items if x.get("active", True)] if active else items

    def _save_lessons(self, items: list[dict]) -> None:
        self.lessons_path.write_text(json.dumps({"_note": "Rules Echo learned in training (core/trainer.py). Set "
                                                 "active to false to switch one off.", "lessons": items}, indent=2) + "\n")
        self.on_lessons()

    def add_lesson(self, text: str, entry: int | None) -> None:
        items = self.lessons()
        items.append({"id": max([x["id"] for x in items], default=0) + 1, "text": text.strip()[:LESSON_CHARS],
                      "from_entry": entry, "added": time.strftime("%Y-%m-%d %H:%M"), "active": True})
        self._save_lessons(items)

    def undo_lesson(self) -> str:
        items = self.lessons()
        act = [x for x in items if x.get("active", True)]
        if not act:
            return "I don't have any lessons to undo."
        act[-1]["active"] = False
        self._save_lessons(items)
        return f"Okay, I've dropped my last lesson: \"{act[-1]['text']}\""


def lessons_block(path: Path) -> str:
    """Text added to Echo's system prompt."""
    try:
        items = [x for x in json.loads(path.read_text()).get("lessons", []) if x.get("active", True)]
    except (OSError, json.JSONDecodeError):
        return ""
    if not items:
        return ""
    return "\n\nLessons the owner taught me in training (I follow these):\n" + "\n".join(f"- {x['text']}" for x in items[:MAX_LESSONS])
