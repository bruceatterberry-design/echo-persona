"""Daydream: an on-demand mode that brings in extra thinking power for one complex task (the owner, 2026-11-04).

"daydream" (said or typed, at the start of a message) starts it; the task can follow in the same message
("daydream: plan our trip to Branson") or in the next one. It ends when the task is answered, or on "wake up" /
"stop daydreaming". Echo says plainly when he goes in and when he comes out.

How the models work together (each helper optional; with none left this is plain Echo):
  1. Echo's local model (Qwen 3.5 9B on this Mac) splits the task into a few parts.
  2. Helpers draft the parts in parallel (the tower's model through the Dream Bridge, Claude through
     core/claude_helper.py; added in later steps). Echo drafts any part no helper answered.
  3. Echo merges the drafts into one answer in his own voice and personality.
Live replies outside a daydream don't go through here at all: they stay local and fast.

Developer-only: off and hidden unless config/echo.yaml has daydream.enabled AND this Mac's hardware id matches
daydream.owner_machine. Any other copy treats "daydream" as an ordinary word and never contacts the owner's tower.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import httpx
import yaml

log = logging.getLogger("echo.daydream")
CONFIG = Path(__file__).resolve().parents[1] / "config" / "echo.yaml"   # tests point this elsewhere

DEFAULTS = {"enabled": False, "owner_machine": "", "max_parts": 4, "task_timeout_s": 480, "local_think": False,
            "tower": {"use": True, "timeout_s": 90, "max_tokens": 500, "wake": True, "wake_wait_s": 150},
            "claude": {"use": True},
            "consistency_runs": 3,
            "training": {"enabled": False, "min_examples": 50, "min_score": 0.8, "where": "mac_overnight",
                         "keep_versions": 2}}

# "daydream", "let's daydream", "Echo, daydream about …", "daydream: …". Only at the very start of a message, so
# "I had a daydream at work" stays an ordinary sentence.
START = re.compile(r"^\W*(?:(?:hey|hi|ok|okay|so)[,.]?\s+)?(?:(?:echo|ecco|eco|echo)[,.]?\s+)?(?:let'?s\s+|please\s+|"
                   r"go\s+|start\s+)?day[\s-]?dream(?:ing)?\b(?:\s+(?:about|on|over|through))?[\s,:;.!-]*(?P<task>.*)$",
                   re.I | re.S)
STOP = re.compile(r"^\W*(?:(?:hey|ok|okay)[,.]?\s+)?(?:(?:echo|ecco|eco|echo)[,.]?\s+)?(?:wake up|stop daydreaming|"
                  r"stop day dreaming|end (?:the )?daydream)\b\W*(?:(?:echo|ecco|eco|echo)\W*)?$", re.I)

ENTER = "I'm going into a daydream now, so I can think this through with extra help. It may take a minute or two."
ENTER_ASK = "I'm going into a daydream now. What would you like me to think through?"
LEAVE = "I'm out of the daydream now."
LEAVE_STOPPED = "Okay, I've stopped daydreaming. I'm back to normal."
FAILED = "I couldn't finish that daydream, sorry. I'm back to normal; you can ask me again the usual way."


def parse_start(text: str) -> tuple[bool, str]:
    """(starts a daydream, the task if it came in the same message)."""
    m = START.match(text or "")
    return (True, m["task"].strip()) if m else (False, "")


REPORT = re.compile(r"^\W*(?:(?:hey|ok|okay)[,.]?\s+)?(?:(?:echo|ecco|eco|echo)[,.]?\s+)?(?:(?:give me|show me|what'?s|"
                    r"read me)\s+)?(?:the\s+|a\s+|your\s+)?day[\s-]?dream(?:ing)?\s+(?:report|scores?|stats|statistics)\W*$", re.I)


UNDO_TRAINING = re.compile(r"^\W*(?:(?:hey|ok|okay)[,.]?\s+)?(?:(?:echo|ecco|eco|echo)[,.]?\s+)?(?:please\s+)?"
                           r"undo (?:the |your |my )?(?:last |latest )?training\W*$", re.I)


def is_undo_training(text: str) -> bool:
    return bool(UNDO_TRAINING.match(text or ""))


def is_report(text: str) -> bool:
    """"daydream report" (checked before parse_start, which would otherwise take "report" as the task)."""
    return bool(REPORT.match(text or ""))


def is_stop(text: str) -> bool:
    return bool(STOP.match(text or ""))


# ---- settings and the developer-only gate ----
def load_config(path: Path) -> dict:
    try:
        c = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        c = {}
    d = c.get("daydream") or {}
    out = DEFAULTS | d
    for k in ("tower", "claude", "training"):
        out[k] = DEFAULTS[k] | (d.get(k) or {})
    return out


def machine_id() -> str:
    """This Mac's hardware UUID (IOPlatformUUID). Empty if it can't be read: then the gate stays closed."""
    try:
        out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True,
                             timeout=5).stdout
        m = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-F-]+)"', out)
        return m[1] if m else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def available(cfg: dict, this_machine: str | None = None) -> bool:
    """Daydream exists only on the owner's development Mac: switched on AND the owner id matches this machine."""
    if not cfg.get("enabled") or not cfg.get("owner_machine"):
        return False
    return (this_machine if this_machine is not None else machine_id()) == str(cfg["owner_machine"]).strip()


# ---- the work ----
Drafter = Callable[[str, list[str]], Awaitable[list[str | None] | None]]   # (task, parts) -> one draft per part, or None

PLAN_SCHEMA = {"type": "object", "properties": {"parts": {"type": "array", "items": {"type": "string"}}},
               "required": ["parts"]}


@dataclass
class Result:
    task: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    answer: str = ""
    parts: list[str] = field(default_factory=list)
    drafts: dict[str, list[str | None]] = field(default_factory=dict)   # helper name -> draft per part
    helpers: dict[str, str] = field(default_factory=dict)              # helper name -> used | offline | failed | off
    issues: list[dict] = field(default_factory=list)                    # the critic's mistakes / gaps / invented facts
    final_issues: list[dict] = field(default_factory=list)              # mistakes / invented facts in the merged answer
    started: float = field(default_factory=time.time)
    revised: bool = False                                               # the answer was rewritten after the final check
    ms: dict[str, int] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=dict)
    ok: bool = False


class Local:
    """Echo's own model through Ollama, with Echo's full instructions (personality + lessons)."""

    def __init__(self, host: str, model: str, options: dict, instructions: Callable[[], str], think: bool | None = False,
                 timeout_s: float = 180, transport: httpx.AsyncBaseTransport | None = None):
        self.host, self.model, self.options, self.instructions = host, model, options, instructions
        self.think, self.timeout_s, self.transport = think, timeout_s, transport
        self.tokens = {"prompt": 0, "reply": 0}

    async def ask(self, prompt: str, schema: dict | None = None, voice: bool = False) -> str:
        """voice=True: answer as Echo (full instructions); otherwise a plain working step with a short system line."""
        system = self.instructions() if voice else "You are a careful planner. Follow the instructions exactly."
        body = {"model": self.model, "stream": False, "keep_alive": "24h", "options": self.options,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if self.think is not None:
            body["think"] = self.think
        if schema:
            body["format"] = schema
        async with httpx.AsyncClient(base_url=self.host, timeout=self.timeout_s, transport=self.transport) as c:
            r = await c.post("/api/chat", json=body)
            if r.status_code >= 400 and schema and "structured output" in r.text:   # qwen3.5 on MLX: no "format"
                body.pop("format")
                body["messages"][-1]["content"] += f"\n\nAnswer with JSON only, matching: {json.dumps(schema)}"
                r = await c.post("/api/chat", json=body)
            r.raise_for_status()
            data = r.json()
        self.tokens["prompt"] += data.get("prompt_eval_count", 0)
        self.tokens["reply"] += data.get("eval_count", 0)
        return (data.get("message") or {}).get("content", "").strip()


def _parts_from(text: str, task: str, max_parts: int) -> list[str]:
    try:
        a, b = text.find("{"), text.rfind("}")
        parts = json.loads(text[a:b + 1]).get("parts") or []
        parts = [str(p).strip() for p in parts if str(p).strip()][:max_parts]
    except (ValueError, AttributeError):
        parts = []
    return parts or [task]


async def plan(local: Local, task: str, max_parts: int) -> list[str]:
    prompt = (f"Split this task into at most {max_parts} parts that can be worked on separately and together answer it "
              f"fully. Simple tasks may have just one part. Each part is one clear instruction.\n\nTASK:\n{task}\n\n"
              'Reply as JSON: {"parts": ["...", "..."]}')
    return _parts_from(await local.ask(prompt, PLAN_SCHEMA), task, max_parts)


def merge_prompt(task: str, parts: list[str], drafts: dict[str, list[str | None]], critique: str = "") -> str:
    lines = [f"Someone asked me to think this through carefully:\n{task}\n",
             "I split it into parts and have drafts for them. The drafts are my own private working notes and my "
             "helpers' notes: the person asking never saw them, so I never mention drafts, notes, parts or helpers, "
             "and never say they wrote or suggested anything. Drafts can be wrong or make things up; I keep only what "
             "is right and supported, and I don't invent facts about anyone's life.\n"]
    for i, p in enumerate(parts):
        lines.append(f"PART {i + 1}: {p}")
        for name, ds in drafts.items():
            if i < len(ds) and ds[i]:
                lines.append(f"  Draft ({name}): {ds[i]}")
    if critique:
        lines.append(f"\nREVIEW OF THE DRAFTS (mistakes, gaps, made-up facts):\n{critique}")
    lines.append("\nNow I write the final answer myself, in my own voice, as I always talk: warm, plain, first person. "
                 "Complete but not padded. No headings about 'drafts' or 'parts'.")
    return "\n".join(lines)


# ---- Claude (the owner's Pro account through core/claude_helper.py): drafts + critique ----
# Privacy: Claude gets the task and the parts Echo split it into, nothing else; the critique also gets the drafts,
# which were made from that same text. Two calls per daydream (all parts in one call) to spare the plan's limits.
DRAFTS_SCHEMA = {"type": "object", "properties": {"drafts": {"type": "array", "items": {"type": "string"}}},
                 "required": ["drafts"]}
CRITIQUE_SCHEMA = {"type": "object", "properties": {
    "issues": {"type": "array", "items": {"type": "object", "properties": {
        "part": {"type": "integer"}, "draft": {"type": "string"},
        "kind": {"type": "string", "enum": ["mistake", "gap", "invented"]}, "note": {"type": "string"}},
        "required": ["part", "draft", "kind", "note"]}},
    "summary": {"type": "string"}}, "required": ["issues", "summary"]}


def claude_drafter(helper) -> Drafter:
    async def draft(task: str, parts: list[str]) -> list[str | None] | None:
        if not helper.allowed("daydream"):
            return None   # off, logged out or capped: the others carry on
        listing = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(parts))
        out = await asyncio.to_thread(helper.ask, "daydream", (
            f"TASK:\n{task}\n\nPARTS:\n{listing}\n\nWrite a careful, correct draft answer for EACH part, in order, "
            f"one draft per part. Be concrete. Say plainly when something can't be known from the task; never invent "
            f"facts about the people involved."), DRAFTS_SCHEMA)
        if not isinstance(out, dict) or not isinstance(out.get("drafts"), list):
            return None
        return [str(d).strip() or None for d in out["drafts"][:len(parts)]]
    return draft


def claude_critic(helper):
    async def critique(task: str, parts: list[str], drafts: dict[str, list[str | None]]) -> dict | None:
        if not helper.allowed("daydream"):
            return None
        lines = [f"TASK:\n{task}\n"]
        for i, p in enumerate(parts):
            lines.append(f"PART {i + 1}: {p}")
            for name, ds in drafts.items():
                if i < len(ds) and ds[i]:
                    lines.append(f"  [{name}] {ds[i]}")
        lines.append("\nReview these drafts. List every mistake (wrong facts, wrong maths, bad logic), gap (something "
                     "the task needs that no draft covers) and invented fact (a claim the task gives no basis for, "
                     "especially about the people involved). Name the draft by its label in brackets. Then a short "
                     "summary of which draft is best for each part. Don't rewrite the answer.")
        out = await asyncio.to_thread(helper.ask, "daydream", "\n".join(lines), CRITIQUE_SCHEMA)
        return out if isinstance(out, dict) and "issues" in out else None
    return critique


def claude_checker(helper):
    """The final check: Claude reads Echo's merged answer (with the task, nothing else) for mistakes and invented
    facts. Found 2026-11-04 in the first real run: the drafts were right and reviewed, but the merge itself said
    "$250 a month reaches $3,000 in eight months" (it takes twelve)."""
    async def check(task: str, answer: str) -> dict | None:
        if not helper.allowed("daydream"):
            return None
        out = await asyncio.to_thread(helper.ask, "daydream", (
            f"TASK:\n{task}\n\nANSWER:\n{answer}\n\nCheck this answer. List only real problems: mistakes (wrong "
            f"facts, wrong maths: recompute every number), and invented facts (claims the task gives no basis for). "
            f"Use part 0 and draft \"answer\" for each. Empty list if it's right. Summary: one line."), CRITIQUE_SCHEMA)
        return out if isinstance(out, dict) and "issues" in out else None
    return check


# ---- the tower (Dream Bridge, protocol rev e) ----
BRIDGE = "http://127.0.0.1:47803"   # the bridge's local control API; Echo never talks to the tower directly


async def wake_tower(wait_s: float, transport: httpx.AsyncBaseTransport | None = None, poll_s: float = 3) -> bool:
    """the owner, 11-04: "if the tower PC is off, issue a wake command just like [the dream schedule] does". If the paired
    tower isn't on the network, the bridge sends its Wake-on-LAN packet (the same one as 18:00), then this waits until
    the tower's beacon and encrypted link are back, at most wait_s. True = the tower is there."""
    async with httpx.AsyncClient(timeout=5, transport=transport) as c:
        def up(st: dict) -> bool:
            return bool((st.get("tower") or {}).get("on_network") and (st.get("link") or {}).get("ok"))
        try:
            st = (await c.get(f"{BRIDGE}/status")).json()
        except (httpx.HTTPError, ValueError):
            return False                       # no bridge: nothing to wake with
        if up(st):
            return True
        if not st.get("paired"):
            return False
        try:
            await c.post(f"{BRIDGE}/wake")
            log.info("daydream: tower was off, wake sent")
        except httpx.HTTPError:
            return False
        end = time.time() + wait_s
        while time.time() < end:
            await asyncio.sleep(poll_s)
            try:
                if up((await c.get(f"{BRIDGE}/status")).json()):
                    log.info("daydream: tower is up after a wake")
                    return True
            except (httpx.HTTPError, ValueError):
                pass
        return False


def tower_drafter(cfg: dict, transport: httpx.AsyncBaseTransport | None = None) -> Drafter:
    """The tower's model drafts every part through the bridge (daydream_request → daydream_draft)."""
    async def draft(task: str, parts: list[str], did: str = "") -> list[str | None] | None:
        t = cfg["tower"]
        if t.get("wake", True) and not await wake_tower(t.get("wake_wait_s", 150), transport):
            log.info("tower drafts: tower didn't come up after a wake")
            return None
        async with httpx.AsyncClient(timeout=t["timeout_s"] + 15, transport=transport) as c:
            r = await c.post(f"{BRIDGE}/daydream/request", json={
                "daydream_id": did or uuid.uuid4().hex[:16], "task": task, "parts": parts,
                "max_tokens": t["max_tokens"], "deadline_s": t["timeout_s"]})
            out = r.json()
        if out.get("status") != "ok":
            log.info("tower drafts: %s %s", out.get("status"), out.get("reason", ""))
            return None
        return out.get("drafts") or None
    draft.wants_id = True
    return draft


def activity(res: "Result", stage: str, seq: int, simulated: bool = False) -> dict:
    """One rev e daydream_activity payload: numbers and states only, never the task or any answer text."""
    kinds = {"mistake": 0, "gap": 0, "invented": 0}
    for x in res.issues + res.final_issues:
        if x.get("kind") in kinds:
            kinds[x["kind"]] += 1
    return {"daydream_id": res.id, "seq": seq, "simulated": simulated, "stage": stage,
            "active": stage not in ("done", "stopped", "failed"), "parts": len(res.parts), "helpers": dict(res.helpers),
            "ms": dict(res.ms), "elapsed_ms": res.ms.get("total") or round((time.time() - res.started) * 1000),
            "tokens": dict(res.tokens), "issues": kinds, "revised": res.revised}


def bridge_reporter(transport: httpx.AsyncBaseTransport | None = None, simulated: bool = False):
    """on_event for run(): posts each stage to the bridge, which forwards it to the tower's Dream Observatory.
    Never waits more than a second and never fails the daydream."""
    seq = {"n": 0}

    async def report(res: "Result", stage: str) -> None:
        seq["n"] += 1
        try:
            async with httpx.AsyncClient(timeout=1.0, transport=transport) as c:
                await c.post(f"{BRIDGE}/daydream/activity", json=activity(res, stage, seq["n"], simulated))
        except Exception as e:  # noqa: BLE001
            log.debug("daydream activity not reported: %s", type(e).__name__)
    return report


def critique_text(c: dict) -> str:
    rows = [f"- part {x.get('part')}, {x.get('draft')}: {x.get('kind')}: {x.get('note')}" for x in c.get("issues") or []]
    return "\n".join(rows + ([f"Summary: {c['summary']}"] if c.get("summary") else []))


async def run(task: str, local: Local, cfg: dict, drafters: dict[str, Drafter] | None = None,
              critic: Callable[[str, list[str], dict], Awaitable[dict | None]] | None = None,
              checker: Callable[[str, str], Awaitable[dict | None]] | None = None,
              on_event: Callable[["Result", str], Awaitable[None]] | None = None, res: "Result | None" = None) -> Result:
    """One daydream. Never raises for a helper problem: a helper that is off, offline, capped or failing is recorded
    and skipped. Only a failure of Echo's own model ends it (ok=False)."""
    res = res or Result(task=task)
    t0 = res.started

    async def emit(stage: str) -> None:
        if on_event:
            try:
                await on_event(res, stage)
            except Exception:  # noqa: BLE001
                pass
    await emit("enter")
    await emit("plan")
    start = dict(local.tokens)   # one Local serves many daydreams: count only this one
    try:
        res.parts = await plan(local, task, int(cfg["max_parts"]))
    except Exception as e:  # noqa: BLE001
        log.warning("daydream plan failed (%s): one part", type(e).__name__)
        res.parts = [task]
    res.ms["plan"] = round((time.time() - t0) * 1000)
    for n in (drafters or {}):
        res.helpers[n] = "working"
    await emit("drafts")

    t1 = time.time()
    names = list((drafters or {}).keys())

    async def one(name: str):
        try:
            d = drafters[name]
            return await (d(task, res.parts, res.id) if getattr(d, "wants_id", False) else d(task, res.parts))
        except Exception as e:  # noqa: BLE001
            log.warning("daydream helper %s failed: %s", name, type(e).__name__)
            return e
    outs = await asyncio.gather(*(one(n) for n in names)) if names else []
    for name, out in zip(names, outs):
        if isinstance(out, Exception):
            res.helpers[name] = "failed"
        elif not out or not any(out):
            res.helpers[name] = "unavailable"
        else:
            res.helpers[name] = "used"
            res.drafts[name] = list(out) + [None] * (len(res.parts) - len(out))
    res.ms["drafts"] = round((time.time() - t1) * 1000)

    missing = [i for i in range(len(res.parts)) if not any(d[i] for d in res.drafts.values())]
    if missing:   # nobody answered these parts: Echo drafts them himself
        t2 = time.time()
        mine: list[str | None] = [None] * len(res.parts)
        for i in missing:
            try:
                mine[i] = await local.ask(f"Overall task: {task}\n\nWork on this part only, carefully and correctly:\n"
                                          f"{res.parts[i]}")
            except Exception as e:  # noqa: BLE001
                log.warning("daydream local draft failed: %s", type(e).__name__)
        res.drafts["echo"] = mine
        res.helpers["echo"] = "used"
        res.ms["echo_drafts"] = round((time.time() - t2) * 1000)

    critique = ""
    if critic and len(res.drafts) > 0:
        res.helpers["critic"] = "working"
        await emit("critique")
        t3 = time.time()
        try:
            c = await critic(task, res.parts, res.drafts)
            if c:
                res.issues = [x for x in c.get("issues") or [] if isinstance(x, dict)]
                critique = critique_text(c)
            res.helpers["critic"] = "used" if c else "unavailable"
        except Exception as e:  # noqa: BLE001
            log.warning("daydream critic failed: %s", type(e).__name__)
            res.helpers["critic"] = "failed"
        res.ms["critique"] = round((time.time() - t3) * 1000)

    await emit("merge")
    t4 = time.time()
    try:
        res.answer = await local.ask(merge_prompt(task, res.parts, res.drafts, critique), voice=True)
        res.ok = bool(res.answer)
    except Exception as e:  # noqa: BLE001
        log.warning("daydream merge failed: %s", type(e).__name__)
    res.ms["merge"] = round((time.time() - t4) * 1000)

    if checker and res.ok:
        res.helpers["checker"] = "working"
        await emit("check")
    if checker and res.ok:   # final check of the answer itself; one revision if it found mistakes or invented facts
        t5 = time.time()
        try:
            c = await checker(task, res.answer)
            res.helpers["checker"] = "used" if c else "unavailable"
            bad = [x for x in (c or {}).get("issues") or [] if isinstance(x, dict) and x.get("kind") in ("mistake", "invented")]
            res.final_issues = bad
            if bad:
                fixed = await local.ask(
                    f"Someone asked me:\n{task}\n\nMy answer was:\n{res.answer}\n\nA careful check found these "
                    f"problems:\n" + "\n".join(f"- {x.get('kind')}: {x.get('note')}" for x in bad) +
                    "\n\nI rewrite my answer with these fixed, in my own voice, changing nothing else.", voice=True)
                if fixed:
                    res.answer, res.revised = fixed, True
        except Exception as e:  # noqa: BLE001
            log.warning("daydream final check failed: %s", type(e).__name__)
            res.helpers["checker"] = "failed"
        res.ms["check"] = round((time.time() - t5) * 1000)
    res.ms["total"] = round((time.time() - t0) * 1000)
    res.tokens = {k: local.tokens[k] - start.get(k, 0) for k in local.tokens}
    await emit("done" if res.ok else "failed")
    return res


class Session:
    """Daydream state for one conversation: idle → waiting for a task → working → idle."""

    def __init__(self, report=None):
        self.state = "idle"          # idle | waiting | working
        self.job: asyncio.Task | None = None
        self.since = 0.0
        self.result: Result | None = None
        self.report = report or bridge_reporter()   # stage events → bridge → tower's Dream Observatory

    def enter(self, waiting: bool) -> None:
        self.state, self.since = ("waiting" if waiting else "working"), time.time()

    def leave(self) -> None:
        if self.job and not self.job.done():
            self.job.cancel()
        self.state, self.job = "idle", None
