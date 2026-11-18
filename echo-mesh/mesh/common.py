"""Shared helpers: request signing, TLS with fingerprint pinning, tiny JSON client."""
import shutil, hashlib, hmac, http.client, json, os, secrets, ssl, subprocess, time

SKEW = 60  # seconds a signed request stays valid
IS_WINDOWS = os.name == "nt"


def private_dir(path: str):
    """Make a folder only its owner should use. On Windows, Python 3.12's makedirs(mode=0o700) builds an ACL that locks out everyone but
    SYSTEM/Administrators/the creator, so a folder made from an admin shell could no longer be opened by the normal user's logon task
    (found 2026-11-18 on the tower: the agent exited "Not enrolled yet"). A user's profile folders are already private to that user on
    Windows, so there the folder simply inherits its parent's permissions."""
    if IS_WINDOWS:
        os.makedirs(path, exist_ok=True)
    else:
        os.makedirs(path, mode=0o700, exist_ok=True)


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sign(secret_hex: str, method: str, path: str, ts: str, nonce: str, body: bytes) -> str:
    msg = f"{method}\n{path}\n{ts}\n{nonce}\n{sha256_hex(body)}".encode()
    return hmac.new(bytes.fromhex(secret_hex), msg, hashlib.sha256).hexdigest()


def ensure_cert(d: str):
    """Self-signed server certificate (made once with the openssl CLI). Returns (crt, key)."""
    private_dir(d)
    crt, key = os.path.join(d, "mesh.crt"), os.path.join(d, "mesh.key")
    if not (os.path.exists(crt) and os.path.exists(key)):
        if not shutil.which("openssl"):
            raise RuntimeError("The Mesh coordinator needs the 'openssl' program to make its certificate, and it was not found. "
                               "(Only the coordinator needs it; the Windows app and the agents do not.)")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt,
                        "-days", "3650", "-subj", "/CN=echo-mesh-coordinator"], check=True, capture_output=True)
        os.chmod(key, 0o600)
    return crt, key


def cert_fingerprint(crt_path: str) -> str:
    with open(crt_path) as f:
        return sha256_hex(ssl.PEM_cert_to_DER_cert(f.read()))


class PinError(Exception):
    pass


class Client:
    """Talks to the coordinator over TLS. The server certificate must match `pin` (or pin is None on first contact)."""

    def __init__(self, host, port, pin=None, node_id=None, secret=None):
        self.host, self.port, self.pin, self.node_id, self.secret = host, port, pin, node_id, secret
        self.seen_fp = None

    def request(self, method, path, obj=None, timeout=30, signed=True):
        body = b"" if obj is None else json.dumps(obj).encode()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # trust comes from the pinned fingerprint, checked below
        conn = http.client.HTTPSConnection(self.host, self.port, context=ctx, timeout=timeout)
        try:
            conn.connect()
            fp = sha256_hex(conn.sock.getpeercert(binary_form=True))
            self.seen_fp = fp
            if self.pin and fp != self.pin:
                raise PinError("coordinator certificate does not match the pinned fingerprint")
            headers = {"Content-Type": "application/json"}
            if signed:
                ts, nonce = str(int(time.time())), secrets.token_hex(12)
                headers.update({"X-Mesh-Node": self.node_id, "X-Mesh-Ts": ts, "X-Mesh-Nonce": nonce,
                                "X-Mesh-Sig": sign(self.secret, method, path, ts, nonce, body)})
            conn.request(method, path, body=body or None, headers=headers)
            r = conn.getresponse()
            data = r.read()
            return r.status, (json.loads(data) if data else None)
        finally:
            conn.close()


def confirm_number(fingerprint: str, request_id: str) -> str:
    """4 digits both sides compute from the coordinator's certificate. If someone sits in the middle, the two screens disagree."""
    return f"{int(sha256_hex((fingerprint + request_id).encode())[:8], 16) % 10000:04d}"


ROLES = ("worker", "peer")      # what a computer can ask to be at setup. Never "primary" or "hub": those are decided by the Mesh owner, not requested.


def local_ipv4s():
    """This computer's private IPv4 addresses (one per network it is on). Uses the routing table trick: connecting a UDP socket sends nothing."""
    import socket
    found = []
    for probe in ("192.168.1.1", "192.168.0.1", "10.0.0.1", "172.16.0.1", "100.100.100.100", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect((probe, 9)); ip = s.getsockname()[0]; s.close()
            if ip not in found and not ip.startswith(("127.", "0.", "169.254.")):
                found.append(ip)
        except OSError:
            pass
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = ai[4][0]
            if ip not in found and not ip.startswith(("127.", "0.", "169.254.")):
                found.append(ip)
    except OSError:
        pass
    return found
