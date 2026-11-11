"""Echo Link pairing and sign-in, with a simulated phone (EC P-256 key like Android Keystore makes)."""
from __future__ import annotations

import json
import secrets

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

import echolink.link as L
from echolink.app import admin_app, public_app

ADMIN = {"x-echo-admin": "1"}


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


class Phone:
    def __init__(self, name="Pixel 9a"):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.name = name
        self.pub = L.b64e(self.key.public_key().public_bytes(serialization.Encoding.DER,
                                                             serialization.PublicFormat.SubjectPublicKeyInfo))
        self.nonce = L.b64e(secrets.token_bytes(16))

    def sign(self, msg: bytes) -> str:
        return L.b64e(self.key.sign(msg, ec.ECDSA(hashes.SHA256())))

    def start(self, method="qr", token=""):
        return {"method": method, "token": token, "device_name": self.name, "pubkey": self.pub, "nonce": self.nonce,
                "sig": self.sign(L.pair_message(method, self.nonce, self.name))}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "tailnet", lambda: {"name": "mac.example.ts.net", "ip": "100.1.2.3"})
    monkeypatch.setattr(L, "lan_addresses", lambda: ["192.168.1.50"])
    clock = Clock()
    link = L.Link(data=tmp_path / "el", now=clock)
    return link, clock, TestClient(public_app(link)), TestClient(admin_app(link))


def make_qr(link):
    return json.loads(link.new_qr(lan=["192.168.1.50"], ts={"name": "mac.example.ts.net", "ip": "100.1.2.3"})["payload"])


def link_phone(link, pub, phone, user="owner"):
    q = make_qr(link)
    r = pub.post("/v1/pair/start", json=phone.start("qr", q["k"]))
    assert r.status_code == 200, r.text
    pid = r.json()["pair_id"]
    assert link.decide(pid, user)["status"] == "approved"
    st = pub.get(f"/v1/pair/{pid}").json()
    return st["device_id"]


def sign_in(pub, phone, device_id):
    c = pub.post("/v1/auth/challenge", json={"device_id": device_id}).json()["challenge"]
    return pub.post("/v1/auth/session", json={"device_id": device_id, "challenge": c,
                                              "sig": phone.sign(L.auth_message(device_id, c))})


def test_qr_holds_no_permanent_secret_and_pins_certificate(env):
    link, _, _, _ = env
    q = make_qr(link)
    assert q["t"] == "echolink" and q["p"] == 8771 and q["h"] == ["192.168.1.50"] and q["ts"] == "mac.example.ts.net"
    assert L.b64d(q["fp"]) == link.fp and len(link.fp) == 32
    stored = (link.data / "qr.json").read_text()
    assert q["k"] not in stored                       # only the hash is kept
    assert "BEGIN" not in json.dumps(q)               # no key material


def test_qr_pairing_end_to_end(env):
    link, _, pub, admin = env
    phone = Phone()
    q = make_qr(link)
    r = pub.post("/v1/pair/start", json=phone.start("qr", q["k"]))
    assert r.status_code == 200
    pid = r.json()["pair_id"]
    assert pub.get(f"/v1/pair/{pid}").json() == {"status": "pending"}
    pend = admin.get("/admin/state").json()["pending"]
    assert len(pend) == 1 and pend[0]["device"] == "Pixel 9a" and len(pend[0]["code"]) == 6
    assert admin.post(f"/admin/pair/{pid}", json={"decision": "loved_one"}, headers=ADMIN).json()["user"] == "loved_one"
    st = pub.get(f"/v1/pair/{pid}").json()
    assert st["status"] == "approved" and st["user"] == "loved_one" and st["port"] == 8771
    s = sign_in(pub, phone, st["device_id"])
    assert s.status_code == 200 and s.json()["user"] == "loved_one"
    me = pub.get("/v1/me", headers={"Authorization": f"Bearer {s.json()['session']}"}).json()
    assert me["user"] == "loved_one" and me["device"] == "Pixel 9a"
    events = [h["event"] for h in link.history()]
    assert events[:3] == ["paired", "pair_requested", "qr_shown"]


def test_qr_is_one_use_and_expires(env):
    link, clock, pub, _ = env
    q = make_qr(link)
    assert pub.post("/v1/pair/start", json=Phone().start("qr", q["k"])).status_code == 200
    assert pub.post("/v1/pair/start", json=Phone().start("qr", q["k"])).status_code == 403    # used
    q = make_qr(link)
    clock.t += L.QR_TTL + 1
    assert pub.post("/v1/pair/start", json=Phone().start("qr", q["k"])).status_code == 403    # expired
    assert pub.post("/v1/pair/start", json=Phone().start("qr", "guess")).status_code == 403


def test_new_qr_replaces_old(env):
    link, _, pub, _ = env
    old = make_qr(link)
    make_qr(link)
    assert pub.post("/v1/pair/start", json=Phone().start("qr", old["k"])).status_code == 403


def test_signature_must_match_key(env):
    link, _, pub, _ = env
    a, b = Phone(), Phone()
    body = a.start("qr", make_qr(link)["k"])
    body["pubkey"] = b.pub                                    # someone else's key
    assert pub.post("/v1/pair/start", json=body).status_code == 401
    bad = a.start("discover")
    bad["pubkey"] = L.b64e(b"not a key")
    assert pub.post("/v1/pair/start", json=bad).status_code == 400


def test_discovery_code_matches_on_both_sides_and_detects_impostor(env):
    link, _, pub, _ = env
    phone = Phone()
    r = pub.post("/v1/pair/start", json=phone.start("discover")).json()
    mac_code = link.pending_list()[0]["code"]
    phone_code = L.pair_code(link.fp, L.b64d(phone.nonce), L.b64d(r["mac_nonce"]))   # phone uses the cert it saw
    assert phone_code == mac_code
    impostor_fp = bytes(32)                                   # a man in the middle shows a different certificate
    assert L.pair_code(impostor_fp, L.b64d(phone.nonce), L.b64d(r["mac_nonce"])) != mac_code


def test_deny_and_expiry(env):
    link, clock, pub, _ = env
    pid = pub.post("/v1/pair/start", json=Phone().start("discover")).json()["pair_id"]
    link.decide(pid, "deny")
    assert pub.get(f"/v1/pair/{pid}").json()["status"] == "denied"
    with pytest.raises(L.LinkError):
        link.decide(pid, "owner")                             # can't approve after denying
    pid = pub.post("/v1/pair/start", json=Phone().start("discover")).json()["pair_id"]
    clock.t += L.PAIR_TTL + 1
    assert pub.get(f"/v1/pair/{pid}").json()["status"] == "expired"
    with pytest.raises(L.LinkError):
        link.decide(pid, "owner")
    assert link.devices() == []


def test_phone_cannot_choose_user_or_admin(env):
    link, _, pub, _ = env
    body = Phone().start("discover")
    body["user"] = "owner"
    body["admin"] = True
    pid = pub.post("/v1/pair/start", json=body).json()["pair_id"]
    assert pub.get(f"/v1/pair/{pid}").json()["status"] == "pending"     # still waits for the Mac
    for path in ("/admin/state", "/admin/qr", f"/admin/pair/{pid}"):
        assert pub.get(path).status_code in (404, 405) and pub.post(path, json={}).status_code in (404, 405)


def test_challenge_one_use_and_bound_to_device(env):
    link, clock, pub, _ = env
    a, b = Phone("A"), Phone("B")
    da, db = link_phone(link, pub, a), link_phone(link, pub, b, "loved_one")
    c = pub.post("/v1/auth/challenge", json={"device_id": da}).json()["challenge"]
    body = {"device_id": da, "challenge": c, "sig": a.sign(L.auth_message(da, c))}
    assert pub.post("/v1/auth/session", json=body).status_code == 200
    assert pub.post("/v1/auth/session", json=body).status_code == 401           # replay
    c = pub.post("/v1/auth/challenge", json={"device_id": da}).json()["challenge"]
    assert pub.post("/v1/auth/session", json={"device_id": db, "challenge": c,
                                              "sig": b.sign(L.auth_message(db, c))}).status_code == 401   # A's challenge
    c = pub.post("/v1/auth/challenge", json={"device_id": db}).json()["challenge"]
    assert pub.post("/v1/auth/session", json={"device_id": db, "challenge": c,
                                              "sig": a.sign(L.auth_message(db, c))}).status_code == 401   # wrong key
    c = pub.post("/v1/auth/challenge", json={"device_id": da}).json()["challenge"]
    clock.t += L.CHALLENGE_TTL + 1
    assert pub.post("/v1/auth/session", json={"device_id": da, "challenge": c,
                                              "sig": a.sign(L.auth_message(da, c))}).status_code == 401   # expired
    assert pub.post("/v1/auth/challenge", json={"device_id": "nope"}).status_code == 401


def test_revoke_cuts_off_at_once(env):
    link, _, pub, admin = env
    phone = Phone()
    did = link_phone(link, pub, phone)
    tok = sign_in(pub, phone, did).json()["session"]
    auth = {"Authorization": f"Bearer {tok}"}
    assert pub.get("/v1/ping", headers=auth).status_code == 200
    assert admin.post(f"/admin/devices/{did}/revoke", headers=ADMIN).status_code == 200
    assert pub.get("/v1/ping", headers=auth).status_code == 401
    assert pub.post("/v1/auth/challenge", json={"device_id": did}).status_code == 401   # can't sign in again
    assert pub.get("/v1/ping", headers={"Authorization": "Bearer made-up"}).status_code == 401
    assert pub.get("/v1/ping").status_code == 401


def test_session_expires(env):
    link, clock, pub, _ = env
    phone = Phone()
    did = link_phone(link, pub, phone)
    tok = sign_in(pub, phone, did).json()["session"]
    clock.t += L.SESSION_TTL + 1
    assert pub.get("/v1/ping", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_relink_after_revoke_gets_new_identity(env):
    link, _, pub, _ = env
    phone = Phone()
    d1 = link_phone(link, pub, phone)
    link.revoke(d1)
    d2 = link_phone(link, pub, phone, "loved_one")
    assert d1 != d2 and sign_in(pub, phone, d2).json()["user"] == "loved_one"


def test_rate_limits(env):
    link, clock, pub, _ = env
    codes = [pub.post("/v1/pair/start", json=Phone().start("discover")).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429                    # 3 waiting at most
    clock.t += L.PAIR_TTL * 3
    codes = [pub.post("/v1/pair/start", json=Phone().start("discover")).status_code for _ in range(10)]
    assert 429 in codes                                                          # 10 attempts per 10 minutes


def test_admin_page_guards(env):
    link, _, _, admin = env
    assert admin.get("/").status_code == 200
    assert admin.post("/admin/qr").status_code == 403                           # no header: cross-site form post
    assert admin.get("/admin/state", headers={"host": "evil.example:8772"}).status_code == 403   # DNS rebinding
    assert admin.post("/admin/qr", headers=ADMIN).status_code == 200


def test_files_private_and_history_has_no_secrets(env):
    link, _, pub, _ = env
    phone = Phone()
    did = link_phone(link, pub, phone)
    tok = sign_in(pub, phone, did).json()["session"]
    for p in link.data.iterdir():
        assert p.stat().st_mode & 0o077 == 0, p.name
    hist = (link.data / "history.jsonl").read_text()
    assert tok not in hist and phone.pub not in hist and "token" not in hist
    assert tok not in (link.data / "devices.json").read_text()


def test_identity_and_certificate_survive_restart(env, tmp_path):
    link, clock, pub, _ = env
    phone = Phone()
    did = link_phone(link, pub, phone)
    again = L.Link(data=link.data, now=clock)
    assert again.fp == link.fp and again.identity == link.identity
    assert sign_in(TestClient(public_app(again)), phone, did).status_code == 200   # still linked after a restart


def test_big_body_refused(env):
    _, _, pub, _ = env
    assert pub.post("/v1/pair/start", content=b"x" * (70 * 1024),
                    headers={"content-type": "application/json"}).status_code == 413


# --- conversation through Echo Link --------------------------------------------------------------------------------
import asyncio  # noqa: E402

from core.config import PersonaConfig  # noqa: E402
from core.llm import EchoBackend  # noqa: E402
from core.server import create_app  # noqa: E402
from echolink.app import from_phone, to_phone  # noqa: E402
from memory.store import MemoryStore  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402


class FakeCore:
    """Stands in for Core's /ws: records what Echo Link sends and the address it used."""

    def __init__(self, replies):
        self.url, self.sent, self.replies = None, [], list(replies)
        self.q = asyncio.Queue()

    def __call__(self, url, **kw):
        self.url = url
        return self

    async def __aenter__(self):
        for r in self.replies:
            self.q.put_nowait(r)
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, m):
        self.sent.append(json.loads(m))
        self.q.put_nowait(json.dumps({"type": "reply", "text": "echo:" + json.loads(m)["text"]}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.q.get()


def test_phone_message_filter():
    assert json.loads(from_phone('{"text":"hi","speaker":"owner","how":"voice"}')) == {"text": "hi", "how": "voice"}
    assert from_phone('{"type":"feedback","text":"x"}') is None          # window controls aren't passed on
    assert from_phone('{"text":"   "}') is None and from_phone("not json") is None and from_phone("[1]") is None
    assert to_phone('{"type":"history","turns":[["secret"]]}') is None
    assert to_phone('{"type":"stand_down"}') is None
    assert to_phone('{"type":"delta","text":"a"}') == '{"type":"delta","text":"a"}'


def test_conversation_needs_a_signed_in_phone_and_uses_the_macs_choice_of_person(env):
    link, _, _, _ = env
    core = FakeCore([json.dumps({"type": "history", "turns": [["user", "the owner's private talk"]]}),
                     json.dumps({"type": "state", "state": "idle"})])
    pub = TestClient(public_app(link, connect=core))
    with pytest.raises(WebSocketDisconnect):
        with pub.websocket_connect("/v1/ws") as ws:
            ws.receive_text()
    phone = Phone()
    did = link_phone(link, pub, phone, "loved_one")
    tok = sign_in(pub, phone, did).json()["session"]
    with pub.websocket_connect("/v1/ws", headers={"Authorization": f"Bearer {tok}"}) as ws:
        assert json.loads(ws.receive_text()) == {"type": "state", "state": "idle"}     # history never reaches the phone
        ws.send_text(json.dumps({"text": "Hello Echo", "speaker": "owner"}))
        assert json.loads(ws.receive_text())["text"] == "echo:Hello Echo"
    assert core.url.endswith("?client=phone&user=loved_one")
    assert core.sent == [{"text": "Hello Echo"}]                                      # phone's "speaker" dropped


def test_core_phone_client_keeps_window_and_fixes_person(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHO_EDIM", "off")
    store = MemoryStore(tmp_path / "m.sqlite")
    store.add_turn("user", "something the owner said earlier", speaker="owner")
    c = TestClient(create_app(PersonaConfig(), EchoBackend(), store))
    with c.websocket_connect("/ws") as window:
        assert window.receive_json()["type"] == "history"
        with c.websocket_connect("/ws?client=phone&user=loved_one") as phone:
            h = phone.receive_json()
            assert h["type"] == "history" and h["turns"] == []                       # no shared history on a phone
            phone.send_json({"text": "Hi, it's me.", "speaker": "owner"})
            got = []
            while not ((m := phone.receive_json())["type"] == "state" and m["state"] == "idle"):
                got.append(m["type"])
            assert "reply" in got
        window.send_json({"text": "Are you still there?"})                          # window was NOT told to stand down
        types = []
        while not ((m := window.receive_json())["type"] == "state" and m["state"] == "idle"):
            types.append(m["type"])
        assert "stand_down" not in types and "reply" in types
    rows = store.db.execute("SELECT speaker FROM turns WHERE role='user' ORDER BY id").fetchall()
    assert [r[0] for r in rows][:2] == ["owner", "loved_one"]                           # the linked person, not the phone's claim
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws?client=phone&user=mallory") as bad:
            bad.receive_json()
