"""Dream reader (BRIDGE_PROTOCOL.md section 7c): checks what the tower sent back and queues it for the owner.

- Reads inbox/*.json (dream_result and challenge messages saved by the receiver).
- Checks every proposal against Echo's real data, READ-ONLY:
    * every ref (memory:N, entity:N, fact:N) must exist, not be hidden, not be private (classroom data);
    * every evidence quote must be an exact substring of the text it cites;
    * synthetic refs (synthetic:...) are never allowed in a real dream.
  A proposal that fails any check is kept as "invalid" with the reason (a disagreement for the tower to learn from).
- Passing proposals wait as "pending" in the bridge's own queue (data/dream-bridge/proposals.sqlite) for the owner.
  NOTHING is written to Echo's memory or personality here. Applying an approved proposal to Echo is a separate,
  later step. Core identity, values, permissions and relationships are marked protected.
- Sends the tower one ack per dream with the per-proposal results (section 7b, "results").
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from . import export, store

ECHO = "http://127.0.0.1:8765"
INSIGHT_CHARS = 200
MAX_INSIGHTS_PER_RESULT = 10

WEIGHTS = {"identity": 0.20, "evidence": 0.20, "novelty": 0.15, "consistency": 0.15, "confidence": 0.15, "usefulness": 0.15}
KINDS = {"association", "question", "goal", "personality", "contradiction", "outdated", "discovery", "challenge"}
PROTECTED = re.compile(r"\b(identity|who i am|core values?|values|permissions?|relationship|wife|husband|married|loved one)\b", re.I)
REF = re.compile(r"^(memory|entity|fact|episode):(\d+)$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS dreams (dream_id TEXT PRIMARY KEY, message_id TEXT, model TEXT, started TEXT, finished TEXT,
  narrative TEXT, file TEXT, received REAL NOT NULL, proposals INTEGER NOT NULL, valid INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS proposals (proposal_id TEXT PRIMARY KEY, dream_id TEXT NOT NULL, kind TEXT NOT NULL,
  proposed_change TEXT NOT NULL, reason TEXT, refs TEXT NOT NULL, evidence TEXT NOT NULL, confidence REAL,
  consequences TEXT, scores TEXT, weighted REAL, goal TEXT, protected INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL, check_notes TEXT, received REAL NOT NULL, decided REAL, decided_by TEXT, decision_note TEXT);
CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, proposal_id TEXT NOT NULL, action TEXT NOT NULL,
  before TEXT, after TEXT, by TEXT NOT NULL, at REAL NOT NULL, note TEXT);
"""


def db() -> sqlite3.Connection:
    store.STATE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(store.STATE_DIR / "proposals.sqlite", timeout=5)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def weighted(scores: dict | None) -> float | None:
    if not isinstance(scores, dict):
        return None
    try:
        return round(sum(w * max(0.0, min(1.0, float(scores.get(k, 0)))) for k, w in WEIGHTS.items()), 3)
    except (TypeError, ValueError):
        return None


class Echo:
    """Read-only look-ups in Echo's memory database."""

    def __init__(self):
        self.con = export._ro(export._db("memory.sqlite"))

    def text(self, ref: str) -> tuple[str | None, str | None]:
        """(text that quotes must come from, problem) for one ref."""
        m = REF.match(ref)
        if not m:
            return None, f"{ref}: not a real Echo reference" + (" (synthetic refs never count as evidence)" if ref.startswith("synthetic:") else "")
        t, i = m.group(1), int(m.group(2))
        if t == "memory":
            r = self.con.execute("SELECT content, when_text, people, place, hidden FROM memories WHERE id=?", (i,)).fetchone()
            if not r:
                return None, f"{ref}: no such memory"
            if r["hidden"] is not None or export.private(r["content"], r["people"], r["place"]):
                return None, f"{ref}: not available to dreams"
            return quote_text(r["content"], r["when_text"], r["people"], r["place"]), None
        if t == "entity":
            r = self.con.execute("SELECT name, note FROM entities WHERE id=?", (i,)).fetchone()
            return (f"{r['name']} {r['note'] or ''}", None) if r else (None, f"{ref}: no such entity")
        if t == "fact":
            r = self.con.execute("SELECT predicate, value, span FROM facts WHERE id=?", (i,)).fetchone()
            return (" ".join(x for x in (r["predicate"], r["value"], r["span"]) if x), None) if r else (None, f"{ref}: no such fact")
        r = self.con.execute("SELECT summary FROM episodes WHERE id=?", (i,)).fetchone()
        return (r["summary"] or "", None) if r else (None, f"{ref}: no such episode")


def quote_text(content, when_text=None, people=None, place=None) -> str:
    """Agreed with the tower (2026-11-03): a memory quote may come from content + when_text + people + place,
    the non-empty ones joined with single spaces, in that order. Entities: name + note."""
    return " ".join(x for x in (content, when_text, people, place) if x)


def check(p: dict, echo: Echo) -> list[str]:
    problems = []
    if p.get("kind") not in KINDS:
        problems.append(f"unknown kind {p.get('kind')!r}")
    if not isinstance(p.get("proposed_change"), str) or not p["proposed_change"].strip():
        problems.append("no proposed_change")
    refs = p.get("supporting_refs") or []
    if not isinstance(refs, list) or not refs:
        problems.append("no supporting memories")
        refs = []
    texts = {}
    for ref in refs[:50]:
        txt, why = echo.text(str(ref))
        if why:
            problems.append(why)
        else:
            texts[ref] = txt
    for ev in (p.get("evidence") or [])[:50]:
        ref, quote = str(ev.get("ref", "")), ev.get("quote") or ""
        if ref not in texts:
            txt, why = echo.text(ref)
            if why:
                problems.append(why)
                continue
            texts[ref] = txt
        if quote and quote not in texts[ref]:
            problems.append(f"quote not found word for word in {ref}")
    return problems


def check_insight(p: dict, names_re) -> list[str]:
    """rev d §7d: general understanding only. No refs, no names, short."""
    problems = []
    if p.get("kind") != "insight":
        problems.append("synthetic results may only contain insights")
    text = p.get("insight")
    if not isinstance(text, str) or not 10 <= len(" ".join(text.split())) <= INSIGHT_CHARS:
        problems.append(f"insight must be 10-{INSIGHT_CHARS} characters")
        text = ""
    if p.get("supporting_refs") or p.get("evidence"):
        problems.append("insights may not cite memories or evidence")
    ex = (p.get("basis") or {}).get("examples") or []
    if not isinstance(ex, list) or len(ex) > 10 or not all(isinstance(x, str) and re.fullmatch(r"syn-\d{4}", x) for x in ex):
        problems.append("basis.examples must be at most 10 syn- ids")
    if export.private(text, p.get("reason")):
        problems.append("mentions students/classroom")
    if names_re and names_re.search(f"{text} {p.get('reason') or ''}"):
        problems.append("uses a real name; insights must be general")
    if re.search(r"\b(I remember|the owner|the loved one)\b", text or "", re.I):
        problems.append("insights must be general, not about Echo's real life")
    return problems


def _names_re():
    from .synthetic import real_names
    n = real_names()
    return re.compile(r"\b(" + "|".join(re.escape(x) for x in sorted(n, key=len, reverse=True)) + r")\b", re.I) if n else None


def _ack_results(dream_id, message_id, results) -> dict:
    return {"dream_id": dream_id, "message_id": message_id, "results": results}


def process_inbox() -> list[dict]:
    """Check every new inbox file. Returns the per-dream results to send back to the tower."""
    d = store.dirs()
    done_dir = d["inbox"] / "read"
    done_dir.mkdir(exist_ok=True)
    out = []
    con = db()
    echo = None
    for f in sorted(d["inbox"].glob("*.json")):
        env = json.loads(f.read_text())
        if env.get("task_type") not in ("dream_result", "challenge"):
            f.rename(done_dir / f.name)
            continue
        echo = echo or Echo()
        pl = env.get("payload") or {}
        if env["task_type"] == "challenge":   # treat a challenge as one proposal
            did = "challenge-" + env["message_id"]
            props = [{"proposal_id": env["message_id"], "kind": "challenge", "proposed_change": env.get("proposed_conclusion") or "",
                      "reason": env.get("context_summary"), "supporting_refs": env.get("memory_refs") or [],
                      "evidence": env.get("supporting_evidence") or [], "confidence": env.get("confidence")}]
        else:
            did, props = pl.get("dream_id"), pl.get("proposals") or []
        synthetic = pl.get("origin") == "synthetic"
        names_re = _names_re() if synthetic else None
        if synthetic:
            props = props[:MAX_INSIGHTS_PER_RESULT]
        results, valid = [], 0
        now = time.time()
        for p in props[:100]:
            if not isinstance(p, dict) or not isinstance(p.get("proposal_id"), str):
                continue
            if synthetic:
                problems = check_insight(p, names_re)
                p = {**p, "proposed_change": " ".join(str(p.get("insight") or "").split()), "supporting_refs": [],
                     "evidence": [], "consequences": "becomes one of Echo's lessons (never a memory)"}
            elif p.get("kind") == "insight":
                problems = ["insights only come from synthetic dreams (origin: synthetic)"]
            else:
                problems = check(p, echo)
            status = "invalid" if problems else "pending"
            valid += not problems
            prot = bool(p.get("kind") == "personality" or PROTECTED.search(f"{p.get('proposed_change', '')} {p.get('consequences', '')}"))
            con.execute("INSERT OR IGNORE INTO proposals (proposal_id, dream_id, kind, proposed_change, reason, refs, evidence, "
                        "confidence, consequences, scores, weighted, goal, protected, status, check_notes, received) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (p["proposal_id"], did, str(p.get("kind")), str(p.get("proposed_change", ""))[:4000],
                         str(p.get("reason") or "")[:4000], json.dumps(p.get("supporting_refs") or []),
                         json.dumps(p.get("basis") if synthetic else (p.get("evidence") or []), ensure_ascii=False), p.get("confidence"),
                         str(p.get("consequences") or "")[:4000], json.dumps(p.get("scores")), weighted(p.get("scores")),
                         json.dumps(p.get("goal")) if p.get("goal") else None, int(prot), status,
                         "; ".join(problems)[:2000] or None, now))
            results.append({"proposal_id": p["proposal_id"], "accepted": not problems,
                            "reason": "queued for the owner's review" if not problems else "; ".join(problems)[:500]})
        con.execute("INSERT OR IGNORE INTO dreams VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (did, env["message_id"], pl.get("model"), pl.get("started"), pl.get("finished"),
                     str(pl.get("narrative") or "")[:20000], f.name, now, len(results), valid))
        con.commit()
        f.rename(done_dir / f.name)
        store.journal(event="dream_read", dream=did, n_checked=len(results), n_valid=valid)
        out.append(_ack_results(did, env["message_id"], results))
    con.close()
    return out


def pending(limit: int = 50) -> list[dict]:
    con = db()
    rows = con.execute("SELECT proposal_id, kind, proposed_change, reason, refs, weighted, protected, dream_id "
                       "FROM proposals WHERE status='pending' ORDER BY weighted DESC NULLS LAST, received LIMIT ?", (limit,)).fetchall()
    con.close()
    return [dict(r) | {"refs": json.loads(r["refs"])} for r in rows]


def decide(proposal_id: str, approve: bool, by: str = "owner", note: str = "") -> dict:
    """the owner's decision. Approved = approved for Echo, NOT yet applied (applying is a separate step)."""
    con = db()
    r = con.execute("SELECT status FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
    if not r:
        con.close()
        raise KeyError(proposal_id)
    if r["status"] != "pending":
        con.close()
        raise ValueError(f"already {r['status']}")
    new = "approved" if approve else "rejected"
    now = time.time()
    kind, text, refs, did = con.execute("SELECT kind, proposed_change, refs, dream_id FROM proposals WHERE proposal_id=?",
                                        (proposal_id,)).fetchone()
    if approve and kind == "insight":
        try:
            _to_echo(proposal_id, text, by)
        except Exception:
            con.close()
            raise
        new = "applied"
    elif approve and kind == "association":
        mem = [int(x.split(":")[1]) for x in json.loads(refs) if REF.match(x) and x.startswith("memory:")]
        mem = list(dict.fromkeys(mem))
        if len(mem) >= 2:   # link consecutive memories, at most 3 links per proposal
            try:
                made = _link_in_echo(list(zip(mem, mem[1:]))[:3], f"dream:{did}:{proposal_id}", by)
            except Exception:
                con.close()
                raise
            new = "applied"
            note = (note + " " if note else "") + "links " + ",".join(f"{a}-{b}" for a, b in made)
    con.execute("UPDATE proposals SET status=?, decided=?, decided_by=?, decision_note=? WHERE proposal_id=?",
                (new, now, by, note[:1000], proposal_id))
    con.execute("INSERT INTO history (proposal_id, action, before, after, by, at, note) VALUES (?,?,?,?,?,?,?)",
                (proposal_id, new, r["status"], new, by, now, note[:1000]))
    con.commit()
    con.close()
    store.journal(event="proposal_" + new, proposal=proposal_id, by=by)
    return {"proposal_id": proposal_id, "status": new}


def _to_echo(proposal_id: str, text: str, by: str) -> None:
    import httpx
    try:
        r = httpx.post(f"{ECHO}/api/lessons/insight", timeout=10,
                       json={"text": text, "source": f"dream-insight:{proposal_id}", "approved_by": by})
    except httpx.HTTPError as e:
        raise RuntimeError(f"Echo isn't answering ({e}); nothing changed") from e
    if r.status_code == 409:
        raise OverflowError(r.json().get("detail", "Echo already has 3 dream insights; retire one first"))
    if r.status_code != 200:
        raise RuntimeError(f"Echo refused the insight ({r.status_code}): {r.text[:200]}")


def _link_in_echo(pairs: list[tuple[int, int]], source: str, by: str) -> list[tuple[int, int]]:
    import httpx
    made = []
    for a, b in pairs:
        try:
            r = httpx.post(f"{ECHO}/api/memory/dream_link", timeout=10,
                           json={"a": a, "b": b, "source": source[:200], "approved_by": by})
        except httpx.HTTPError as e:
            raise RuntimeError(f"Echo isn't answering ({e}); links made so far: {made}") from e
        if r.status_code == 409:
            raise OverflowError(r.json().get("detail", "Echo has the maximum number of dream links"))
        if r.status_code != 200:
            raise RuntimeError(f"Echo refused link {a}-{b} ({r.status_code}): {r.text[:200]}")
        made.append((a, b))
    return made


def unlink(a: int, b: int) -> dict:
    import httpx
    r = httpx.delete(f"{ECHO}/api/memory/dream_link/{int(a)}/{int(b)}", timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"Echo answered {r.status_code}")
    store.journal(event="dream_link_removed", a=a, b=b)
    return r.json()


def retire(proposal_id: str, by: str = "owner") -> dict:
    """Switch an applied insight off in Echo (the lesson stays in history, inactive)."""
    import httpx
    r = httpx.delete(f"{ECHO}/api/lessons/insight/dream-insight:{proposal_id}", timeout=10)
    if r.status_code != 200 or not r.json().get("dropped"):
        raise KeyError(proposal_id)
    con = db()
    con.execute("UPDATE proposals SET status='retired' WHERE proposal_id=?", (proposal_id,))
    con.execute("INSERT INTO history (proposal_id, action, before, after, by, at) VALUES (?,?,?,?,?,?)",
                (proposal_id, "retired", "applied", "retired", by, time.time()))
    con.commit(); con.close()
    store.journal(event="insight_retired", proposal=proposal_id, by=by)
    return {"proposal_id": proposal_id, "status": "retired"}


def counts() -> dict:
    con = db()
    c = dict(con.execute("SELECT status, count(*) FROM proposals GROUP BY status").fetchall())
    n = con.execute("SELECT count(*) FROM dreams").fetchone()[0]
    con.close()
    return {"dreams": n, **{k: c.get(k, 0) for k in ("pending", "approved", "rejected", "invalid", "applied", "retired")}}
