"""Pairing + encryption for the Echo Dream Bridge (docs/dream/BRIDGE_PROTOCOL.md sections 3-4).

Both the Mac and the tower must compute exactly these values, byte for byte.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PAIR_LABEL = b"echo-dream-pair-v1"
KEY_INFO = b"echo-dream-v1"


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def b64d(s: str) -> bytes:
    return base64.b64decode(s, validate=True)


class Ephemeral:
    """One pairing attempt's X25519 key + 16-byte nonce. Thrown away after the attempt."""

    def __init__(self) -> None:
        self._priv = X25519PrivateKey.generate()
        self.pub = self._priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.nonce = os.urandom(16)

    def shared(self, peer_pub: bytes) -> bytes:
        if len(peer_pub) != 32:
            raise ValueError("peer public key must be 32 bytes")
        return self._priv.exchange(X25519PublicKey.from_public_bytes(peer_pub))


COMMIT_LABEL = b"echo-dream-commit-v1"


def commitment(mac_pub: bytes, mac_nonce: bytes) -> bytes:
    """rev c: the Mac commits to its nonce before seeing the tower's values (stops 4-digit grinding)."""
    return hashlib.sha256(COMMIT_LABEL + mac_pub + mac_nonce).digest()


def pair_code(mac_pub: bytes, tower_pub: bytes, mac_nonce: bytes, tower_nonce: bytes) -> str:
    h = hashlib.sha256(PAIR_LABEL + mac_pub + tower_pub + mac_nonce + tower_nonce).digest()
    return f"{int.from_bytes(h[:4], 'big') % 10000:04d}"


def derive_key(shared: bytes, mac_nonce: bytes, tower_nonce: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=mac_nonce + tower_nonce, info=KEY_INFO).derive(shared)


def proof(key: bytes, label: str) -> str:
    """label is "mac-confirm" or "tower-confirm"."""
    return b64e(hmac.new(key, label.encode(), hashlib.sha256).digest())


def proof_ok(key: bytes, label: str, given: str) -> bool:
    return hmac.compare_digest(proof(key, label), given or "")


def _aad(method: str, path: str, sender: str) -> bytes:
    return f"{method} {path} {sender}".encode()


def seal(key: bytes, sender: str, method: str, path: str, obj: dict) -> dict:
    """Encrypt one envelope. Requests: method "POST"/"GET". Responses: method "RESP"."""
    nonce = os.urandom(12)
    pt = json.dumps(obj, separators=(",", ":")).encode()
    ct = AESGCM(key).encrypt(nonce, pt, _aad(method, path, sender))
    return {"v": 1, "from": sender, "nonce": b64e(nonce), "ct": b64e(ct)}


class Rejected(Exception):
    pass


def unseal(key: bytes, method: str, path: str, body: dict) -> dict:
    try:
        if body.get("v") != 1:
            raise Rejected("version")
        nonce, ct = b64d(body["nonce"]), b64d(body["ct"])
        if len(nonce) != 12:
            raise Rejected("nonce")
        pt = AESGCM(key).decrypt(nonce, ct, _aad(method, path, str(body["from"])))
        obj = json.loads(pt)
    except Rejected:
        raise
    except Exception as e:  # bad base64, wrong key, tampered, not JSON
        raise Rejected("decrypt") from e
    if not isinstance(obj, dict):
        raise Rejected("not an object")
    return obj
