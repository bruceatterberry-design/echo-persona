"""Daydream training (steps 5–6): good daydream answers become training examples for Echo's own model, and a new
model version is only switched on if it beats the current one, with an "undo training" to go back.

Rules the owner set (2026-11-04):
  * Train only on Echo's and the tower model's text and the owner's approvals. Claude is judge and critic only; its
    wording never goes into training data (Anthropic's terms restrict using Claude output to train other models).
    A merged answer that shares any 8-word run with Claude's drafts or notes is marked and left out.
  * Examples are kept apart from memories (data/daydream/examples.sqlite), with what went in and what came out.
  * Saved: answers the owner 👍 (unless they copy Claude), and high-scoring ones (personality ≥ training.min_score,
    accuracy 1.0 where known, no mistake or invented fact left after the final check). 👎 never.
  * Safety gate: a new version replaces the current model ONLY if it does better on reasoning (baseline.py) or the
    daydream test set, and is not worse on identity, invention (inventing instead of "I don't know") or personality.
    Otherwise it is thrown away and the reason logged. The last good version is kept; "undo training" goes back.
  * Never trains during a conversation, and only in the night window on this Mac (scripts/daydream_train.py).
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "daydream"
LOG = ROOT / "docs" / "bench" / "daydream" / "training-log.jsonl"
SHINGLE = 8   # words: a run this long shared with Claude's text counts as copying


# ---- Claude's wording stays out ----
def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9$%']+", (text or "").lower())


def claude_overlap(answer: str, claude_texts: list[str], n: int = SHINGLE) -> int:
    """How many n-word runs of the answer also appear in Claude's drafts / critique / check notes."""
    def runs(ws: list[str]) -> set[tuple]:
        return {tuple(ws[i:i + n]) for i in range(len(ws) - n + 1)}
    mine = runs(_words(answer))
    theirs: set[tuple] = set()
    for t in claude_texts:
        theirs |= runs(_words(t))
    return len(mine & theirs)


def claude_texts(res) -> list[str]:
    """Everything Claude wrote in one daydream: its drafts and the critique / final-check notes."""
    out = [d for d in (res.drafts.get("claude") or []) if d]
    out += [str(x.get("note", "")) for x in (res.issues or []) + (res.final_issues or [])]
    return out


# ---- examples ----
class Examples:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA / "examples.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS examples (id INTEGER PRIMARY KEY, at REAL NOT NULL, daydream_id TEXT UNIQUE,
            kind TEXT, input TEXT NOT NULL, output TEXT NOT NULL, personality REAL, accuracy REAL, vote TEXT,
            claude_overlap INTEGER NOT NULL DEFAULT 0, issues_left INTEGER NOT NULL DEFAULT 0,
            include INTEGER NOT NULL DEFAULT 0, reason TEXT);""")
        self.db.commit()

    @staticmethod
    def decide(row: dict, min_score: float) -> tuple[bool, str]:
        if row["claude_overlap"]:
            return False, f"copies Claude's wording ({row['claude_overlap']} shared 8-word runs): left out"
        if row.get("vote") == "down":
            return False, "the owner 👎"
        if row.get("vote") == "up":
            return True, "the owner 👍"
        if row["issues_left"]:
            return False, "a mistake or invented fact was left in the answer"
        if row.get("accuracy") is not None and row["accuracy"] < 1:
            return False, f"accuracy {row['accuracy']:.0%}"
        if (row.get("personality") or 0) < min_score:
            return False, f"personality {row.get('personality') or 0:.2f} < {min_score}"
        return True, "high score"

    def consider(self, res, card: dict, min_score: float, kind: str = "live") -> dict:
        """Called once a daydream's score card exists. Saves the example (kept or left out, with the reason)."""
        d = card["modes"].get("daydream", {})
        left = 0 if res.revised else sum(1 for x in res.final_issues or [] if x.get("kind") in ("mistake", "invented"))
        row = {"daydream_id": res.id, "kind": kind, "input": res.task, "output": res.answer,
               "personality": d.get("personality"), "accuracy": d.get("accuracy"), "vote": card.get("vote"),
               "claude_overlap": claude_overlap(res.answer, claude_texts(res)), "issues_left": left}
        row["include"], row["reason"] = self.decide(row, min_score)
        with self.lock, self.db:
            self.db.execute("""INSERT OR REPLACE INTO examples(at,daydream_id,kind,input,output,personality,accuracy,
                vote,claude_overlap,issues_left,include,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (time.time(), row["daydream_id"], kind, row["input"], row["output"], row["personality"],
                             row["accuracy"], row["vote"], row["claude_overlap"], left, int(row["include"]), row["reason"]))
        return row

    def vote(self, daydream_id: str, vote: str, min_score: float) -> bool:
        with self.lock, self.db:
            r = self.db.execute("SELECT * FROM examples WHERE daydream_id=?", (daydream_id,)).fetchone()
            if not r:
                return False
            row = dict(r) | {"vote": vote}
            inc, why = self.decide(row, min_score)
            self.db.execute("UPDATE examples SET vote=?, include=?, reason=? WHERE id=?", (vote, int(inc), why, r["id"]))
        return True

    def counts(self) -> dict:
        r = self.db.execute("SELECT count(*), sum(include), sum(claude_overlap > 0), sum(vote='up') FROM examples").fetchone()
        return {"total": r[0] or 0, "included": r[1] or 0, "claude_left_out": r[2] or 0, "thumbs_up": r[3] or 0}

    def export(self, out_dir: Path, instructions: str, valid_share: float = 0.1, seed: int = 4) -> dict:
        """MLX-LM chat format: train.jsonl + valid.jsonl, one {"messages": [system, user, assistant]} per line."""
        rows = [dict(r) for r in self.db.execute("SELECT input, output FROM examples WHERE include=1 ORDER BY id")]
        random.Random(seed).shuffle(rows)
        n_valid = max(1, round(len(rows) * valid_share)) if len(rows) >= 10 else 0
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, part in (("valid", rows[:n_valid]), ("train", rows[n_valid:])):
            with open(out_dir / f"{name}.jsonl", "w") as f:
                for r in part:
                    f.write(json.dumps({"messages": [{"role": "system", "content": instructions},
                                                     {"role": "user", "content": r["input"]},
                                                     {"role": "assistant", "content": r["output"]}]}) + "\n")
        return {"train": len(rows) - n_valid, "valid": n_valid, "dir": str(out_dir)}


# ---- the safety gate ----
GATE_KEYS = ("reasoning", "daydream_set", "identity", "invention", "personality")


def baseline_numbers(pub: dict, answers: dict | None = None) -> dict:
    """From a scripts/baseline.py run: reasoning and identity accuracy, and the invention rate on the unknowns set
    (share of answers that don't say "I don't know" in any of personality/evals.py's ways)."""
    from personality.evals import DONT_KNOW

    def frac(s: str | None) -> float | None:
        if not s:
            return None
        a, b = s.split("/")
        return int(a) / int(b) if int(b) else None
    summ = pub.get("summary", {})
    out = {"reasoning": frac(summ.get("reasoning", {}).get("accuracy")),
           "identity": frac(summ.get("identity", {}).get("accuracy")), "invention": None}
    if answers:
        unk = [r for r in answers.get("results", []) if r.get("set") == "unknowns"]
        if unk:
            low = [(r.get("a") or "").lower().replace("’", "'") for r in unk]
            out["invention"] = sum(1 for a in low if not any(p in a for p in DONT_KNOW)) / len(unk)
    return out


def gate(current: dict, candidate: dict) -> tuple[bool, list[str]]:
    """Switch only if the candidate is better at something that matters and worse at nothing that protects Echo.
    Lower is better for invention; higher for everything else. Missing numbers block the switch."""
    why, better = [], []
    for k in GATE_KEYS:
        a, b = current.get(k), candidate.get(k)
        if a is None or b is None:
            why.append(f"{k}: not measured on both, can't compare")
            continue
        worse = b > a + 1e-9 if k == "invention" else b < a - 1e-9
        if worse:
            why.append(f"{k} got worse: {a:.3f} → {b:.3f}")
        elif k in ("reasoning", "daydream_set") and b > a + 1e-9:
            better.append(f"{k} {a:.3f} → {b:.3f}")
    if not better:
        why.append("not better than the current model on reasoning or the daydream test set")
    ok = not why
    return ok, (better if ok else why)


def log(event: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}) + "\n")


# ---- versions: switch / undo (config/echo.yaml llm.model) ----
MODEL_LINE = re.compile(r"^(\s*model:\s*)(\S+)(.*)$", re.M)


class Versions:
    def __init__(self, config_path: Path | None = None, state_path: Path | None = None):
        self.config_path = config_path or ROOT / "config" / "echo.yaml"
        self.state_path = state_path or DATA / "models.json"

    def current(self) -> str:
        m = MODEL_LINE.search(self.config_path.read_text())
        return m[2] if m else ""

    def state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {"previous": [], "history": []}

    def _write_model(self, name: str) -> None:
        text = self.config_path.read_text()
        self.config_path.write_text(MODEL_LINE.sub(lambda m: m[1] + name + m[3], text, count=1))

    def switch(self, candidate: str, why: list[str]) -> dict:
        st, cur = self.state(), self.current()
        st["previous"] = ([cur] + st.get("previous", []))[:5]
        st["history"] = st.get("history", []) + [{"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "from": cur,
                                                  "to": candidate, "why": why}]
        self._write_model(candidate)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(st, indent=1))
        log({"event": "switched", "from": cur, "to": candidate, "why": why})
        return {"from": cur, "to": candidate}

    def undo(self) -> dict | None:
        """Back to the last good version. None if training never switched the model."""
        st = self.state()
        if not st.get("previous"):
            return None
        cur, back = self.current(), st["previous"][0]
        st["previous"] = st["previous"][1:]
        st["history"] = st.get("history", []) + [{"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "from": cur,
                                                  "to": back, "why": ["undo training"]}]
        self._write_model(back)
        self.state_path.write_text(json.dumps(st, indent=1))
        log({"event": "undo", "from": cur, "to": back})
        return {"from": cur, "to": back}


def restart_core(delay_s: float = 8) -> None:
    """Restart Echo's core (launchd) a few seconds from now, so the reply that announced it is spoken first."""
    import os
    import subprocess
    subprocess.Popen(["/bin/sh", "-c", f"sleep {delay_s}; launchctl kickstart -k gui/{os.getuid()}/com.echopersona.core"],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
