"""Conversation-first scheduler (CA 2.0 Phase 2). One rule: a person talking to Echo always goes first.

Measured 2026-11-02: Ollama answers one request at a time (OLLAMA_NUM_PARALLEL=1), so a background job in flight made
Echo's first word wait 16–17 s; cancelling the job freed Ollama at once (first word 0.4–0.5 s). So:
  * turn_started()     — someone spoke to Echo: every background LLM request in flight is CANCELLED (the job retries
                         later; nothing is marked done or failed), and no new one starts.
  * spoken_until(t)    — Echo is speaking until t; background work waits QUIET_S after that.
  * job(name)          — background code runs inside this; chat_post()/run() inside it become cancellable.
  * memory pressure    — when the Mac's free memory is at or below min_free_percent (~14.5 GB of 16 GB used),
                         background work is suspended (conversation is never affected).
  * paused             — the owner can pause all background work (config/scheduler.json or POST /api/scheduler).
  * dream tower        — turn_started() also tells the Dream Bridge (127.0.0.1:47803) that someone is talking, so
                         the tower pauses its dreaming. Fire-and-forget on a daemon thread, at most once per 20 s,
                         0.5 s timeout, errors ignored: Echo never waits for it. ECHO_DREAM_BRIDGE=0 turns it off.
Everything is observable: snapshot() → /api/scheduler (state, reason, per-job counts, last durations).
Without a running event loop (tests, scripts) calls run directly, uncancellable — same behaviour as before.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import os
import subprocess
import urllib.request
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "config" / "scheduler.json"
DEFAULTS = {"background": True, "quiet_seconds": 20, "min_free_percent": 10}
TURN_MAX_S = 120
DREAM_PING_URL = "http://127.0.0.1:47803/conversation/ping"
DREAM_PING_EVERY_S = 20
_dream_last = 0.0


def _dream_ping() -> None:
    try:
        urllib.request.urlopen(urllib.request.Request(DREAM_PING_URL, data=b"", method="POST"), timeout=0.5).close()
    except Exception:
        pass   # bridge not installed or not running: nothing to do


def notify_dream_bridge() -> bool:
    """Tell the Dream Bridge someone is talking. Never blocks; returns True if a ping was started."""
    global _dream_last
    if os.environ.get("ECHO_DREAM_BRIDGE", "1") == "0" or time.time() - _dream_last < DREAM_PING_EVERY_S:
        return False
    _dream_last = time.time()
    threading.Thread(target=_dream_ping, name="dream-ping", daemon=True).start()
    return True


class Preempted(BaseException):
    """A background job was stopped because someone started talking (or work is suspended). Retry later.
    A BaseException on purpose: jobs that catch every Exception (and would mark an item "failed") let it pass, so an
    interrupted item is simply retried; only the scheduler's own loops catch it."""


def free_memory_percent() -> int | None:
    """macOS's own measure of available memory, 0–100 (kern.memorystatus_level)."""
    try:
        return int(subprocess.run(["sysctl", "-n", "kern.memorystatus_level"], capture_output=True, text=True,
                                  timeout=2).stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


class Scheduler:
    def __init__(self, settings: Path = SETTINGS, memory_probe=free_memory_percent):
        self.settings_path, self.memory_probe = settings, memory_probe
        self.loop: asyncio.AbstractEventLoop | None = None
        self.lock = threading.Lock()
        self.busy_until = 0.0          # end of the current turn / spoken reply (wall clock)
        self.turn_open_until = 0.0     # a turn is being answered (closed by spoken_until; 2 min at most, in case a
                                       # reply path never reports back)
        self.inflight: dict[int, tuple[str, object]] = {}   # id → (job name, future/task)
        self.jobs: dict[str, dict] = {}
        self.preemptions = 0
        self.local = threading.local()
        self._settings, self._settings_at = dict(DEFAULTS), 0.0
        self._mem, self._mem_at = None, 0.0
        self.manual_pause: bool | None = None   # set from the status page; None = follow the settings file
        # CA 2.0 Phase 7: something other than the conversation used the model since the conversation was last read
        # (Ollama keeps ONE prompt in its cache, so the next reply would have to re-read everything: 5-14 s measured)
        self.cache_disturbed: dict | None = None

    # ---- settings (re-read every 10 s, so a change needs no restart) ----
    def settings(self) -> dict:
        if time.time() - self._settings_at > 10:
            try:
                self._settings = DEFAULTS | json.loads(self.settings_path.read_text())
            except (OSError, json.JSONDecodeError):
                self._settings = dict(DEFAULTS)
            self._settings_at = time.time()
        return self._settings

    def memory_free(self) -> int | None:
        if time.time() - self._mem_at > 5:
            self._mem, self._mem_at = self.memory_probe(), time.time()
        return self._mem

    # ---- conversation signals ----
    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def turn_started(self) -> int:
        """Someone is talking to Echo: stop background LLM work now. Returns how many requests were cancelled."""
        with self.lock:
            self.turn_open_until = time.time() + TURN_MAX_S
            victims = list(self.inflight.values())
        notify_dream_bridge()
        n = 0
        for name, fut in victims:
            if fut.cancel():
                n += 1
                self._count(name, "preempted")
        self.preemptions += n
        return n

    def spoken_until(self, t: float) -> None:
        """The reply is finished being written; Echo is speaking until about t."""
        with self.lock:
            self.turn_open_until = 0.0
            self.busy_until = max(self.busy_until, t)

    def why_not(self) -> str | None:
        """None if background work may run now, else the reason it waits."""
        s = self.settings()
        if self.manual_pause or (self.manual_pause is None and not s["background"]):
            return "paused"
        if time.time() < self.turn_open_until:
            return "conversation"
        if time.time() < self.busy_until + s["quiet_seconds"]:
            return "conversation"
        if (m := self.memory_free()) is not None and m <= s["min_free_percent"]:
            return f"memory low ({m}% free)"
        return None

    def quiet(self) -> bool:
        return self.why_not() is None

    # ---- the model's prompt cache (CA 2.0 Phase 7) ----
    def cache_used(self, by: str | None) -> None:
        """A request that isn't the conversation went to the model (a background job, the loved one's writing, the music check).
        The conversation's prompt is no longer in Ollama's cache; core/executive.py re-reads it in quiet time."""
        if by != "warm":
            with self.lock:
                self.cache_disturbed = {"by": by or "other", "at": time.time()}

    def cache_restored(self) -> dict | None:
        """The conversation was just read again: returns what had disturbed it (for the log)."""
        with self.lock:
            d, self.cache_disturbed = self.cache_disturbed, None
        return d

    # ---- background jobs ----
    def _count(self, name: str, key: str, seconds: float | None = None) -> None:
        with self.lock:
            j = self.jobs.setdefault(name, {"runs": 0, "done": 0, "preempted": 0, "failed": 0, "waited": 0})
            j[key] = j.get(key, 0) + 1
            if seconds is not None:
                j["last_seconds"] = round(seconds, 2)
            j["last"] = time.strftime("%H:%M:%S")

    @contextlib.contextmanager
    def job(self, name: str):
        """Run a piece of background work. Raises Preempted at the start if it must wait (caller retries later)."""
        if (why := self.why_not()):
            self._count(name, "waited")
            raise Preempted(why)
        prev = getattr(self.local, "job", None)
        self.local.job = name
        t = time.perf_counter()
        self._count(name, "runs")
        try:
            yield
            self._count(name, "done", time.perf_counter() - t)
        except Preempted:
            raise
        except Exception:
            self._count(name, "failed", time.perf_counter() - t)
            raise
        finally:
            self.local.job = prev

    def in_thread(self, name: str, fn, *a, **k):
        """For asyncio.to_thread(SCHED.in_thread, "summaries", fn, ...): the job is marked inside the worker thread."""
        with self.job(name):
            return fn(*a, **k)

    def current_job(self) -> str | None:
        return getattr(self.local, "job", None)

    def call(self, coro_fn):
        """From a worker thread inside job(): run coro_fn() on Echo's event loop so it can be cancelled when someone
        talks. Outside a job, or without a loop, it runs directly (not cancellable)."""
        name = self.current_job()
        if not name or not self.loop or self.loop.is_closed():
            return asyncio.run(coro_fn())
        if (why := self.why_not()):
            raise Preempted(why)
        fut = asyncio.run_coroutine_threadsafe(coro_fn(), self.loop)
        key = id(fut)
        with self.lock:
            self.inflight[key] = (name, fut)
        try:
            return fut.result()
        except concurrent.futures.CancelledError:
            raise Preempted("conversation")
        finally:
            with self.lock:
                self.inflight.pop(key, None)

    async def run(self, name: str, coro):
        """Background work that already lives on the event loop (e.g. reading the prompt in advance)."""
        if (why := self.why_not()):
            coro.close()
            self._count(name, "waited")
            raise Preempted(why)
        task = asyncio.ensure_future(coro)
        key = id(task)
        with self.lock:
            self.inflight[key] = (name, task)
        self._count(name, "runs")
        t = time.perf_counter()
        try:
            r = await task
            self._count(name, "done", time.perf_counter() - t)
            return r
        except asyncio.CancelledError:
            if task.cancelled() and not asyncio.current_task().cancelling():
                raise Preempted("conversation")
            raise
        finally:
            with self.lock:
                self.inflight.pop(key, None)

    def snapshot(self) -> dict:
        why = self.why_not()
        with self.lock:
            running = sorted({n for n, _ in self.inflight.values()})
            jobs = {k: dict(v) for k, v in self.jobs.items()}
        return {"background": "running" if running else ("waiting" if why else "allowed"), "reason": why,
                "running": running, "preemptions": self.preemptions, "memory_free_percent": self.memory_free(),
                "settings": self.settings(), "manual_pause": self.manual_pause, "jobs": jobs,
                "cache_disturbed_by": (self.cache_disturbed or {}).get("by")}


SCHED = Scheduler()   # one per process: core and its background loop share it
