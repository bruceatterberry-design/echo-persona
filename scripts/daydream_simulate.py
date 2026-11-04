"""Simulated daydream for the joint Mac ⇄ tower test (protocol rev e). Uses no model and no Claude quota.

Sends a realistic sequence of daydream_activity events (simulated: true) through the real, encrypted Dream Bridge, so
the tower's Dream Observatory can show a daydream as it happens, and (with --request) one real daydream_request so
the tower's model drafts two harmless parts.

    ~/EchoPersona/.venv/bin/python scripts/daydream_simulate.py [--seconds 40] [--request] [--stop]
--stop ends with "stopped" (as if the owner said "wake up") instead of "done".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx                                  # noqa: E402

from core import daydream                     # noqa: E402

TASK = "SIMULATED: plan a relaxed Saturday: a morning walk, lunch, and an easy afternoon project."
PARTS = ["Suggest a morning walk plan for about an hour.", "Suggest an easy afternoon project for two people."]


async def post(path: str, body: dict, timeout: float = 5) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(daydream.BRIDGE + path, json=body)
        return r.json()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=40)
    ap.add_argument("--request", action="store_true", help="also ask the tower's model to draft two parts")
    ap.add_argument("--stop", action="store_true", help='end with "stopped" instead of "done"')
    a = ap.parse_args()
    res = daydream.Result(task=TASK, parts=list(PARTS))
    # (stage, share of the run, what changes when the stage starts)
    plan = [("enter", .02, {}), ("plan", .12, {}),
            ("drafts", .30, {"helpers": {"tower": "working", "claude": "working"}}),
            ("critique", .18, {"helpers": {"claude": "used", "critic": "working"},
                               "issues": [{"kind": "gap"}, {"kind": "invented"}]}),
            ("merge", .25, {"helpers": {"critic": "used"}}),
            ("check", .13, {"helpers": {"checker": "working"}})]
    seq, ok, t0 = 0, 0, time.time()
    tower_out = None
    for stage, share, change in plan:
        res.helpers.update(change.get("helpers", {}))
        res.issues += change.get("issues", [])
        seq += 1
        res.ms["total"] = 0
        ev = daydream.activity(res, stage, seq, simulated=True)
        ev["elapsed_ms"] = round((time.time() - t0) * 1000)
        r = await post("/daydream/activity", ev)
        ok += bool(r.get("queued"))
        print(f"{ev['elapsed_ms'] / 1000:5.1f}s  {stage:<8} helpers={ev['helpers']}  bridge={r}")
        t_stage = time.time()
        if stage == "drafts" and a.request:
            tower_out = await post("/daydream/request", {"daydream_id": res.id, "task": TASK, "parts": PARTS,
                                                         "max_tokens": 300, "deadline_s": 90}, timeout=110)
            res.helpers["tower"] = "used" if tower_out.get("status") == "ok" else "unavailable"
            print(f"        tower drafts: status={tower_out.get('status')} model={tower_out.get('model')} "
                  f"ms={tower_out.get('ms')} reason={tower_out.get('reason', '')}")
        if stage == "drafts" and not a.request:
            res.helpers["tower"] = "used"
        await asyncio.sleep(max(0, share * a.seconds - (time.time() - t_stage)))
        res.ms[stage] = round((time.time() - t_stage) * 1000)
    res.final_issues = [{"kind": "mistake"}]
    res.revised = True
    res.helpers["checker"] = "used"
    res.ms["total"] = round((time.time() - t0) * 1000)
    res.tokens = {"prompt": 3400, "reply": 260}
    end = "stopped" if a.stop else "done"
    r = await post("/daydream/activity", daydream.activity(res, end, seq + 1, simulated=True))
    ok += bool(r.get("queued"))
    print(f"{res.ms['total'] / 1000:5.1f}s  {end:<8} issues={daydream.activity(res, end, 0)['issues']} bridge={r}")
    print(json.dumps({"daydream_id": res.id, "events_queued": ok, "events": seq + 1,
                      "tower_drafts": tower_out and {k: tower_out.get(k) for k in ("status", "model", "ms", "tokens")}}))
    return 0 if ok == seq + 1 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
