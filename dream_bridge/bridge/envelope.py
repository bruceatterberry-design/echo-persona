"""Envelope (BRIDGE_PROTOCOL.md section 5): validation, replay and clock checks."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROTO = 1
MAX_ROUNDS = 3
CLOCK_SKEW_S = 120
REPLAY_WINDOW_S = 600

TaskType = Literal["heartbeat", "conversation", "memory_batch", "memory_request", "dream_result", "challenge", "ack",
                   "daydream_activity", "daydream_request", "daydream_draft"]   # rev e (2026-11-04): Daydream


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(max_length=64)
    quote: str = Field(default="", max_length=4000)


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str = Field(min_length=8, max_length=64)
    timestamp: str
    source: Literal["echo", "dream"]
    destination: Literal["echo", "dream"]
    task_type: TaskType
    in_reply_to: str | None = Field(default=None, max_length=64)
    memory_refs: list[str] = Field(default_factory=list, max_length=500)
    context_summary: str = Field(default="", max_length=8000)
    proposed_conclusion: str | None = Field(default=None, max_length=8000)
    supporting_evidence: list[Evidence] = Field(default_factory=list, max_length=500)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    uncertainty: str = Field(default="", max_length=4000)
    priority: int = Field(default=1, ge=1, le=5)
    required_response: Literal["none", "ack", "answer", "review"] = "none"
    status: Literal["new", "processing", "done", "rejected", "error"] = "new"
    round: int = Field(default=0, ge=0)
    payload: dict = Field(default_factory=dict)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make(task_type: str, payload: dict | None = None, **kw) -> dict:
    env = Envelope(message_id=str(uuid.uuid4()), timestamp=now_iso(), source="echo", destination="dream",
                   task_type=task_type, payload=payload or {}, **kw)
    return env.model_dump()


class Replay:
    def __init__(self):
        self.seen: dict[str, float] = {}

    def check(self, env: Envelope) -> str | None:
        """None if fine, else the reason to reject."""
        try:
            ts = datetime.fromisoformat(env.timestamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return "timestamp"
        now = time.time()
        if abs(now - ts) > CLOCK_SKEW_S:
            return "clock"
        self.seen = {k: t for k, t in self.seen.items() if now - t < REPLAY_WINDOW_S}
        if env.message_id in self.seen:
            return "replay"
        self.seen[env.message_id] = now
        return None
