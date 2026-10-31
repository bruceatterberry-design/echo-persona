"""Tool permissions + audit log (JARVIS step 3; idea from JARVIS core/permissions.py, MIT).

Every tool Echo has is listed here with what it can touch and its risk. Echo's rule stays: anything that changes
things outside this Mac, spends money, sends messages or downloads files needs the owner's yes first. Today's tools only
READ public web pages, so they run without asking, but only on addresses the owner gave (or links, when he asked me to
research). Settings: config/tools.json. The audit log (data/tool_audit.sqlite) keeps tool, site, why it was allowed,
outcome, size and time: never page text or what was asked.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

CATALOGUE = {
    # name: (what it touches, risk, needs the owner's yes each time)
    "read_webpage": ("reads a public web page (no login, nothing saved); if it shows nothing without JavaScript, runs it "
                     "in an invisible Chrome with an empty profile (public addresses only, no downloads)", "low", False),
    "page_links": ("lists the links on a public web page", "low", False),
    "web_search": ("sends the search words to DuckDuckGo (no account, no cookies) and lists the results", "low", False),
    "listen_music": ("downloads the audio of a YouTube music video the owner/the loved one sent, only approved genres/artists "
                     "(config/music.json), analyses it on this Mac, then deletes the audio", "medium", False),
}
DEFAULTS = {"enabled": True, "follow_links": "any", "max_pages": 5}


def load_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    out = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
    if out["follow_links"] not in ("any", "same_site", "never"):
        out["follow_links"] = "same_site"
    out["max_pages"] = max(1, min(int(out["max_pages"]), 8))
    return out


class Audit:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("""CREATE TABLE IF NOT EXISTS tool_audit (id INTEGER PRIMARY KEY, at REAL NOT NULL, tool TEXT NOT NULL,
                           host TEXT, why TEXT, outcome TEXT, chars INTEGER, ms INTEGER, risk TEXT)""")
        self.db.commit()

    def __call__(self, tool: str, host: str, why: str, outcome: str, chars: int, ms: int) -> None:
        risk = CATALOGUE.get(tool, ("", "unknown", True))[1]
        with self.lock:
            self.db.execute("INSERT INTO tool_audit (at, tool, host, why, outcome, chars, ms, risk) VALUES (?,?,?,?,?,?,?,?)",
                            (time.time(), tool, host, why, outcome, chars, ms, risk))
            self.db.execute("DELETE FROM tool_audit WHERE id <= (SELECT MAX(id) FROM tool_audit) - 5000")   # keep the last 5,000
            self.db.commit()

    def recent(self, limit: int = 50) -> list[dict]:
        with self.lock:
            cur = self.db.execute("SELECT at, tool, host, why, outcome, chars, ms, risk FROM tool_audit ORDER BY id DESC LIMIT ?",
                                  (max(1, min(limit, 500)),))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
