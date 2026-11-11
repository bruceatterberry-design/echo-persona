"""Echo Link core: the Mac's identity, linked phones, pairing and sign-in. No web code here (see app.py).

Design: docs/android/PHASE2_LINK_DESIGN.md. Protocol (what the Android app must do) in docs/android/ECHOLINK_API.md.
- The Mac has its own TLS certificate. Phones PIN its SHA-256 fingerprint (from the QR code, or after the 6-digit
  number matched on both screens), so a fake "Echo" on the network is rejected. The same pin works at home and over
  Tailscale.
- Each phone makes an EC P-256 key pair inside Android Keystore. The Mac keeps only the public key; every sign-in signs
  a fresh one-use challenge (nothing reusable to steal or replay).
- Only the Mac can approve a phone and choose its person (owner / loved_one); a phone can't change its own person.
- Nothing secret is logged; pairing history has names, people, methods and times only.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import json
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "echolink"
PORT = 8771
ADMIN_PORT = 8772
USERS = ("owner", "loved_one")
QR_TTL = 300            # QR code: 5 minutes, one use
PAIR_TTL = 300          # a pairing waiting for the Mac's answer
CHALLENGE_TTL = 60
SESSION_TTL = 12 * 3600
MAX_PENDING = 3
STARTS_PER_10MIN = 10
TS_SOCKET = ROOT / "data" / "tailscale" / "tailscaled.sock"


class LinkError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64d(s: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError) as e:
        raise LinkError(400, "bad base64") from e


def _read(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.chmod(0o600)
    tmp.replace(p)


def pair_code(fp: bytes, phone_nonce: bytes, mac_nonce: bytes) -> str:
    """The 6-digit number both screens show. The phone uses the certificate fingerprint IT saw, so a man in the middle
    (different certificate, committed before the nonces exist) gives a different number."""
    h = hashlib.sha256(b"echolink-code-v1" + fp + phone_nonce + mac_nonce).digest()
    return f"{int.from_bytes(h[:4], 'big') % 1_000_000:06d}"


def pair_message(method: str, nonce: str, device_name: str) -> bytes:
    return f"echolink-pair-v1|{method}|{nonce}|{device_name}".encode()


def auth_message(device_id: str, challenge: str) -> bytes:
    return f"echolink-auth-v1|{device_id}|{challenge}".encode()


def load_pubkey(b64: str) -> ec.EllipticCurvePublicKey:
    try:
        k = serialization.load_der_public_key(b64d(b64))
    except ValueError as e:
        raise LinkError(400, "bad public key") from e
    if not isinstance(k, ec.EllipticCurvePublicKey) or k.curve.name != "secp256r1":
        raise LinkError(400, "public key must be EC P-256")
    return k


def verify(pub: ec.EllipticCurvePublicKey, sig_b64: str, msg: bytes) -> bool:
    try:
        pub.verify(b64d(sig_b64), msg, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, LinkError, ValueError):
        return False


def lan_addresses() -> list[str]:
    """This Mac's private home-network addresses (not loopback, not Tailscale's 100.64/10)."""
    out = []
    try:
        txt = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "inet":
            try:
                ip = ipaddress.ip_address(parts[1])
            except ValueError:
                continue
            if ip.is_private and not ip.is_loopback and ip not in ipaddress.ip_network("100.64.0.0/10") \
                    and not ip.is_link_local:
                out.append(str(ip))
    return out


def tailnet() -> dict:
    """{'name': 'echo-mac.tailXXXX.ts.net', 'ip': '100.x'} or {} when Tailscale isn't answering."""
    try:
        r = subprocess.run(["/opt/homebrew/bin/tailscale", f"--socket={TS_SOCKET}", "status", "--json"],
                           capture_output=True, text=True, timeout=5)
        s = json.loads(r.stdout)["Self"]
        ips = [i for i in s.get("TailscaleIPs", []) if "." in i]
        return {"name": s.get("DNSName", "").rstrip("."), "ip": ips[0] if ips else ""}
    except Exception:  # noqa: BLE001
        return {}


@dataclass
class Pending:
    pair_id: str
    method: str
    device_name: str
    pubkey: str
    phone_nonce: bytes
    mac_nonce: bytes
    code: str
    created: float
    addr: str
    status: str = "pending"          # pending | approved | denied | expired
    user: str = ""
    device_id: str = ""


@dataclass
class Link:
    data: Path = DATA
    now: callable = time.time
    pending: dict = field(default_factory=dict)
    challenges: dict = field(default_factory=dict)     # challenge -> (device_id, expires)
    sessions: dict = field(default_factory=dict)       # sha256(token) -> {device_id, user, expires}
    starts: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        self.data.mkdir(parents=True, exist_ok=True)
        self.data.chmod(0o700)
        ident = _read(self.data / "identity.json", None)
        if not ident:
            ident = {"mac_id": secrets.token_hex(6), "name": f"Echo on {socket.gethostname().split('.')[0]}",
                     "created": time.strftime("%Y-%m-%d %H:%M")}
            _write(self.data / "identity.json", ident)
        self.identity = ident
        self.cert_path, self.key_path = self.data / "cert.pem", self.data / "key.pem"
        if not (self.cert_path.exists() and self.key_path.exists()):
            self._make_cert()
        cert = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
        self.fp = cert.fingerprint(hashes.SHA256())

    # --- certificate ---------------------------------------------------------------------------------------------
    def _make_cert(self) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Echo Link {self.identity['mac_id']}")])
        host = socket.gethostname().split(".")[0]
        sans = [x509.DNSName(f"{host}.local"), x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
        start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(start)
                .not_valid_after(start + dt.timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName(sans), critical=False)
                .sign(key, hashes.SHA256()))
        self.key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
        self.key_path.chmod(0o600)
        self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        self.cert_path.chmod(0o600)

    # --- records -------------------------------------------------------------------------------------------------
    def devices(self) -> list[dict]:
        return _read(self.data / "devices.json", [])

    def _save_devices(self, ds: list[dict]) -> None:
        _write(self.data / "devices.json", ds)

    def history(self, n: int = 50) -> list[dict]:
        try:
            lines = (self.data / "history.jsonl").read_text().splitlines()[-n:]
        except OSError:
            return []
        return [json.loads(x) for x in reversed(lines) if x.strip()]

    def _event(self, what: str, **kw) -> None:
        rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.now())), "event": what, **kw}
        p = self.data / "history.jsonl"
        with p.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        p.chmod(0o600)

    def hello(self) -> dict:
        return {"service": "echolink", "v": 1, "name": self.identity["name"], "mac_id": self.identity["mac_id"]}

    # --- QR ------------------------------------------------------------------------------------------------------
    def new_qr(self, lan: list[str] | None = None, ts: dict | None = None) -> dict:
        """A one-use code valid 5 minutes plus how to reach and recognise this Mac. No permanent secret."""
        token = secrets.token_urlsafe(18)
        _write(self.data / "qr.json", {"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                                       "expires": self.now() + QR_TTL})
        ts = tailnet() if ts is None else ts
        payload = {"t": "echolink", "v": 1, "id": self.identity["mac_id"], "n": self.identity["name"],
                   "h": lan_addresses() if lan is None else lan, "p": PORT, "ts": ts.get("name", ""),
                   "ti": ts.get("ip", ""), "fp": b64e(self.fp), "k": token}
        self._event("qr_shown")
        return {"payload": json.dumps(payload, separators=(",", ":")), "expires": self.now() + QR_TTL}

    def _use_qr(self, token: str) -> bool:
        q = _read(self.data / "qr.json", {})
        ok = bool(q) and q.get("expires", 0) > self.now() and secrets.compare_digest(
            q.get("token_sha256", ""), hashlib.sha256(token.encode()).hexdigest())
        if ok:
            (self.data / "qr.json").unlink(missing_ok=True)     # one use
        return ok

    # --- pairing -------------------------------------------------------------------------------------------------
    def _expire(self) -> None:
        t = self.now()
        for p in self.pending.values():
            if p.status == "pending" and t - p.created > PAIR_TTL:
                p.status = "expired"
                self._event("pair_expired", device=p.device_name, method=p.method)
        for k in [k for k, p in self.pending.items() if t - p.created > PAIR_TTL * 2]:
            del self.pending[k]
        for k in [k for k, (_, exp) in self.challenges.items() if exp < t]:
            del self.challenges[k]
        for k in [k for k, s in self.sessions.items() if s["expires"] < t]:
            del self.sessions[k]

    def start_pair(self, body: dict, addr: str = "") -> dict:
        with self.lock:
            self._expire()
            t = self.now()
            self.starts = [x for x in self.starts if t - x < 600]
            if len(self.starts) >= STARTS_PER_10MIN:
                raise LinkError(429, "too many link attempts, wait a few minutes")
            self.starts.append(t)
            method = body.get("method")
            if method not in ("qr", "discover"):
                raise LinkError(400, "method must be qr or discover")
            name = "".join(c for c in str(body.get("device_name", ""))[:40] if c.isalnum() or c in " .-_'") or "phone"
            pubkey, nonce = str(body.get("pubkey", "")), str(body.get("nonce", ""))
            pub = load_pubkey(pubkey)
            phone_nonce = b64d(nonce)
            if len(phone_nonce) < 16:
                raise LinkError(400, "nonce too short")
            if not verify(pub, str(body.get("sig", "")), pair_message(method, nonce, name)):
                raise LinkError(401, "signature does not match the key")
            if method == "qr" and not self._use_qr(str(body.get("token", ""))):
                self._event("pair_refused", reason="qr code wrong, used or expired", device=name)
                raise LinkError(403, "QR code expired or already used: show a new one on the Mac")
            if sum(p.status == "pending" for p in self.pending.values()) >= MAX_PENDING:
                raise LinkError(429, "too many phones waiting, answer them on the Mac first")
            mac_nonce = secrets.token_bytes(16)
            p = Pending(secrets.token_urlsafe(18), method, name, pubkey, phone_nonce, mac_nonce,
                        pair_code(self.fp, phone_nonce, mac_nonce), t, addr)
            self.pending[p.pair_id] = p
            self._event("pair_requested", device=name, method=method)
            return {"pair_id": p.pair_id, "mac_nonce": b64e(mac_nonce), "expires_in": PAIR_TTL}

    def pair_status(self, pair_id: str) -> dict:
        with self.lock:
            self._expire()
            p = self.pending.get(pair_id)
            if not p:
                raise LinkError(404, "unknown or expired")
            out = {"status": p.status}
            if p.status == "approved":
                ts = tailnet()
                out.update(device_id=p.device_id, user=p.user, mac_id=self.identity["mac_id"],
                           name=self.identity["name"], lan=lan_addresses(), port=PORT, ts=ts.get("name", ""),
                           ti=ts.get("ip", ""))
            return out

    def decide(self, pair_id: str, decision: str) -> dict:
        with self.lock:
            self._expire()
            p = self.pending.get(pair_id)
            if not p or p.status != "pending":
                raise LinkError(404, "nothing waiting with that id")
            if decision == "deny":
                p.status = "denied"
                self._event("pair_denied", device=p.device_name, method=p.method)
                return {"status": "denied"}
            if decision not in USERS:
                raise LinkError(400, "choose owner, loved_one or deny")
            ds = self.devices()
            dev = {"id": secrets.token_hex(6), "name": p.device_name, "user": decision, "pubkey": p.pubkey,
                   "method": p.method, "paired": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.now())),
                   "last_seen": None, "revoked": None}
            ds.append(dev)
            self._save_devices(ds)
            p.status, p.user, p.device_id = "approved", decision, dev["id"]
            self._event("paired", device=p.device_name, user=decision, method=p.method, device_id=dev["id"])
            return {"status": "approved", "device_id": dev["id"], "user": decision}

    def revoke(self, device_id: str) -> dict:
        with self.lock:
            ds = self.devices()
            d = next((d for d in ds if d["id"] == device_id and not d.get("revoked")), None)
            if not d:
                raise LinkError(404, "no such linked phone")
            d["revoked"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.now()))
            self._save_devices(ds)
            for k in [k for k, s in self.sessions.items() if s["device_id"] == device_id]:
                del self.sessions[k]                    # cut off at once
            self._event("revoked", device=d["name"], user=d["user"], device_id=device_id)
            return {"revoked": device_id}

    def pending_list(self) -> list[dict]:
        with self.lock:
            self._expire()
            return [{"pair_id": p.pair_id, "device": p.device_name, "method": p.method, "code": p.code,
                     "seconds_left": max(0, int(PAIR_TTL - (self.now() - p.created))), "from": p.addr}
                    for p in self.pending.values() if p.status == "pending"]

    # --- sign-in -------------------------------------------------------------------------------------------------
    def _device(self, device_id: str) -> dict:
        d = next((d for d in self.devices() if d["id"] == device_id), None)
        if not d or d.get("revoked"):
            raise LinkError(401, "this phone is not linked (or was removed on the Mac)")
        return d

    def challenge(self, device_id: str) -> dict:
        with self.lock:
            self._expire()
            self._device(device_id)
            c = secrets.token_urlsafe(24)
            self.challenges[c] = (device_id, self.now() + CHALLENGE_TTL)
            return {"challenge": c, "expires_in": CHALLENGE_TTL}

    def sign_in(self, device_id: str, challenge: str, sig: str) -> dict:
        with self.lock:
            self._expire()
            owner = self.challenges.pop(challenge, None)          # one use, even when the signature is wrong
            if not owner or owner[0] != device_id:
                raise LinkError(401, "challenge unknown, used or expired")
            d = self._device(device_id)
            if not verify(load_pubkey(d["pubkey"]), sig, auth_message(device_id, challenge)):
                raise LinkError(401, "signature does not match this phone")
            token = secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = {
                "device_id": device_id, "user": d["user"], "expires": self.now() + SESSION_TTL}
            ds = self.devices()
            for x in ds:
                if x["id"] == device_id:
                    x["last_seen"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.now()))
            self._save_devices(ds)
            return {"session": token, "expires_in": SESSION_TTL, "user": d["user"], "device_id": device_id}

    def session(self, token: str) -> dict:
        with self.lock:
            self._expire()
            s = self.sessions.get(hashlib.sha256(token.encode()).hexdigest()) if token else None
            if not s:
                raise LinkError(401, "sign in again")
            self._device(s["device_id"])                       # revoked since?
            return dict(s)


class Advertiser:
    """Network discovery: macOS's built-in dns-sd announces `_echolink._tcp` on every network the Mac is on.
    Restarted if it ever exits."""

    def __init__(self, name: str, mac_id: str, port: int = PORT):
        self.cmd = ["/usr/bin/dns-sd", "-R", name, "_echolink._tcp", "local", str(port), "v=1", f"id={mac_id}"]
        self.proc: subprocess.Popen | None = None
        self.stop = threading.Event()

    def run(self) -> None:
        # a killed earlier run can leave its dns-sd behind (it would announce a second "Echo"): clear it first
        subprocess.run(["/usr/bin/pkill", "-f", f"dns-sd -R .*_echolink._tcp .*{self.cmd[-1]}"], check=False)
        while not self.stop.is_set():
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while self.proc.poll() is None and not self.stop.wait(5):
                pass
            if self.proc.poll() is None:
                self.proc.terminate()
            self.stop.wait(5)

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True, name="echolink-dns-sd").start()

    def close(self) -> None:
        self.stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
