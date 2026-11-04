"""Echo Dream Bridge, Mac side. A separate service: Echo never waits on it.

- Discovery: UDP 47800 beacons on the wired port (en0).
- LAN API on <en0 ip>:47802 for the paired tower only (encrypted, section 4).
- Control API on 127.0.0.1:47803 for this Mac only (status, pair, wake). Used by `python -m bridge`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import crypto, discovery, envelope, export, reader, store

log = logging.getLogger("dream_bridge")

IFACE = os.environ.get("DREAM_BRIDGE_IFACE", "en0")
LAN_PORT = 47802
CONTROL_PORT = 47803
HEARTBEAT_S = 60
PAIR_WAIT_S = 300
MAX_BODY = 8 * 1024 * 1024
TZ = ZoneInfo("America/Chicago")
WAKE_TIMES = ((18, 0), (18, 5))   # protocol section 8: wake at 18:00, retry once at 18:05
CONV_QUIET_S = 180                # no ping from Echo for 3 min = conversation over
CONV_RESEND_S = 600               # during a long conversation, resend active:true every 10 min (tower pause expires at 30)
DREAM_WINDOW = (18, 22)           # hours, America/Chicago: the evening batch is pushed once inside this window
WAKE_GRACE_MIN = 5                # still send if the Mac was busy/restarting a few minutes late


def package_sha256(payload: dict) -> str:
    """Protocol section 7b: SHA-256 of the payload without its own sha256 key, canonical JSON."""
    body = {k: v for k, v in payload.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def wake_due(now: datetime, sent: set[str]) -> str | None:
    """Which wake slot (e.g. "2026-11-03 18:05") is due now and not yet used, else None."""
    for h, m in WAKE_TIMES:
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        slot = start.strftime("%Y-%m-%d %H:%M")
        if slot not in sent and 0 <= (now - start).total_seconds() < WAKE_GRACE_MIN * 60:
            return slot
    return None


PAIR_ERRORS = {   # rev c, section 3
    401: "The tower rejected the commitment or proof (attempt used up).",
    403: "The tower's pairing window isn't open.",
    404: "The tower has no pairing attempt from this Mac.",
    409: "Pairing steps arrived in the wrong order.",
    422: "The tower couldn't read the pairing request.",
    429: "3 attempts used. Reopen 'Pair with Echo' on the tower.",
}


def wake_packet(hw_mac: str) -> bytes:
    raw = bytes.fromhex(hw_mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError("hardware address must be 6 bytes")
    return b"\xff" * 6 + raw * 16


class Bridge:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.me = store.identity()["device_id"]
        self.name = socket.gethostname().split(".")[0]
        self.pair = store.load_pair()
        self.disc = discovery.Discovery(self.me, IFACE, self.beacon)
        if transport is None:   # rev c: the tower binds a pairing to the calling IP, so always leave via en0
            addr = discovery.interface_ip(IFACE)
            transport = httpx.AsyncHTTPTransport(local_address=addr[0]) if addr else None
        self.http = httpx.AsyncClient(timeout=10, transport=transport)
        self.replay = envelope.Replay()
        self.pending: dict | None = None      # one pairing attempt at a time
        self.link = {"ok": False, "last_ok": None, "last_error": None, "tower_state": None}
        self.conversation_active = False
        self.wake_slots: set[str] = set()
        self.pushed_on: str | None = None      # date of tonight's automatic batch
        self.last_ping = 0.0                   # last "someone is talking" ping from Echo core
        self.conv_sent_at = 0.0

    # ---- discovery ----
    def beacon(self) -> dict:
        return {"svc": "echo-dream", "proto": envelope.PROTO, "role": "mac", "device_id": self.me,
                "name": self.name, "port": LAN_PORT,
                "paired_with": self.pair["peer_id"] if self.pair else None, "pairing_open": False}

    def tower(self) -> dict | None:
        """The paired tower's latest beacon (newest IP wins)."""
        if not self.pair:
            return None
        return next((t for t in self.disc.live_towers() if t["device_id"] == self.pair["peer_id"]), None)

    def _url(self, t: dict, path: str) -> str:
        return f"http://{t['ip']}:{t['port']}{path}"

    # ---- pairing (section 3) ----
    async def pair_start(self, device_id: str | None = None) -> dict:
        open_ = [t for t in self.disc.live_towers() if t["pairing_open"]]
        if device_id:
            open_ = [t for t in open_ if t["device_id"] == device_id]
        if not open_:
            raise HTTPException(404, "No tower is in pairing mode. On the tower, open 'Pair with Echo' first.")
        if len(open_) > 1:
            raise HTTPException(409, {"choose": [{"device_id": t["device_id"], "name": t["name"], "ip": t["ip"]} for t in open_]})
        t = open_[0]
        eph = crypto.Ephemeral()
        # rev c: commit first, reveal the nonce only after the tower's pub + nonce are fixed
        r = await self.http.post(self._url(t, "/v1/pair/start"), json={
            "device_id": self.me, "name": self.name, "pub": crypto.b64e(eph.pub),
            "commit": crypto.b64e(crypto.commitment(eph.pub, eph.nonce))})
        if r.status_code != 200:
            raise HTTPException(502, PAIR_ERRORS.get(r.status_code, f"Tower refused pairing ({r.status_code})."))
        a = r.json()
        try:
            tpub, tnonce = crypto.b64d(a["pub"]), crypto.b64d(a["nonce"])
            if a["device_id"] != t["device_id"] or len(tnonce) != 16:
                raise ValueError("mismatch")
            key = crypto.derive_key(eph.shared(tpub), eph.nonce, tnonce)
        except Exception:
            raise HTTPException(502, "Tower sent a bad pairing answer.")
        r = await self.http.post(self._url(t, "/v1/pair/reveal"), json={"device_id": self.me, "nonce": crypto.b64e(eph.nonce)})
        if r.status_code != 200 or r.json().get("status") != "revealed":
            raise HTTPException(502, PAIR_ERRORS.get(r.status_code, f"Tower refused the reveal ({r.status_code})."))
        code = crypto.pair_code(eph.pub, tpub, eph.nonce, tnonce)
        self.pending = {"tower": t, "key": key, "code": code, "hw_mac": a.get("hw_mac"),
                        "name": a.get("name", t["name"]), "started": time.time()}
        store.journal(event="pair_start", tower=t["device_id"], ip=t["ip"])
        return {"code": code, "tower": t["name"], "ip": t["ip"]}

    async def pair_confirm(self) -> dict:
        p = self.pending
        if not p or time.time() - p["started"] > PAIR_WAIT_S:
            self.pending = None
            raise HTTPException(410, "No pairing in progress (or it expired). Start again.")
        t, key = p["tower"], p["key"]
        deadline = time.time() + PAIR_WAIT_S
        while time.time() < deadline:
            r = await self.http.post(self._url(t, "/v1/pair/confirm"),
                                     json={"device_id": self.me, "proof": crypto.proof(key, "mac-confirm")})
            if r.status_code != 200:
                self.pending = None
                store.journal(event="pair_failed", tower=t["device_id"], status=r.status_code)
                raise HTTPException(403, PAIR_ERRORS.get(r.status_code, "The tower rejected the pairing.") + " Pairing cancelled.")
            a = r.json()
            if a.get("status") == "rejected":
                self.pending = None
                store.journal(event="pair_failed", tower=t["device_id"], reason="doesn't match clicked")
                raise HTTPException(403, "'Doesn't match' was clicked on the tower. Pairing cancelled, nothing saved.")
            if a.get("status") == "waiting":
                await asyncio.sleep(2)
                continue
            if not crypto.proof_ok(key, "tower-confirm", a.get("proof", "")):
                self.pending = None
                store.journal(event="pair_failed", tower=t["device_id"], reason="tower proof")
                raise HTTPException(403, "The tower's answer didn't match. Pairing cancelled.")
            self.pair = {"device_id": self.me, "peer_id": t["device_id"], "peer_name": p["name"],
                         "peer_hw_mac": p["hw_mac"], "key": crypto.b64e(key), "paired_at": time.time()}
            store.save_pair(self.pair)
            self.pending = None
            store.journal(event="paired", tower=t["device_id"], name=p["name"])
            asyncio.get_running_loop().create_task(self.heartbeat_once())
            return {"paired": True, "tower": p["name"]}
        self.pending = None
        raise HTTPException(408, "Timed out waiting for Match on the tower.")

    def unpair(self) -> dict:
        had = store.forget_pair()
        self.pair = None
        self.link.update(ok=False, tower_state=None)
        store.journal(event="unpaired")
        return {"unpaired": had}

    # ---- talking to the tower (section 4) ----
    def key(self) -> bytes:
        return crypto.b64d(self.pair["key"])

    async def send(self, env: dict, timeout: float | None = None) -> dict:
        t = self.tower()
        if not t:
            raise ConnectionError("tower not seen on the network")
        body = crypto.seal(self.key(), self.me, "POST", "/v1/msg", env)
        r = await self.http.post(self._url(t, "/v1/msg"), json=body,   # default 10 s; daydream drafts need longer
                                 **({"timeout": timeout} if timeout else {}))
        if r.status_code != 200:
            raise ConnectionError(f"tower answered {r.status_code}")
        reply = crypto.unseal(self.key(), "RESP", "/v1/msg", r.json())
        if r.json().get("from") != self.pair["peer_id"]:
            raise crypto.Rejected("reply from wrong device")
        rep = envelope.Envelope.model_validate(reply).model_dump()
        if rep["in_reply_to"] != env["message_id"]:
            raise crypto.Rejected("reply to a different message")
        return rep

    async def heartbeat_once(self) -> None:
        if not self.pair:
            return
        try:
            rep = await self.send(envelope.make("heartbeat", {"state": "IDLE", "busy": self.conversation_active}))
            self.link.update(ok=True, last_ok=time.time(), last_error=None,
                             tower_state=rep.get("payload", {}).get("state"))
        except Exception as e:
            if self.link["ok"]:
                log.warning("link to tower lost: %s", e)
            self.link.update(ok=False, last_error=str(e)[:200])

    async def read_and_report(self) -> None:
        """Run the dream reader, then tell the tower which proposals passed (section 7b 'results')."""
        await asyncio.sleep(0.2)   # let the receipt reply leave first
        try:
            new = await asyncio.to_thread(reader.process_inbox)
        except Exception as e:
            log.exception("dream reader failed: %s", e)
            return
        p = store.STATE_DIR / "unsent_results.json"
        queue = (json.loads(p.read_text()) if p.exists() else []) + new
        left = []
        for r in queue:
            try:
                await self.send(envelope.make("ack", {"accepted": True, "reason": "checked", "dream_id": r["dream_id"],
                                                      "results": r["results"]}, in_reply_to=r["message_id"], status="done"))
            except Exception:
                left.append(r)
        store._write_private(p, left) if left or p.exists() else None
        if new:
            log.info("read %d dream(s); results sent=%d, waiting=%d", len(new), len(queue) - len(left), len(left))

    async def conversation_loop(self) -> None:
        """Echo pings on every turn; tell the tower to pause, and to resume after 3 quiet minutes."""
        while True:
            now = time.time()
            talking = now - self.last_ping < CONV_QUIET_S
            if talking and (not self.conversation_active or now - self.conv_sent_at > CONV_RESEND_S):
                r = await self.set_conversation(True)
                if r.get("sent"):
                    self.conv_sent_at = now
            elif not talking and self.conversation_active:
                await self.set_conversation(False)
            await asyncio.sleep(2)

    # ---- Daydream (rev e) ----
    ACTIVITY_KEYS = {"daydream_id": str, "seq": int, "simulated": bool, "stage": str, "active": bool, "parts": int,
                     "helpers": dict, "ms": dict, "elapsed_ms": int, "tokens": dict, "issues": dict, "revised": bool}
    STAGES = ("enter", "plan", "drafts", "critique", "merge", "check", "done", "stopped", "failed")

    def daydream_activity(self, ev: dict) -> dict:
        if not isinstance(ev, dict):
            raise HTTPException(422, "not an object")
        pl = {k: ev[k] for k, t in self.ACTIVITY_KEYS.items() if isinstance(ev.get(k), t)}   # no other field, no text
        if not pl.get("daydream_id") or pl.get("stage") not in self.STAGES or len(pl["daydream_id"]) > 64:
            raise HTTPException(422, "daydream_id and a known stage are needed")
        pl["at"] = envelope.now_iso()
        if not self.pair:
            return {"queued": False, "reason": "not paired"}

        async def go():
            try:
                await self.send(envelope.make("daydream_activity", pl))
            except Exception as e:  # noqa: BLE001  (the tower may be asleep: Echo never waits for this)
                log.info("daydream activity not delivered: %s", str(e)[:120])
        self.activity_task = asyncio.get_running_loop().create_task(go())   # kept so it isn't collected mid-send
        return {"queued": True}

    async def daydream_request(self, req: dict) -> dict:
        did = str(req.get("daydream_id", ""))[:64]
        parts = [str(p)[:500] for p in (req.get("parts") or [])][:4]
        if not self.pair or not did or not parts:
            return {"daydream_id": did, "status": "unavailable", "drafts": [], "reason": "not paired" if not self.pair else "bad request"}
        deadline = min(int(req.get("deadline_s") or 90), 300)
        pl = {"daydream_id": did, "task": str(req.get("task", ""))[:4000], "parts": parts,
              "max_tokens": min(int(req.get("max_tokens") or 800), 2000), "deadline_s": deadline}
        try:
            # the tower answers up to 3 s before deadline_s (TOWER rev e note): wait a little longer than that
            rep = await self.send(envelope.make("daydream_request", pl, required_response="answer"), timeout=deadline + 10)
        except Exception as e:  # noqa: BLE001
            return {"daydream_id": did, "status": "unavailable", "drafts": [], "reason": f"{type(e).__name__}: {str(e)[:120]}"}
        out = rep.get("payload") or {}
        if rep.get("task_type") != "daydream_draft" or out.get("daydream_id") != did:
            return {"daydream_id": did, "status": "unavailable", "drafts": [],
                    "reason": f"tower answered {rep.get('task_type')}: {str(out.get('reason', ''))[:120]}"}
        drafts = [d if isinstance(d, str) and d.strip() else None for d in (out.get("drafts") or [])][:len(parts)]
        return {"daydream_id": did, "status": out.get("status") if out.get("status") in ("ok", "busy", "unavailable") else "unavailable",
                "drafts": drafts, "model": str(out.get("model", ""))[:120], "ms": out.get("ms"), "tokens": out.get("tokens"),
                **({"reason": str(out["reason"])[:200]} if out.get("reason") else {})}

    def ping(self) -> dict:
        first = time.time() - self.last_ping >= CONV_QUIET_S
        self.last_ping = time.time()
        return {"ok": True, "new_conversation": first}

    async def heartbeat_loop(self) -> None:
        await asyncio.sleep(8)   # let discovery hear a beacon first
        while True:
            was = self.link["ok"]
            await self.heartbeat_once()
            if self.link["ok"]:
                await self.read_and_report()   # catches anything left in the inbox or results not yet delivered
            if self.link["ok"] and not was:
                log.info("linked to tower %s", self.pair and self.pair["peer_name"])
                store.journal(event="linked", tower=self.pair["peer_id"])
            await asyncio.sleep(HEARTBEAT_S)

    async def set_conversation(self, active: bool) -> dict:
        self.conversation_active = active
        store.journal(event="conversation", active=active)
        if not (self.pair and self.tower()):
            return {"sent": False}
        try:
            await self.send(envelope.make("conversation", {"active": active}, priority=5))
            return {"sent": True}
        except Exception as e:
            return {"sent": False, "error": str(e)[:200]}

    def wake(self) -> dict:
        if not (self.pair and self.pair.get("peer_hw_mac")):
            raise HTTPException(409, "Not paired yet (the tower's network address is learned at pairing).")
        pkt = wake_packet(self.pair["peer_hw_mac"])
        addr = discovery.interface_ip(IFACE)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.IPPROTO_IP, discovery.IP_BOUND_IF, socket.if_nametoindex(IFACE))
        for dest in ([addr[1]] if addr else []) + ["255.255.255.255"]:
            s.sendto(pkt, (dest, 9))
        s.close()
        store.journal(event="wake_sent", tower=self.pair["peer_id"])
        return {"sent": True}

    # ---- messages from the tower ----
    async def receive(self, body: dict) -> dict:
        if not self.pair or body.get("from") != self.pair["peer_id"]:
            raise crypto.Rejected("not paired")
        raw = crypto.unseal(self.key(), "POST", "/v1/msg", body)
        env = envelope.Envelope.model_validate(raw)
        if env.source != "dream" or env.destination != "echo":
            raise crypto.Rejected("direction")
        if (why := self.replay.check(env)):
            raise crypto.Rejected(why)
        accepted, reason, extra = True, "stored", {}
        if env.round > envelope.MAX_ROUNDS:
            accepted, reason = False, "too many rounds"
        elif env.task_type == "dream_result":
            accepted, reason, extra = self._take_dream(env)
        elif env.task_type == "memory_request":
            if export.settings().get("enabled"):
                pl = env.payload
                batch = export.build(query=pl.get("query") if isinstance(pl.get("query"), str) else None,
                                     refs=[r for r in pl.get("refs", []) if isinstance(r, str)] if isinstance(pl.get("refs"), list) else None,
                                     limit=pl.get("limit") if isinstance(pl.get("limit"), int) else export.MAX_MEMORIES)
                export.save_outbox(batch, "answer")
                export.mark_sent(batch)
                store.journal(event="received", type=env.task_type, id=env.message_id, accepted=True, reason="answered",
                              batch=batch["batch_id"], memories=len(batch["memories"]))
                reply = envelope.make("memory_batch", export.for_sending(batch), in_reply_to=env.message_id, status="done")
                return crypto.seal(self.key(), self.me, "RESP", "/v1/msg", reply)
            reason = "no batch available (memory export is switched off on the Mac)"
        elif env.task_type == "challenge":
            store.save_inbox(env.model_dump())
            asyncio.get_running_loop().create_task(self.read_and_report())
            reason = "stored for review"
        elif env.task_type == "heartbeat":
            self.link.update(ok=True, last_ok=time.time(), tower_state=env.payload.get("state"))
            reason = "ok"
        store.journal(event="received", type=env.task_type, id=env.message_id, accepted=accepted, reason=reason)
        ack = envelope.make("ack", {"accepted": accepted, "reason": reason, "state": "IDLE", **extra},
                            in_reply_to=env.message_id, status="done" if accepted else "rejected")
        return crypto.seal(self.key(), self.me, "RESP", "/v1/msg", ack)

    def _take_dream(self, env: envelope.Envelope) -> tuple[bool, str, dict]:
        """Section 7b: check the checksum, store durably, THEN give a receipt. Resends get the same receipt."""
        pl = env.payload
        did = pl.get("dream_id")
        if not (isinstance(did, str) and 0 < len(did) <= 64):
            return False, "missing dream_id", {}
        if (old := store.receipts().get(did)):
            return True, "already stored", {"receipt": old}
        if pl.get("sha256") != package_sha256(pl):
            return False, "checksum", {}
        store.save_inbox(env.model_dump())          # fsynced before we answer
        receipt = {"dream_id": did, "sha256": pl["sha256"], "stored_at": envelope.now_iso()}
        store.add_receipt(did, receipt)
        asyncio.get_running_loop().create_task(self.read_and_report())
        return True, "stored for review", {"receipt": receipt}

    async def push_batch(self, by: str = "schedule") -> dict:
        """Send one memory_batch now (only when the export switch is on and the tower is linked)."""
        if not export.settings().get("enabled"):
            raise HTTPException(409, "Memory export is switched off. the owner turns it on with 'all systems go'.")
        if not (self.pair and self.tower()):
            raise HTTPException(503, "The tower isn't on the network right now.")
        batch = export.build(previous_dream_ids=list(store.receipts())[-20:])
        export.save_outbox(batch, "push")
        rep = await self.send(envelope.make("memory_batch", export.for_sending(batch), required_response="ack", priority=2))
        ok = bool(rep.get("payload", {}).get("accepted"))
        if ok:
            export.mark_sent(batch)
        store.journal(event="batch_sent", by=by, batch=batch["batch_id"], memories=len(batch["memories"]), accepted=ok,
                      reason=rep.get("payload", {}).get("reason"))
        return {"accepted": ok, **export.summary(batch), "tower_reason": rep.get("payload", {}).get("reason")}

    async def evening_loop(self) -> None:
        """Protocol section 8: once per evening, when the tower shows up inside the dream window, push a batch."""
        while True:
            now = datetime.now(TZ)
            today = now.strftime("%Y-%m-%d")
            if (DREAM_WINDOW[0] <= now.hour < DREAM_WINDOW[1] and self.pushed_on != today
                    and export.settings().get("enabled") and self.pair and self.tower() and not self.conversation_active):
                try:
                    r = await self.push_batch()
                    self.pushed_on = today
                    log.info("evening batch sent: %s memories, accepted=%s", r["memories"], r["accepted"])
                except Exception as e:
                    log.warning("evening batch failed (will retry): %s", e)
            await asyncio.sleep(60)

    async def wake_loop(self) -> None:
        """At 18:00 (retry 18:05) Chicago time: if paired and the tower isn't on the network, wake it."""
        while True:
            slot = wake_due(datetime.now(TZ), self.wake_slots)
            if slot:
                self.wake_slots.add(slot)
                if self.pair and self.pair.get("peer_hw_mac") and not self.tower():
                    try:
                        self.wake()
                        log.info("sent Wake-on-LAN (%s)", slot)
                    except Exception as e:
                        log.warning("Wake-on-LAN failed: %s", e)
            await asyncio.sleep(30)

    def status(self) -> dict:
        t = self.tower()
        return {
            "device_id": self.me, "name": self.name, "iface": IFACE,
            "lan": discovery.interface_ip(IFACE) and discovery.interface_ip(IFACE)[0],
            "towers_seen": [{k: v for k, v in x.items() if k != "seen"} | {"seconds_ago": round(time.time() - x["seen"])}
                            for x in self.disc.live_towers()],
            "paired": bool(self.pair),
            "tower": self.pair and {"name": self.pair["peer_name"], "device_id": self.pair["peer_id"],
                                    "ip": t and t["ip"], "on_network": bool(t)},
            "link": self.link, "pairing_in_progress": bool(self.pending),
            "dream_folder": str(store.dream_root()),
            "wake_times": [f"{h:02d}:{m:02d} America/Chicago" for h, m in WAKE_TIMES],
            "dreams_received": len(store.receipts()),
            "dreams": reader.counts(),
            "conversation": {"active": self.conversation_active, "last_ping_s_ago": round(time.time() - self.last_ping) if self.last_ping else None},
            "export": {"enabled": bool(export.settings().get("enabled")), "batches_sent": len(export.sent_log()["batches"]),
                       "memories_ever_sent": len(export.sent_log()["memories"]), "evening_batch_sent_on": self.pushed_on},
        }


def lan_app(b: Bridge) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/v1/health")
    def health():
        return {"proto": envelope.PROTO, "role": "mac", "paired": bool(b.pair), "state": "IDLE"}

    @app.post("/v1/msg")
    async def msg(request: Request):
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return JSONResponse({}, 413)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise crypto.Rejected("body")
            return await b.receive(body)
        except crypto.Rejected as e:
            store.journal(event="rejected", reason=str(e), ip=request.client and request.client.host)
            return JSONResponse({}, 401)
        except (ValidationError, ValueError):
            store.journal(event="invalid", ip=request.client and request.client.host)
            return JSONResponse({}, 422)

    return app


def control_app(b: Bridge) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.get("/status")
    def status():
        return b.status()

    @app.post("/pair/start")
    async def pair_start(device_id: str | None = None):
        return await b.pair_start(device_id)

    @app.post("/pair/confirm")
    async def pair_confirm():
        return await b.pair_confirm()

    @app.post("/pair/cancel")
    def pair_cancel():
        b.pending = None
        return {"cancelled": True}

    @app.post("/unpair")
    def unpair():
        return b.unpair()

    @app.post("/wake")
    def wake():
        return b.wake()

    @app.get("/export/preview")
    def export_preview():
        """What the next batch would contain. Nothing is sent; a copy is saved in the outbox for the owner to read."""
        batch = export.build()
        path = export.save_outbox(batch, "preview")
        return {**export.summary(batch), "saved_to": str(path)}

    @app.post("/export/enable")
    def export_enable():
        return export.set_enabled(True)

    @app.post("/export/disable")
    def export_disable():
        return export.set_enabled(False)

    @app.post("/export/send")
    async def export_send():
        return await b.push_batch(by="owner")

    @app.post("/conversation/ping")
    def conversation_ping():
        """Echo core calls this when someone starts talking (core/sched.py turn_started)."""
        return b.ping()

    @app.get("/dreams")
    def dreams():
        return {**reader.counts(), "pending": reader.pending()}

    @app.post("/dreams/{pid}/{decision}")
    def decide(pid: str, decision: str, note: str = ""):
        if decision == "retire":          # the generic route matches first, so retire is handled here
            try:
                return reader.retire(pid)
            except KeyError:
                raise HTTPException(404, "No active insight with that id.")
        if decision not in ("approve", "reject"):
            raise HTTPException(404)
        try:
            return reader.decide(pid, decision == "approve", note=note)
        except KeyError:
            raise HTTPException(404, "No such proposal.")
        except (ValueError, OverflowError) as e:
            raise HTTPException(409, str(e))
        except RuntimeError as e:
            raise HTTPException(502, str(e))

    @app.post("/links/{a}/{b}/remove")
    def unlink(a: int, b: int):
        try:
            return reader.unlink(a, b)
        except RuntimeError as e:
            raise HTTPException(502, str(e))

    @app.post("/conversation")
    async def conversation(active: bool):
        return await b.set_conversation(active)

    @app.post("/daydream/activity")
    async def daydream_activity(request: Request):
        """Echo core: one Daydream stage change (rev e). Never task text. Fire-and-forget: queued, answered at once."""
        return b.daydream_activity(await request.json())

    @app.post("/daydream/request")
    async def daydream_request(request: Request):
        """Echo core: ask the tower's model to draft the parts of a daydream task (rev e). Always answers; on any
        problem status is "unavailable" and Echo carries on without the tower."""
        return await b.daydream_request(await request.json())

    return app


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    addr = discovery.interface_ip(IFACE)
    if not addr:
        log.error("%s has no IPv4 address (is the Ethernet cable in?); retrying via launchd", IFACE)
        await asyncio.sleep(30)
        sys.exit(1)
    ip = addr[0]
    b = Bridge()
    store.dirs()
    log.info("dream bridge %s on %s (%s); data in %s; paired=%s", b.me, IFACE, ip, store.dream_root(), bool(b.pair))
    lan = uvicorn.Server(uvicorn.Config(lan_app(b), host=ip, port=LAN_PORT, log_level="warning", access_log=False))
    ctl = uvicorn.Server(uvicorn.Config(control_app(b), host="127.0.0.1", port=CONTROL_PORT, log_level="warning"))

    async def watch_ip():   # if the wired address changes, exit; launchd restarts us on the new one
        while True:
            await asyncio.sleep(60)
            now = discovery.interface_ip(IFACE)
            if not now or now[0] != ip:
                log.warning("%s address changed (%s -> %s); restarting", IFACE, ip, now and now[0])
                os._exit(3)

    await asyncio.gather(b.disc.run(), b.heartbeat_loop(), b.wake_loop(), b.evening_loop(), b.conversation_loop(), lan.serve(), ctl.serve(), watch_ip())


if __name__ == "__main__":
    asyncio.run(main())
