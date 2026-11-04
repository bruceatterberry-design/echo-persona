"""End-to-end process test with the Synthetic Dreams collection, in a throwaway sandbox.

    cd ~/EchoPersona/dream_bridge && .venv/bin/python scripts/process_test.py

Runs the REAL Mac bridge code (pairing, sealed messages, receipt, dream reader, approval, Echo's lesson cap) against a
stand-in "dream machine" that follows BRIDGE_PROTOCOL.md rev d. Uses temporary folders and a copy-free fake Echo:
nothing touches the owner's memory, Echo's lessons, the live bridge's queue or the real tower. The insights below are what
a tower *might* distil; the real tower writes its own.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
SYN = HERE.parent / "docs" / "dream" / "synthetic"

tmp = Path(tempfile.mkdtemp(prefix="dream-process-test-"))
os.environ["DREAM_BRIDGE_DATA"] = str(tmp / "dream")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from bridge import crypto, envelope, reader, service, store, synthetic  # noqa: E402

store.STATE_DIR = tmp / "state"
sys.path.insert(0, str(HERE))
from tests.test_bridge import TOWER_ID, FakeTower  # noqa: E402

# what a dream machine might distil from collection 1: one general insight per category (+ deliberately bad ones)
INSIGHTS = [
    ("children", "When someone shy or grieving talks to me, I listen patiently first and let them lead, instead of pushing."),
    ("love", "Love often shows in small daily habits; when people mention them, I notice and appreciate them."),
    ("kindness", "When someone is upset, I listen before offering solutions; being heard often helps most."),
    ("computing", "For stacked discounts I multiply, not add: two 20% discounts are 36% off. I explain steps kindly."),
    ("garden", "When I link two things, I say 'may have helped' unless the cause is shown, like bees and squash."),
]
BAD = [
    ("names", "the owner prefers patient listening when he talks about his cats."),
    ("past", "I remember planting tomatoes after the last frost every spring."),
    ("long", "x" * 230),
]


class FakeEchoLessons:
    """Stands in for Echo's POST/DELETE /api/lessons/insight with the same 3-insight cap."""

    def __init__(self):
        self.lessons = []

    def post(self, url, json, timeout):
        if url.endswith("/api/lessons/insight"):
            if len([x for x in self.lessons if x["active"]]) >= 3:
                return httpx.Response(409, json={"detail": "Echo already has 3 dream insights; switch one off first"})
            self.lessons.append({**json, "active": True})
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    def delete(self, url, timeout):
        src = url.rsplit("/", 1)[1]
        for x in self.lessons:
            if x["source"] == src and x["active"]:
                x["active"] = False
                return httpx.Response(200, json={"dropped": True})
        return httpx.Response(200, json={"dropped": False})


def step(n, text):
    print(f"\n[{n}] {text}")


async def main() -> int:
    ok = True

    def expect(cond, what):
        nonlocal ok
        print(("   PASS  " if cond else "   FAIL  ") + what)
        ok &= bool(cond)

    step(1, "Check each synthetic file (one chunk at a time) with the Mac checker")
    files = sorted(glob.glob(str(SYN / "synthetic_dreams_*.json")))
    total = 0
    for f in files:
        r = synthetic.check(f)
        total += r.get("records", 0)
        expect(r["ok"], f"{Path(f).name}: {r.get('records')} records, {r.get('flawed')} flawed, "
                        f"{len(json.dumps(json.load(open(f)))) // 1024} KB")
    expect(total == 30 and len(files) == 5, f"{total} records in {len(files)} chunks")

    step(2, "Pair the Mac bridge with the stand-in dream machine (4-digit code, commit-then-reveal)")
    tower = FakeTower()
    b = service.Bridge(transport=httpx.MockTransport(tower.handler))
    b.disc.towers[TOWER_ID] = {"device_id": TOWER_ID, "name": "stand-in dream machine", "ip": "192.168.1.50",
                               "port": 47801, "proto": 1, "pairing_open": True, "paired_with": None, "seen": time.time()}
    s = await b.pair_start()
    r = await b.pair_confirm()
    expect(r["paired"] and s["code"] == tower.code, f"paired, both screens showed {s['code']}")

    step(3, "Conversation pause: Echo starts talking, dream machine is told to pause")
    expect((await b.set_conversation(True))["sent"], "pause sent")
    expect((await b.set_conversation(False))["sent"], "resume sent")

    step(4, "Dream machine sends its distilled insights (origin: synthetic), sealed, with checksum")
    props = [{"proposal_id": f"ins-{k}", "kind": "insight", "insight": t, "reason": f"pattern across the {k} dreams",
              "basis": {"synthetic_records": 6, "examples": ["syn-0101"]}, "confidence": 0.7,
              "scores": {x: 0.7 for x in reader.WEIGHTS}} for k, t in INSIGHTS + BAD]
    pl = {"dream_id": "process-test-1", "origin": "synthetic", "model": "stand-in",
          "narrative": "[Speculation: insights distilled from synthetic dreams, not memories.]", "proposals": props}
    pl["sha256"] = service.package_sha256(pl)
    msg = {**envelope.make("dream_result", pl), "source": "dream", "destination": "echo"}
    b.read_and_report = _noop   # run the reader by hand below, so each step is visible
    lan = TestClient(service.lan_app(b))
    rep = crypto.unseal(tower.key, "RESP", "/v1/msg", lan.post("/v1/msg", json=crypto.seal(tower.key, TOWER_ID, "POST", "/v1/msg", msg)).json())
    expect(rep["payload"].get("receipt", {}).get("sha256") == pl["sha256"], "Mac stored it durably and sent a matching receipt")
    again = crypto.unseal(tower.key, "RESP", "/v1/msg", lan.post("/v1/msg", json=crypto.seal(
        tower.key, TOWER_ID, "POST", "/v1/msg", {**msg, "message_id": "resend-0001"})).json())
    expect(again["payload"]["reason"] == "already stored", "a resend is recognised, not stored twice")

    step(5, "Dream reader checks every insight")
    await service.Bridge.read_and_report(b)
    st = dict(reader.db().execute("SELECT proposal_id, status FROM proposals").fetchall())
    notes = dict(reader.db().execute("SELECT proposal_id, check_notes FROM proposals").fetchall())
    for k, _ in INSIGHTS:
        expect(st[f"ins-{k}"] == "pending", f"ins-{k}: queued for the owner")
    for k, _ in BAD:
        expect(st[f"ins-{k}"] == "invalid", f"ins-{k}: rejected ({notes[f'ins-{k}']})")
    res = [m for m in tower.received if m["task_type"] == "ack" and m["in_reply_to"] == msg["message_id"]]
    expect(res and len(res[0]["payload"]["results"]) == 8, "dream machine got per-insight results back")

    step(6, "the owner approves insights: they become Echo lessons, max 3, never memories")
    fe = FakeEchoLessons()
    httpx.post, httpx.delete = fe.post, fe.delete
    for k, _ in INSIGHTS[:3]:
        expect(reader.decide(f"ins-{k}", True)["status"] == "applied", f"ins-{k} → lesson")
    try:
        reader.decide(f"ins-{INSIGHTS[3][0]}", True)
        expect(False, "4th insight should be blocked by the cap")
    except OverflowError:
        expect(True, "4th insight blocked: Echo keeps at most 3 (stays pending)")
    reader.retire(f"ins-{INSIGHTS[0][0]}")
    expect(reader.decide(f"ins-{INSIGHTS[3][0]}", True)["status"] == "applied", "after retiring one, the 4th fits")
    active = [x for x in fe.lessons if x["active"]]
    expect(len(active) == 3 and sum(len(x["text"]) for x in active) <= 600,
           f"Echo would hold {len(active)} insight lessons, {sum(len(x['text']) for x in active)} characters in all")
    expect(reader.decide(f"ins-{INSIGHTS[4][0]}", False)["status"] == "rejected", "the owner can say no")

    step(7, "Nothing private or real was touched")
    expect(not store.STATE_DIR.samefile(Path.home() / "EchoPersona" / "data" / "dream-bridge") if (Path.home() / "EchoPersona" / "data" / "dream-bridge").exists() else True,
           f"sandbox only: {tmp}")
    expect(not any(m["task_type"] == "memory_batch" for m in tower.received), "no memory batch was sent")

    print("\nRESULT:", "ALL PASS" if ok else "SOME CHECKS FAILED", f"(sandbox {tmp})")
    return 0 if ok else 1


async def _noop():
    return None


if __name__ == "__main__":
    async def nosleep(_s):
        return None
    service.asyncio.sleep = nosleep   # the stand-in answers instantly; no 2 s waits during pairing
    sys.exit(asyncio.run(main()))
