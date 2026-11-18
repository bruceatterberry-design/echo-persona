"""Signed brain updates with rollback. A "brain package" is a bundle of DATA files (policy templates, model allow-lists, instructions in .json/.txt/.md)
that the owner of the Mesh publishes to the computers in it. By design it can never carry code, an AI's memory, or anything personal.

  publish:  build(name, version, files, seed)            -> package (dict, JSON-safe)      [owner's Mac only; the signing seed never leaves it]
  node:     BrainStore(dir, trusted_public_key).apply()  -> verify signature, version, paths, every file hash; stage; swap atomically;
            run a health check; if it fails, put the previous version back at once. rollback() goes back by hand.
Standard library only (mesh/ed25519.py).
"""
import base64, hashlib, json, os, re, shutil, time

from . import ed25519
from .common import private_dir

FORMAT = 1
MAX_FILES, MAX_BYTES = 200, 5 * 1024 * 1024
OK_EXT = (".json", ".txt", ".md")
PATH_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(?:/[A-Za-z0-9][A-Za-z0-9._\-]*)*$")
KEEP = 3


class BrainError(ValueError):
    pass


def _canon(manifest):
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def safe_path(p):
    if not isinstance(p, str) or not PATH_RX.match(p) or ".." in p.split("/") or not p.lower().endswith(OK_EXT) or len(p) > 120:
        raise BrainError(f"not an allowed file: {p!r}")
    return p


def build(name, version, files, seed, notes=""):
    """files: {relative path: bytes or str}. Returns a package that nodes can verify with public_key(seed)."""
    if not files or len(files) > MAX_FILES:
        raise BrainError("a package needs between 1 and %d files" % MAX_FILES)
    raw, entries, total = {}, {}, 0
    for p, data in files.items():
        safe_path(p)
        b = data.encode() if isinstance(data, str) else bytes(data)
        total += len(b)
        raw[p] = b
        entries[p] = {"sha256": hashlib.sha256(b).hexdigest(), "size": len(b)}
    if total > MAX_BYTES:
        raise BrainError("package too large")
    manifest = {"format": FORMAT, "name": str(name)[:60], "version": int(version), "created": int(time.time()), "notes": str(notes)[:300], "files": entries}
    return {"manifest": manifest, "signature": ed25519.sign(seed, _canon(manifest)).hex(),
            "files": {p: base64.b64encode(b).decode() for p, b in raw.items()}}


def public_key_hex(seed):
    return ed25519.public_key(seed).hex()


def verify(pkg, public_key):
    """-> (manifest, {path: bytes}). Raises BrainError on anything wrong: signature, hashes, sizes, paths, extra or missing files."""
    try:
        man, sig, files = pkg["manifest"], bytes.fromhex(pkg["signature"]), pkg["files"]
        pk = bytes.fromhex(public_key) if isinstance(public_key, str) else public_key
    except (KeyError, TypeError, ValueError):
        raise BrainError("malformed package")
    if not ed25519.verify(pk, _canon(man), sig):
        raise BrainError("signature does not match the owner's key")
    if man.get("format") != FORMAT or not isinstance(man.get("version"), int) or not isinstance(man.get("files"), dict):
        raise BrainError("unsupported package")
    if set(man["files"]) != set(files) or len(files) > MAX_FILES:
        raise BrainError("file list does not match the signed list")
    out, total = {}, 0
    for p, e in man["files"].items():
        safe_path(p)
        try:
            b = base64.b64decode(files[p], validate=True)
        except Exception:
            raise BrainError(f"unreadable file {p}")
        total += len(b)
        if len(b) != e.get("size") or hashlib.sha256(b).hexdigest() != e.get("sha256"):
            raise BrainError(f"file {p} does not match its signed hash")
        out[p] = b
    if total > MAX_BYTES:
        raise BrainError("package too large")
    return man, out


class BrainStore:
    """Where a computer keeps its brain versions. current.json points at the live one; everything swaps with one atomic rename."""

    def __init__(self, root, public_key):
        self.root, self.pk = root, public_key
        private_dir(os.path.join(root, "versions"))

    def _cur(self):
        try:
            return json.load(open(os.path.join(self.root, "current.json")))
        except (OSError, ValueError):
            return {"version": 0, "previous": None}

    def _point(self, version, previous):
        tmp = os.path.join(self.root, "current.json.tmp")
        with open(tmp, "w") as f:
            json.dump({"version": version, "previous": previous}, f)
        os.replace(tmp, os.path.join(self.root, "current.json"))

    def _log(self, event, **kw):
        with open(os.path.join(self.root, "history.log"), "a") as f:
            f.write(json.dumps({"ts": int(time.time()), "event": event, **kw}) + "\n")

    def version(self):
        return self._cur()["version"]

    def path(self, version=None):
        v = version if version is not None else self.version()
        return os.path.join(self.root, "versions", str(v)) if v else None

    def read(self, rel):
        d = self.path()
        return open(os.path.join(d, safe_path(rel)), "rb").read() if d else None

    def apply(self, pkg, health=None):
        """-> (ok, message). health(dir) -> bool runs on the staged version AFTER the swap; False (or an exception) puts the previous version back."""
        try:
            man, files = verify(pkg, self.pk)
        except BrainError as e:
            self._log("refused", reason=str(e))
            return False, str(e)
        cur = self._cur()
        if man["version"] <= cur["version"]:
            self._log("refused", reason="not newer", version=man["version"])
            return False, f"version {man['version']} is not newer than the current {cur['version']}"
        final = os.path.join(self.root, "versions", str(man["version"]))
        stage = final + ".staging"
        shutil.rmtree(stage, ignore_errors=True); shutil.rmtree(final, ignore_errors=True)
        for p, b in files.items():
            dest = os.path.join(stage, p)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(b)
        os.rename(stage, final)
        self._point(man["version"], cur["version"] or None)
        try:
            healthy = True if health is None else bool(health(final))
        except Exception:
            healthy = False
        if not healthy:
            self._point(cur["version"], None)                    # automatic rollback: the old brain is live again before this returns
            shutil.rmtree(final, ignore_errors=True)
            self._log("rolled-back", failed=man["version"], restored=cur["version"])
            return False, f"version {man['version']} failed its health check; version {cur['version']} is back"
        self._log("applied", version=man["version"], previous=cur["version"])
        self._prune()
        return True, f"version {man['version']} is live"

    def rollback(self):
        cur = self._cur()
        prev = cur.get("previous")
        if not prev or not os.path.isdir(self.path(prev)):
            return False, "there is no earlier version to go back to"
        self._point(prev, None)
        self._log("rolled-back-by-hand", from_version=cur["version"], to=prev)
        return True, f"back on version {prev}"

    def _prune(self):
        vs = sorted((int(d) for d in os.listdir(os.path.join(self.root, "versions")) if d.isdigit()), reverse=True)
        keep = set(vs[:KEEP]) | {self.version()}
        for v in vs:
            if v not in keep:
                shutil.rmtree(self.path(v), ignore_errors=True)
