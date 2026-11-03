"""Long-term behavioural evaluation (CA 2.0 Phase 10): built in, dormant until each measure has enough data.

the owner (2026-11-03): "build it so it's there but stays dormant until a specific data threshold is reached — eventually
Echo could be installed on thousands of computers." So nothing here is tuned to one household:
  * every measure has its own minimum-data threshold (config/longterm.json, defaults below). Below it the measure is
    `dormant` and only shows how far along it is ("23 of 50 replies").
  * only data collected AFTER the evaluator was installed counts (its first run is the start): on an existing install
    the older records predate other changes (11-03: they would have made the slow replies from before the day's
    speed fixes the baseline), and some counts weren't kept before (which questions got answers).
  * the first window that meets the threshold becomes THIS computer's baseline (frozen, kept with its date).
  * after that, each run compares the latest window with the baseline: `ok`, or `watch` with the reason.
  * runs at most once a day, in quiet time, as a scheduler job (stops the moment someone talks); no model call.
  * everything stays on this computer (data/longterm.sqlite: numbers only, no conversation text). Nothing is sent.
Measures (all from records Echo already keeps):
  first_word      wait for the first word of model replies: median and p90             (turn records)
  lessons         Train Echo lessons followed? per-lesson verdicts (core/development.py) (conversation log)
  notes           how often each one-time note / module fires (core/executive.py)      (turn records)
  questions       answer rate for the questions Echo asks (life + photo)               (curiosity state)
  memory_health   database integrity, memory counts, suggestions waiting               (memory DB) — always on
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import time
from pathlib import Path

DEFAULTS = {
    "enabled": True,
    "every_hours": 24,
    "window_days": 7,
    "min": {"first_word_replies": 50, "first_word_days": 3, "notes_turns": 100, "questions_asked": 10},
    "watch": {"first_word_p90_ratio": 1.3, "first_word_p90_min_ms": 1000, "answer_rate_below": 0.3},
}


def load_settings(path: Path | None) -> dict:
    s = json.loads(json.dumps(DEFAULTS))
    try:
        user = json.loads(Path(path).read_text()) if path else {}
    except (OSError, json.JSONDecodeError):
        user = {}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k].update(v)
        elif not k.startswith("_"):
            s[k] = v
    return s


def _p(v: list[float], q: float) -> float:
    v = sorted(v)
    return v[max(0, int(len(v) * q) - 1)] if v else 0


# ---- measures: each returns (amount of data, threshold label, value or None when dormant) ----
def first_word(records: list[dict], s: dict) -> tuple[dict, dict | None]:
    llm = [r for r in records if (r.get("route") or "").startswith("llm") and r.get("first_ms") is not None]
    days = len({time.strftime("%Y-%m-%d", time.localtime(r["started"])) for r in llm})
    need = s["min"]
    have = {"replies": len(llm), "days": days, "need": f"{need['first_word_replies']} replies over "
                                                         f"{need['first_word_days']} days"}
    if len(llm) < need["first_word_replies"] or days < need["first_word_days"]:
        return have, None
    ms = [r["first_ms"] for r in llm]
    return have, {"median_ms": statistics.median(ms), "p90_ms": _p(ms, 0.9), "n": len(ms)}


def notes(records: list[dict], s: dict) -> tuple[dict, dict | None]:
    ex = [r["exec"] for r in records if isinstance(r.get("exec"), dict) and r["exec"].get("modules")]
    have = {"turns": len(ex), "need": f"{s['min']['notes_turns']} turns"}
    if len(ex) < s["min"]["notes_turns"]:
        return have, None
    mods: dict[str, int] = {}
    for e in ex:
        for m, v in e["modules"].items():
            mods[m] = mods.get(m, 0) + (v == "fired")
    return have, {m: round(n / len(ex), 3) for m, n in sorted(mods.items())}


def lessons(report: dict, s: dict) -> tuple[dict, dict | None]:
    measured = [x for x in report.get("lessons", []) if x.get("measured") and x.get("active", True)]
    ready = [x for x in measured if x["verdict"] != "not_enough_data"]
    have = {"lessons_measurable": len(measured), "with_enough_data": len(ready),
            "need": "20 real replies before and after a lesson"}
    if not ready:
        return have, None
    return have, {str(x["id"]): x["verdict"] for x in ready}


def questions(cur_state: dict, s: dict) -> tuple[dict, dict | None]:
    asked = set(cur_state.get("asked", []))
    answered = set(cur_state.get("answered_ids", [])) & asked
    have = {"asked": len(asked), "need": f"{s['min']['questions_asked']} questions asked"}
    if len(asked) < s["min"]["questions_asked"]:
        return have, None
    photo = {q for q in asked if str(q).startswith("photo:")}
    return have, {"answer_rate": round(len(answered) / len(asked), 3),
                  "photo_answer_rate": round(len(answered & photo) / len(photo), 3) if photo else None,
                  "asked": len(asked), "answered": len(answered)}


def memory_health(store) -> tuple[dict, dict]:
    db = store.db
    ok = db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    mem = db.execute("SELECT count(*) FROM memories WHERE hidden IS NULL").fetchone()[0]
    photos = db.execute("SELECT count(*), sum(content LIKE '%the owner says:%') FROM memories "
                        "WHERE source LIKE 'photo:%' AND hidden IS NULL").fetchone()
    try:
        waiting = db.execute("SELECT count(*) FROM candidates WHERE status='pending'").fetchone()[0]
    except sqlite3.Error:
        waiting = None
    return {"always_on": True}, {"integrity_ok": ok, "memories": mem, "photos": photos[0],
                                 "photos_captioned": photos[1] or 0, "suggestions_waiting": waiting}


# ---- comparing with this computer's baseline ----
def compare(metric: str, base: dict, now: dict, s: dict) -> list[str]:
    """Reasons to watch (empty = ok)."""
    w = s["watch"]
    why = []
    if metric == "first_word":
        if now["p90_ms"] > base["p90_ms"] * w["first_word_p90_ratio"] and now["p90_ms"] - base["p90_ms"] >= \
                w["first_word_p90_min_ms"]:
            why.append(f"slow replies: p90 {now['p90_ms']} ms vs {base['p90_ms']} ms at baseline")
    elif metric == "lessons":
        worse = [k for k, v in now.items() if v == "worse_since"]
        if worse:
            why.append("lessons not followed: " + ", ".join(worse))
    elif metric == "questions":
        if now["answer_rate"] < w["answer_rate_below"]:
            why.append(f"most questions go unanswered ({now['answered']} of {now['asked']})")
    elif metric == "notes":
        for m, r in now.items():
            b = base.get(m)
            if b is not None and ((b >= 0.05 and r < b / 3) or (r >= 0.05 and r > max(b, 0.01) * 3)):
                why.append(f"{m} fires {r:.0%} of turns vs {b:.0%} at baseline")
    elif metric == "memory_health":
        if not now["integrity_ok"]:
            why.append("memory database integrity check failed")
        if base.get("memories") and now["memories"] < base["memories"] * 0.9:
            why.append(f"memories fell from {base['memories']} to {now['memories']}")
    return why


class LongTerm:
    def __init__(self, path: Path, settings_path: Path | None = None):
        self.path, self.settings_path = Path(path), settings_path
        with self._db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, at REAL NOT NULL, result TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS baselines (metric TEXT PRIMARY KEY, at REAL NOT NULL, value TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def _db(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=2)
        c.row_factory = sqlite3.Row
        return c

    def settings(self) -> dict:
        return load_settings(self.settings_path)

    def last_run(self) -> dict | None:
        with self._db() as c:
            r = c.execute("SELECT at, result FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return {"at": r["at"], **json.loads(r["result"])} if r else None

    def due(self, now: float | None = None) -> bool:
        s = self.settings()
        if not s.get("enabled", True):
            return False
        last = self.last_run()
        return not last or (now or time.time()) - last["at"] >= s["every_hours"] * 3600

    def start(self, cur_state: dict, now: float) -> dict:
        """When measuring began on this computer, and the questions already asked by then (set aside)."""
        with self._db() as c:
            r = c.execute("SELECT value FROM meta WHERE key='start'").fetchone()
            if r:
                return json.loads(r[0])
            st = {"at": now, "asked_before": sorted(map(str, cur_state.get("asked", [])))}
            c.execute("INSERT INTO meta(key, value) VALUES ('start', ?)", (json.dumps(st),))
            return st

    def baselines(self) -> dict:
        with self._db() as c:
            return {r["metric"]: {"at": r["at"], "value": json.loads(r["value"])}
                    for r in c.execute("SELECT metric, at, value FROM baselines")}

    def run(self, records: list[dict], lesson_report: dict, cur_state: dict, store, now: float | None = None) -> dict:
        """One evaluation. `records` = turn records of the latest window."""
        now = now or time.time()
        s = self.settings()
        start = self.start(cur_state, now)
        records = [r for r in records if r.get("started", 0) >= start["at"]]
        before = set(start["asked_before"])
        cur_state = {**cur_state, "asked": [q for q in cur_state.get("asked", []) if str(q) not in before]}
        measured = {"first_word": first_word(records, s), "notes": notes(records, s),
                    "lessons": lessons(lesson_report, s), "questions": questions(cur_state, s),
                    "memory_health": memory_health(store)}
        base = self.baselines()
        out = {}
        with self._db() as c:
            for m, (have, value) in measured.items():
                if value is None:
                    out[m] = {"state": "dormant", "data": have}
                elif m not in base:
                    c.execute("INSERT INTO baselines(metric, at, value) VALUES (?,?,?)", (m, now, json.dumps(value)))
                    out[m] = {"state": "baseline_set", "data": have, "value": value}
                else:
                    why = compare(m, base[m]["value"], value, s)
                    out[m] = {"state": "watch" if why else "ok", "why": why, "data": have, "value": value,
                              "baseline": base[m]["value"],
                              "baseline_from": time.strftime("%Y-%m-%d", time.localtime(base[m]["at"]))}
            c.execute("INSERT INTO runs(at, result) VALUES (?,?)", (now, json.dumps(out)))
        return out

    def history(self, metric: str, limit: int = 60) -> list[dict]:
        with self._db() as c:
            rows = c.execute("SELECT at, result FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"at": r["at"], **json.loads(r["result"]).get(metric, {})} for r in reversed(rows)]

    def reset_baseline(self, metric: str) -> bool:
        """After an approved change that is MEANT to move a measure (e.g. a faster model), the owner can start it afresh."""
        with self._db() as c:
            return c.execute("DELETE FROM baselines WHERE metric=?", (metric,)).rowcount > 0
