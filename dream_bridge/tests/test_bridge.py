"""Mac side of the dream bridge against a stand-in tower that follows BRIDGE_PROTOCOL.md."""
import asyncio
import ipaddress
import json
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge import crypto, discovery, envelope, service, store

TOWER_ID = "tower-" + uuid.uuid4().hex[:12]


class FakeTower:
    """Implements the tower's side of sections 3-5, the way the tower session must."""

    def __init__(self, wrong_code=False, click="match", bad_reveal=False):
        self.eph = crypto.Ephemeral()
        self.key = None
        self.mac_id = None
        self.clicked = False
        self.confirm_calls = 0
        self.wrong_code = wrong_code
        self.received = []
        self.click = click
        self.bad_reveal = bad_reveal
        self.start_body = None
        self.order = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        self.order.append(req.url.path)
        if req.url.path == "/v1/pair/start":
            if "nonce" in body or "commit" not in body:
                return httpx.Response(422, json={})          # rev c: nonce must not be sent yet
            self.start_body = body
            self.mac_id = body["device_id"]
            return httpx.Response(200, json={"device_id": TOWER_ID, "name": "TOWER", "pub": crypto.b64e(self.eph.pub),
                                             "nonce": crypto.b64e(self.eph.nonce), "hw_mac": "aa:bb:cc:dd:ee:ff"})
        if req.url.path == "/v1/pair/reveal":
            if self.start_body is None:
                return httpx.Response(409, json={})
            mpub, mnonce = crypto.b64d(self.start_body["pub"]), crypto.b64d(body["nonce"])
            if self.bad_reveal or crypto.commitment(mpub, mnonce) != crypto.b64d(self.start_body["commit"]):
                return httpx.Response(401, json={})
            self.key = crypto.derive_key(self.eph.shared(mpub), mnonce, self.eph.nonce)
            if self.wrong_code:   # a man-in-the-middle: different key on this side
                self.key = b"\x00" * 32
            self.code = crypto.pair_code(mpub, self.eph.pub, mnonce, self.eph.nonce)
            return httpx.Response(200, json={"status": "revealed"})
        if req.url.path == "/v1/pair/confirm":
            self.confirm_calls += 1
            if not crypto.proof_ok(self.key, "mac-confirm", body["proof"]):
                return httpx.Response(403, json={})
            if self.confirm_calls == 1:
                return httpx.Response(200, json={"status": "waiting"})   # the owner hasn't clicked Match yet
            if self.click == "reject":
                return httpx.Response(200, json={"status": "rejected"})
            return httpx.Response(200, json={"proof": crypto.proof(self.key, "tower-confirm")})
        if req.url.path == "/v1/msg":
            env = crypto.unseal(self.key, "POST", "/v1/msg", body)
            assert body["from"] == self.mac_id
            envelope.Envelope.model_validate(env)
            self.received.append(env)
            ack = {**envelope.make("ack", {"accepted": True, "state": "SLEEP"}, in_reply_to=env["message_id"]),
                   "source": "dream", "destination": "echo"}
            return httpx.Response(200, json=crypto.seal(self.key, TOWER_ID, "RESP", "/v1/msg", ack))
        return httpx.Response(404)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("DREAM_BRIDGE_DATA", str(tmp_path / "dream"))
    monkeypatch.setattr(service.asyncio, "sleep", _nosleep)
    return tmp_path


async def _nosleep(_s):
    return None


def bridge_with(tower: FakeTower, pairing_open=True) -> service.Bridge:
    b = service.Bridge(transport=httpx.MockTransport(tower.handler))
    b.disc.towers[TOWER_ID] = {"device_id": TOWER_ID, "name": "TOWER", "ip": "192.168.1.50", "port": 47801,
                               "proto": 1, "pairing_open": pairing_open, "paired_with": None, "seen": time.time()}
    return b


# ---- crypto ----
def test_both_sides_get_same_code_and_key():
    m, t = crypto.Ephemeral(), crypto.Ephemeral()
    assert crypto.derive_key(m.shared(t.pub), m.nonce, t.nonce) == crypto.derive_key(t.shared(m.pub), m.nonce, t.nonce)
    c = crypto.pair_code(m.pub, t.pub, m.nonce, t.nonce)
    assert len(c) == 4 and c.isdigit()


def test_seal_rejects_wrong_key_path_or_tampering():
    k = b"k" * 32
    box = crypto.seal(k, "mac", "POST", "/v1/msg", {"x": 1})
    assert crypto.unseal(k, "POST", "/v1/msg", box) == {"x": 1}
    with pytest.raises(crypto.Rejected):
        crypto.unseal(b"j" * 32, "POST", "/v1/msg", box)
    with pytest.raises(crypto.Rejected):
        crypto.unseal(k, "POST", "/v1/other", box)
    with pytest.raises(crypto.Rejected):
        crypto.unseal(k, "POST", "/v1/msg", {**box, "from": "someone-else"})


# ---- discovery ----
def test_beacon_parsing():
    lan = ipaddress.IPv4Network("192.168.1.0/24")
    good = json.dumps({"svc": "echo-dream", "proto": 1, "role": "tower", "device_id": TOWER_ID, "name": "PC",
                       "port": 47801, "paired_with": None, "pairing_open": True}).encode()
    t = discovery.parse_beacon(good, "192.168.1.50", "me", lan)
    assert t and t["pairing_open"] and t["ip"] == "192.168.1.50"
    assert discovery.parse_beacon(good, "10.0.0.5", "me", lan) is None          # not our LAN
    assert discovery.parse_beacon(good, "192.168.1.50", TOWER_ID, lan) is None  # our own beacon
    assert discovery.parse_beacon(b"not json", "192.168.1.50", "me", lan) is None
    assert discovery.parse_beacon(good.replace(b'"tower"', b'"mac"'), "192.168.1.50", "me", lan) is None
    assert discovery.parse_beacon(b"x" * 3000, "192.168.1.50", "me", lan) is None


def test_wake_packet():
    p = service.wake_packet("aa:bb:cc:dd:ee:ff")
    assert len(p) == 102 and p[:6] == b"\xff" * 6 and p[6:12] == bytes.fromhex("aabbccddeeff")


# ---- pairing + link ----
async def test_pair_then_heartbeat(env):
    tower = FakeTower()
    b = bridge_with(tower)
    s = await b.pair_start()
    assert s["code"] == tower.code             # same 4 digits on both screens
    r = await b.pair_confirm()
    assert r["paired"] and tower.confirm_calls == 2
    saved = store.load_pair()
    assert saved["peer_id"] == TOWER_ID and saved["peer_hw_mac"] == "aa:bb:cc:dd:ee:ff"
    assert (store.STATE_DIR / "pair.json").stat().st_mode & 0o777 == 0o600
    await b.heartbeat_once()
    assert b.link["ok"] and b.link["tower_state"] == "SLEEP"
    assert tower.received[-1]["task_type"] == "heartbeat"


async def test_mismatched_keys_never_pair(env):
    b = bridge_with(FakeTower(wrong_code=True))
    await b.pair_start()
    with pytest.raises(service.HTTPException):
        await b.pair_confirm()
    assert store.load_pair() is None


async def test_no_tower_in_pairing_mode(env):
    b = bridge_with(FakeTower(), pairing_open=False)
    with pytest.raises(service.HTTPException) as e:
        await b.pair_start()
    assert e.value.status_code == 404


async def test_tower_down_echo_unaffected(env):
    b = bridge_with(FakeTower())
    await b.pair_start(); await b.pair_confirm()
    b.disc.towers.clear()                      # tower switched off
    await b.heartbeat_once()
    assert not b.link["ok"]
    assert (await b.set_conversation(True)) == {"sent": False}


# ---- messages from the tower ----
async def test_receive_dream_result_replay_and_strangers(env):
    tower = FakeTower()
    b = bridge_with(tower)
    await b.pair_start(); await b.pair_confirm()
    client = TestClient(service.lan_app(b))
    pl = {"dream_id": "d1", "proposals": []}
    pl["sha256"] = service.package_sha256(pl)
    msg = {**envelope.make("dream_result", pl), "source": "dream", "destination": "echo"}
    box = crypto.seal(tower.key, TOWER_ID, "POST", "/v1/msg", msg)
    r = client.post("/v1/msg", json=box)
    assert r.status_code == 200
    ack = crypto.unseal(tower.key, "RESP", "/v1/msg", r.json())
    assert ack["task_type"] == "ack" and ack["payload"]["accepted"]
    assert len(list((env / "dream" / "inbox").rglob("*.json"))) == 1       # in inbox/ or already read into inbox/read/
    assert client.post("/v1/msg", json=box).status_code == 401                    # replay
    stranger = crypto.seal(b"s" * 32, TOWER_ID, "POST", "/v1/msg", msg)
    assert client.post("/v1/msg", json=stranger).status_code == 401               # wrong key
    bad = crypto.seal(tower.key, TOWER_ID, "POST", "/v1/msg", {**msg, "message_id": "x" * 9, "task_type": "rm -rf"})
    assert client.post("/v1/msg", json=bad).status_code == 422                     # not a valid envelope
    journal = (env / "dream" / "journal.jsonl").read_text()
    assert "proposals" not in journal                                              # journal holds no content


def test_control_api_is_local_only(env):
    b = bridge_with(FakeTower())
    c = TestClient(service.control_app(b))
    assert c.get("/status").status_code == 200
    assert c.get("/status", headers={"host": "192.168.1.25:47803"}).status_code == 400


# ---- dream package receipt (protocol 7b) ----
def _dream(tower, did="dream-1", tamper=False):
    pl = {"dream_id": did, "narrative": "a hypothesis", "proposals": [{"proposal_id": "p1", "kind": "question"}]}
    pl["sha256"] = service.package_sha256(pl)
    if tamper:
        pl["narrative"] = "changed in transit"
    msg = {**envelope.make("dream_result", pl), "source": "dream", "destination": "echo"}
    return crypto.seal(tower.key, TOWER_ID, "POST", "/v1/msg", msg)


async def test_receipt_only_after_stored_and_resend_is_deduplicated(env):
    tower = FakeTower()
    b = bridge_with(tower)
    await b.pair_start(); await b.pair_confirm()
    c = TestClient(service.lan_app(b))
    ack = crypto.unseal(tower.key, "RESP", "/v1/msg", c.post("/v1/msg", json=_dream(tower)).json())
    rc = ack["payload"]["receipt"]
    assert ack["payload"]["accepted"] and rc["dream_id"] == "dream-1" and len(rc["sha256"]) == 64
    files = list((env / "dream" / "inbox").rglob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["payload"]["dream_id"] == "dream-1"
    again = crypto.unseal(tower.key, "RESP", "/v1/msg", c.post("/v1/msg", json=_dream(tower)).json())   # tower resends
    assert again["payload"]["receipt"] == rc and again["payload"]["reason"] == "already stored"
    assert len(list((env / "dream" / "inbox").rglob("*.json"))) == 1       # in inbox/ or already read into inbox/read/


async def test_bad_checksum_gets_no_receipt(env):
    tower = FakeTower()
    b = bridge_with(tower)
    await b.pair_start(); await b.pair_confirm()
    c = TestClient(service.lan_app(b))
    ack = crypto.unseal(tower.key, "RESP", "/v1/msg", c.post("/v1/msg", json=_dream(tower, tamper=True)).json())
    assert not ack["payload"]["accepted"] and ack["payload"]["reason"] == "checksum" and "receipt" not in ack["payload"]
    assert not list((env / "dream" / "inbox").glob("*.json"))


def test_checksum_matches_reference():
    # Fixed vector so the tower can check its own implementation gives the same hex.
    pl = {"dream_id": "abc", "proposals": [], "narrative": "café", "sha256": "ignored"}
    assert service.package_sha256(pl) == __import__("hashlib").sha256(
        '{"dream_id":"abc","narrative":"café","proposals":[]}'.encode()).hexdigest()


# ---- 18:00 wake-up (protocol 8) ----
def test_wake_schedule():
    from datetime import datetime
    tz = service.TZ
    sent = set()
    assert service.wake_due(datetime(2026, 11, 3, 17, 59, tzinfo=tz), sent) is None
    s1 = service.wake_due(datetime(2026, 11, 3, 18, 0, 20, tzinfo=tz), sent)
    assert s1 == "2026-11-03 18:00"
    sent.add(s1)
    assert service.wake_due(datetime(2026, 11, 3, 18, 2, tzinfo=tz), sent) is None     # not twice
    assert service.wake_due(datetime(2026, 11, 3, 18, 5, 10, tzinfo=tz), sent) == "2026-11-03 18:05"
    assert service.wake_due(datetime(2026, 11, 3, 18, 30, tzinfo=tz), set()) is None   # too late: don't wake mid-evening
    assert service.wake_due(datetime(2026, 11, 4, 18, 1, tzinfo=tz), sent) == "2026-11-04 18:00"  # next day


async def test_no_wake_when_tower_already_on(env, monkeypatch):
    from datetime import datetime
    b = bridge_with(FakeTower())
    await b.pair_start(); await b.pair_confirm()
    calls = []
    monkeypatch.setattr(b, "wake", lambda: calls.append(1))
    monkeypatch.setattr(service, "datetime", type("D", (), {"now": staticmethod(lambda tz: datetime(2026, 11, 3, 18, 0, 5, tzinfo=tz))}))
    async def stop(_):
        raise asyncio.CancelledError
    monkeypatch.setattr(service.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await b.wake_loop()
    assert calls == []                       # tower's beacon is live: no wake packet
    b.disc.towers.clear()
    b.wake_slots.clear()
    with pytest.raises(asyncio.CancelledError):
        await b.wake_loop()
    assert calls == [1]                      # tower off: wake it


# ---- rev c: commit-then-reveal ----
async def test_commit_before_reveal(env):
    tower = FakeTower()
    b = bridge_with(tower)
    s = await b.pair_start()
    assert tower.order == ["/v1/pair/start", "/v1/pair/reveal"]
    assert "nonce" not in tower.start_body and s["code"] == tower.code


async def test_doesnt_match_on_tower_saves_nothing(env):
    b = bridge_with(FakeTower(click="reject"))
    await b.pair_start()
    with pytest.raises(service.HTTPException) as e:
        await b.pair_confirm()
    assert "Doesn't match" in e.value.detail and store.load_pair() is None


async def test_bad_commitment_refused(env):
    b = bridge_with(FakeTower(bad_reveal=True))
    with pytest.raises(service.HTTPException) as e:
        await b.pair_start()
    assert "commitment" in e.value.detail and b.pending is None


def test_commitment_reference():
    # matches the tower's formula: SHA256(b"echo-dream-commit-v1" + mac_pub + mac_nonce)
    pub, nonce = bytes(range(32)), bytes(range(16))
    assert crypto.commitment(pub, nonce) == __import__("hashlib").sha256(b"echo-dream-commit-v1" + pub + nonce).digest()
