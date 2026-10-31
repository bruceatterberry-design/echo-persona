"""Optional helper: the owner's Claude Code account (the `claude` command and its subscription) for heavy background jobs.

Safe to disable or lose: every caller treats None as "not available" and does the job on this Mac (Ollama) as before.
Never used for live replies (those stay local and instant). Off unless config/claude.json says otherwise, and each job
must be switched on by itself, because each job sends some personal text off the Mac:
  personality  the owner's own recorded words (transcripts) -> traits     (their own data)
  condense     the owner's approved traits -> short summary                 (their own data)
  summaries    conversation turns with the loved one -> memory notes             (their words too: off unless they agree)

The call runs with no tools, no files, no MCP servers, no settings or memory, from an empty folder, so Claude only sees
the one prompt. A daily cap protects the owner's Pro plan limits; if the cap is hit or anything fails, Echo goes local.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("echo.claude")
JOBS = ("personality", "condense", "summaries", "training")   # training: core/trainer.py, on only while training
DEFAULT = {"enabled": False, "model": "sonnet", "timeout_s": 120, "daily_limit": 60,
           "jobs": {j: False for j in JOBS}}


class ClaudeHelper:
    def __init__(self, config_path: Path, state_path: Path):
        self.config_path, self.state_path = config_path, state_path
        self.lock = threading.Lock()
        self.stats = {"calls": 0, "ok": 0, "failed": 0, "last_error": None, "last_ok": None}

    def config(self) -> dict:
        try:
            c = json.loads(self.config_path.read_text())
        except (OSError, json.JSONDecodeError):
            c = {}
        return DEFAULT | c | {"jobs": DEFAULT["jobs"] | c.get("jobs", {})}

    def save_config(self, c: dict) -> None:
        self.config_path.write_text(json.dumps(c, indent=2) + "\n")

    def binary(self) -> str | None:
        return shutil.which("claude") or next((p for p in (Path.home() / ".local/bin/claude",
                                                           Path("/opt/homebrew/bin/claude")) if p.exists()), None)

    def used_today(self) -> int:
        try:
            s = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return 0
        return s.get("count", 0) if s.get("day") == time.strftime("%Y-%m-%d") else 0

    def _count(self) -> None:
        self.state_path.write_text(json.dumps({"day": time.strftime("%Y-%m-%d"), "count": self.used_today() + 1}))

    def allowed(self, job: str) -> bool:
        c = self.config()
        return bool(c["enabled"] and (job == "test" or c["jobs"].get(job)) and self.binary() and self.used_today() < c["daily_limit"])

    def ask(self, job: str, prompt: str, schema: dict | None = None, timeout: int | None = None) -> dict | str | None:
        """Run one prompt through Claude. Returns parsed JSON (with schema) or text, or None: caller then works locally."""
        if job not in JOBS + ("test",) or not self.allowed(job):
            return None
        c = self.config()
        cmd = [str(self.binary()), "-p", "--output-format", "json", "--model", c["model"], "--tools", "",
               "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence",
               "--system-prompt", "You are a careful text-analysis helper. Follow the instructions exactly and use only "
                                  "the text given. Reply with the requested output only."]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        with self.lock:   # one at a time: gentle on the plan's limits
            self.stats["calls"] += 1
            self._count()
            t0 = time.time()
            try:
                with tempfile.TemporaryDirectory() as empty:   # nothing on disk for it to see
                    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout or c["timeout_s"],
                                       cwd=empty, env={k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"})
                out = json.loads(r.stdout)
                if r.returncode or out.get("is_error"):
                    raise RuntimeError(str(out.get("result") or r.stderr)[:200])
                result = out.get("structured_output") if schema else out.get("result")
                if result is None:
                    raise RuntimeError("empty answer")
            except Exception as e:   # timeouts, logged out, limits reached, no internet: all mean "do it locally"
                self.stats["failed"] += 1
                self.stats["last_error"] = f"{time.strftime('%H:%M')} {type(e).__name__}: {str(e)[:160]}"
                log.warning("claude helper %s failed, using local model: %s", job, self.stats["last_error"])
                return None
            self.stats["ok"] += 1
            self.stats["last_ok"] = time.strftime("%Y-%m-%d %H:%M")
            log.info("claude helper %s ok ms=%d", job, (time.time() - t0) * 1000)   # never the content
            return result

    def logged_in(self) -> bool | None:
        """Is the `claude` command signed in to the owner's account? (local check, uses no plan quota). None = can't tell."""
        b = self.binary()
        if not b:
            return None
        try:
            r = subprocess.run([str(b), "auth", "status"], capture_output=True, text=True, timeout=20)
            return bool(json.loads(r.stdout).get("loggedIn"))
        except Exception:  # noqa: BLE001
            return None

    def open_login(self) -> bool:
        """Open a Terminal window running Claude's sign-in (it continues in the browser). the owner finishes it himself."""
        b = self.binary()
        if not b:
            return False
        cmd = f"'{b}' auth login"
        try:
            subprocess.run(["osascript", "-e", f'tell application "Terminal" to do script "{cmd}"',
                            "-e", 'tell application "Terminal" to activate'], check=True, timeout=20, capture_output=True)
            log.info("opened Claude sign-in")
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def test(self) -> dict:
        """A harmless check (no personal text): is the account reachable from Echo, and how long does a call take?"""
        t0 = time.time()
        ok = self.ask("test", "Reply with only the word OK.") is not None
        return {"ok": ok, "seconds": round(time.time() - t0, 1), "error": None if ok else self.stats["last_error"]}

    def status(self) -> dict:
        c = self.config()
        return {"enabled": c["enabled"], "jobs": c["jobs"], "model": c["model"], "installed": bool(self.binary()),
                "used_today": self.used_today(), "daily_limit": c["daily_limit"]} | self.stats
