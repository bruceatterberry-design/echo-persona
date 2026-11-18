"""Echo Mesh coordinator (runs on the Mac). Node API over TLS; admin API on 127.0.0.1 only.
Nodes connect OUT to this service; no inbound ports are needed on node machines."""
import hashlib, hmac, json, os, secrets, socket, ssl, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from . import COORD_PORT, ADMIN_PORT, BEACON_PORT, VERSION
from .common import private_dir, local_ipv4s, sign, sha256_hex, ensure_cert, cert_fingerprint, SKEW, confirm_number, ROLES
import ipaddress
from .store import Store
from .tasks import Tasks
from .boards import Boards
from . import brainpkg
from . import policy

NODE_TYPES = ("primary", "brain", "compute")
ONLINE_S = 30        # a node is online if it was heard from this recently
JOB_TIMEOUT_S = 600  # a job assigned this long without a result is requeued
MAX_ATTEMPTS = 3
CODE_TTL_S = 300
CODE_MAX_FAILS = 5
REQ_TTL_S = 600          # a join request waits this long for the owner
REQ_MAX_PENDING = 10     # overall, and 3 per address
DEBT_CAP_S = 300.0       # an outside participant may use at most this many more seconds than it has given (settlement rule; a proposal to confirm)
MAX_OPEN_JOBS = 5        # jobs one outside node may have waiting or running at once


def is_lan(addr: str) -> bool:
    """A private home-network address. Tailscale/CGNAT (100.64/10) and anything public is NOT 'LAN': those must use a pairing code."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_private and not (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"))


class Coordinator:
    def __init__(self, data_dir, coord_port=COORD_PORT, admin_port=ADMIN_PORT, beacon_port=BEACON_PORT, bind="0.0.0.0"):
        self.dir = data_dir
        self._wrong = {}            # wrong join-number tries per request
        policy.set_terms(os.path.join(data_dir, "private-terms.json"))   # names of family, pets, town: this owner's own file, never in the repo
        self.store = Store(os.path.join(data_dir, "mesh.db"))
        self.crt, self.key = ensure_cert(os.path.join(data_dir, "tls"))
        self.fp = cert_fingerprint(self.crt)
        self.admin_token = self._load_admin_token()
        self.client_token = self._load_client_token()
        self.ports = (coord_port, admin_port, beacon_port)
        self.bind = bind
        self.nonces = {}          # nonce -> expiry (replay protection)
        self.nlock = threading.Lock()
        self.stop = threading.Event()
        self.servers = []
        self.tasks = Tasks(self)
        self.boards = Boards(self.store)
        self.started = time.time()

    def _load_admin_token(self):
        """Admin calls need this bearer token (so other programs on the Mac cannot approve or revoke nodes)."""
        p = os.path.join(self.dir, "admin.token")
        if not os.path.exists(p):
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(24))
        return open(p).read().strip()

    def _load_client_token(self):
        """A second, much weaker token for programs that only need to hand the Mesh a text job and read the answer (Echo's background offload).
        It cannot approve, pause, revoke, pair, read boards, change settings or see a computer's details: see Handler._client."""
        p = os.path.join(self.dir, "client.token")
        if not os.path.exists(p):
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(24))
        return open(p).read().strip()

    # ---- signed brain updates (the owner's Mac signs; every computer verifies with the public key it was given when it joined) ----
    def _brain_seed(self):
        p = os.path.join(self.dir, "brain-signing.seed")
        if not os.path.exists(p):
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
        return bytes.fromhex(open(p).read().strip())

    def brain_key(self):
        return brainpkg.public_key_hex(self._brain_seed())

    def brain_publish(self, files, notes=""):
        cur = self.brain_latest_version()
        pkg = brainpkg.build("echo-mesh-brain", cur + 1, files, self._brain_seed(), notes)
        private_dir(os.path.join(self.dir, "brain"))
        tmp = os.path.join(self.dir, "brain", "latest.json.tmp")
        with open(tmp, "w") as f:
            json.dump(pkg, f)
        os.replace(tmp, os.path.join(self.dir, "brain", "latest.json"))
        self.store.audit("brain-published", version=cur + 1, files=len(files))
        return cur + 1

    def _brain_pkg(self):
        try:
            return json.load(open(os.path.join(self.dir, "brain", "latest.json")))
        except (OSError, ValueError):
            return None

    def brain_latest_version(self):
        p = self._brain_pkg()
        return p["manifest"]["version"] if p else 0

    # ---- duplicate identity: one computer = one entry; one entry = one computer ----
    @staticmethod
    def _mid(inv):
        m = (inv or {}).get("machine_id")
        return m if isinstance(m, str) and 8 <= len(m) <= 64 else None

    def same_machine(self, inv, exclude=None):
        """Existing (not removed) nodes that report the same machine id as this inventory."""
        mid = self._mid(inv)
        if not mid:
            return []
        return [n for n in self.store.q("SELECT id, name, inventory, status FROM nodes WHERE status!='revoked'")
                if n["id"] != exclude and self._mid(json.loads(n["inventory"] or "{}")) == mid]

    def _retire_duplicates(self, new_id, inv):
        """The same computer joined again (reinstall): the older entry is removed so the Mesh never counts one computer twice."""
        for n in self.same_machine(inv, exclude=new_id):
            self.store.x("UPDATE nodes SET status='revoked' WHERE id=?", (n["id"],))
            self.store.audit("duplicate-retired", node=n["id"], replaced_by=new_id, name=n["name"])

    # ---- pairing and enrollment ----
    def new_pairing_code(self, node_type="compute", owner="owner", lan=True, ttl=CODE_TTL_S):
        ttl = max(60, min(3600, float(ttl))) if ttl >= 0 else ttl      # a code lives 1 to 60 minutes (negative only for tests)
        if node_type not in NODE_TYPES:
            raise ValueError("bad node type")
        code = f"{secrets.randbelow(10000):04d}"
        salt = secrets.token_hex(8)
        self.store.x("INSERT INTO pairing(code_hash,salt,node_type,owner,lan,expires,created) VALUES(?,?,?,?,?,?,?)",
                     (sha256_hex((salt + code).encode()), salt, node_type, owner, int(lan), time.time() + ttl, time.time()))
        self.store.audit("pairing-code-created", node_type=node_type, lan=lan, owner=owner)
        return code

    def enroll(self, code, name, inventory, remote="?"):
        now = time.time()
        rows = self.store.q("SELECT * FROM pairing WHERE used=0 AND expires>? AND attempts<?", (now, CODE_MAX_FAILS))
        for r in rows:
            if hmac.compare_digest(r["code_hash"], sha256_hex((r["salt"] + str(code)).encode())):
                self.store.x("UPDATE pairing SET used=1 WHERE id=?", (r["id"],))
                nid, secret = "n-" + uuid.uuid4().hex[:10], secrets.token_hex(32)
                status = "approved" if r["lan"] else "pending"   # outside participants always need the owner's approval
                self.store.x("INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (nid, name, r["node_type"], r["owner"], secret, status, r["lan"], json.dumps(inventory),
                              "{}", now, 100.0 if r["lan"] else 30.0, now))
                self.store.audit("node-enrolled", node=nid, name=name, node_type=r["node_type"], status=status, remote=remote)
                if status == "approved":
                    self._retire_duplicates(nid, inventory)
                return {"node_id": nid, "secret": secret, "status": status, "node_type": r["node_type"], "brain_key": self.brain_key()}
        # wrong code: burn attempts on every live code so guessing is bounded
        self.store.x("UPDATE pairing SET attempts=attempts+1 WHERE used=0 AND expires>?", (now,))
        self.store.audit("enroll-failed", remote=remote, name=name)
        return None

    # ---- automatic join on the home network (needs the owner's approval and a matching number) ----
    def join_request(self, name, inventory, remote):
        if not is_lan(remote):
            return None, "outside"
        now = time.time()
        self.store.x("UPDATE join_requests SET status='expired' WHERE status='pending' AND created<?", (now - REQ_TTL_S,))
        pend = self.store.q("SELECT remote FROM join_requests WHERE status='pending'")
        if len(pend) >= REQ_MAX_PENDING or sum(1 for p in pend if p["remote"] == remote) >= 3:
            self.store.audit("join-request-refused", remote=remote, reason="too many pending")
            return None, "busy"
        rid, token = "r-" + uuid.uuid4().hex[:10], secrets.token_hex(24)
        self.store.x("INSERT INTO join_requests(id,token_hash,name,inventory,remote,status,created) VALUES(?,?,?,?,?,'pending',?)",
                     (rid, sha256_hex(token.encode()), name, json.dumps(inventory), remote, now))
        self.store.audit("join-request", request=rid, name=name, remote=remote)
        return {"request_id": rid, "poll_token": token, "expires_in_s": REQ_TTL_S}, None

    def join_poll(self, rid, token):
        r = self.store.one("SELECT * FROM join_requests WHERE id=?", (rid,))
        if not r or not hmac.compare_digest(r["token_hash"], sha256_hex(str(token).encode())):
            return None
        if r["status"] == "pending" and time.time() - r["created"] > REQ_TTL_S:
            self.store.x("UPDATE join_requests SET status='expired' WHERE id=?", (rid,))
            return {"status": "expired"}
        if r["status"] == "approved" and not r["delivered"]:
            self.store.x("UPDATE join_requests SET delivered=1 WHERE id=?", (rid,))      # the secret is handed over exactly once
            n = self.store.one("SELECT id, secret, node_type FROM nodes WHERE id=?", (r["node_id"],))
            return {"status": "approved", "node_id": n["id"], "secret": n["secret"], "node_type": n["node_type"], "brain_key": self.brain_key()}
        return {"status": "approved-delivered" if r["status"] == "approved" else r["status"]}

    def pending_requests(self):
        now = time.time()
        self.store.x("UPDATE join_requests SET status='expired' WHERE status='pending' AND created<?", (now - REQ_TTL_S,))
        out = []
        for r in self.store.q("SELECT * FROM join_requests WHERE status='pending' ORDER BY created"):
            inv = json.loads(r["inventory"] or "{}")
            out.append({"id": r["id"], "name": r["name"], "remote": r["remote"], "age_s": round(now - r["created"]),
                        "confirm": confirm_number(self.fp, r["id"]), "duplicate_of": [d["name"] for d in self.same_machine(inv)] or None, "threads": inv.get("cpu_threads"), "os": inv.get("os"),
                        "role": inv.get("role") if inv.get("role") in ROLES else None})          # what the computer asked to be; approving it still only makes a compute node
        return out

    def decide_request(self, rid, approve, number=None, require_number=False):
        """require_number (always on for the admin API): approving needs the 4-digit number the NEW computer shows, typed on the Mac. The number comes from this
        Mac's certificate, so a computer that reached some other machine in the middle shows a different one. Three wrong tries turn the request down."""
        r = self.store.one("SELECT * FROM join_requests WHERE id=? AND status='pending'", (rid,))
        if not r or time.time() - r["created"] > REQ_TTL_S:
            return False
        if approve and require_number:
            if not hmac.compare_digest(str(number or "").strip().encode(), confirm_number(self.fp, rid).encode()):
                bad = self._wrong.get(rid, 0) + 1
                self._wrong[rid] = bad
                self.store.audit("join-number-mismatch", request=rid, tries=bad)
                if bad >= 3:
                    self.store.x("UPDATE join_requests SET status='denied' WHERE id=?", (rid,))
                    self.store.audit("join-denied", request=rid, reason="number did not match three times")
                return "mismatch"
        if not approve:
            self.store.x("UPDATE join_requests SET status='denied' WHERE id=?", (rid,))
            self.store.audit("join-denied", request=rid)
            return True
        nid, secret, now = "n-" + uuid.uuid4().hex[:10], secrets.token_hex(32), time.time()
        # a join request can only ever create a COMPUTE node; brain/primary needs the owner's separate pairing-code decision
        self.store.x("INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (nid, r["name"], "compute", "owner", secret, "approved", 1, r["inventory"], "{}", now, 100.0, now))
        self.store.x("UPDATE join_requests SET status='approved', node_id=? WHERE id=?", (nid, rid))
        self.store.audit("join-approved", request=rid, node=nid, name=r["name"])
        self._retire_duplicates(nid, json.loads(r["inventory"] or "{}"))
        return True

    def approve(self, node_id):
        n = self.store.x("UPDATE nodes SET status='approved' WHERE id=? AND status='pending'", (node_id,))
        self.store.audit("node-approved", node=node_id)
        return n > 0

    def set_status(self, node_id, status):
        n = self.store.x("UPDATE nodes SET status=? WHERE id=?", (status, node_id))
        self.store.audit("node-" + status, node=node_id)
        return n > 0

    # ---- request authentication ----
    def authenticate(self, method, path, headers, body):
        nid = headers.get("X-Mesh-Node", "")
        ts, nonce, sig = headers.get("X-Mesh-Ts", ""), headers.get("X-Mesh-Nonce", ""), headers.get("X-Mesh-Sig", "")
        node = self.store.one("SELECT * FROM nodes WHERE id=?", (nid,))
        if not node or not ts.isdigit() or abs(time.time() - int(ts)) > SKEW or not nonce:
            return None, "unauthorized"
        if not hmac.compare_digest(sig, sign(node["secret"], method, path, ts, nonce, body)):
            return None, "unauthorized"
        with self.nlock:
            now = time.time()
            self.nonces = {k: v for k, v in self.nonces.items() if v > now}
            if nonce in self.nonces:
                return None, "replay"
            self.nonces[nonce] = now + 2 * SKEW
        if node["status"] != "approved":
            return node, node["status"]     # pending / revoked / paused: known but not allowed to work
        return node, None

    # ---- node operations ----
    def heartbeat(self, node, info):
        mid = info.get("machine_id") if isinstance(info.get("machine_id"), str) else None
        known = self._mid(json.loads(node["inventory"] or "{}"))
        if mid and known and mid != known:
            # the same node id and secret, but a different computer: an install was copied. Pause it until the owner decides.
            self.store.x("UPDATE nodes SET status='paused' WHERE id=?", (node["id"],))
            self.store.audit("identity-conflict", node=node["id"], name=node["name"])
            return {"ok": False, "error": "identity-conflict"}
        if mid and not known:                                  # older agent: remember its computer the first time it says
            inv = json.loads(node["inventory"] or "{}"); inv["machine_id"] = mid
            self.store.x("UPDATE nodes SET inventory=? WHERE id=?", (json.dumps(inv), node["id"]))
        self.store.x("UPDATE nodes SET last_seen=?, load=?, inventory=COALESCE(?,inventory) WHERE id=?",
                     (time.time(), json.dumps(info.get("load", {})),
                      json.dumps(info["inventory"]) if info.get("inventory") else None, node["id"]))
        return {"ok": True, "coordinator_time": time.time(), "share_limit": node["share_limit"], "owner_balance": self.exchange_balance(node["owner"])}

    def next_job(self, node, wait=0):
        deadline = time.time() + wait
        while True:
            job = self._claim(node)
            if job or time.time() >= deadline or self.stop.is_set():
                return job
            time.sleep(0.3)

    def _claim(self, node):
        inv = json.loads(node["inventory"] or "{}")
        have = set(inv.get("ollama", {}).get("models", []))
        with self.store.lock:
            for j in self.store.q("SELECT * FROM jobs WHERE status='queued' ORDER BY created"):
                need = json.loads(j["requires"] or "{}")
                if j["requester"] == node["id"]:
                    continue                    # a node never runs its own request: the point is that others help
                if not node["lan"] and not j["ext_ok"]:
                    continue                    # an outside computer only gets work the owner chose to send out
                if need.get("model") and need["model"] not in have:
                    continue
                if need.get("node") and need["node"] != node["id"]:
                    continue
                self.store.x("UPDATE jobs SET status='assigned', node=?, assigned=?, attempts=attempts+1 WHERE id=?",
                             (node["id"], time.time(), j["id"]))
                self.store.x("UPDATE nodes SET last_seen=? WHERE id=?", (time.time(), node["id"]))
                return {"id": j["id"], "type": j["type"], "payload": json.loads(j["payload"])}
        return None

    def job_result(self, node, job_id, ok, result, error):
        j = self.store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not j or j["node"] != node["id"] or j["status"] != "assigned":
            return False   # not yours, or already requeued/finished
        self.store.x("UPDATE jobs SET status=?, result=?, error=?, finished=? WHERE id=?",
                     ("done" if ok else "failed", json.dumps(result), error, time.time(), job_id))
        self.store.audit("job-" + ("done" if ok else "failed"), job=job_id, node=node["id"])
        return True

    # ---- jobs (admin side) ----
    def get_setting(self, key, default="0"):
        r = self.store.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else default

    def set_setting(self, key, value):
        self.store.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        self.store.audit("setting", key=key, value=str(value))

    def submit_job(self, jtype, payload, project="default", requires=None, requester="owner", ext_ok=None):
        """ext_ok: may a computer owned by someone else run this job? Only the owner's own jobs can say yes (and only when he has
        switched 'let outside computers help' on, or says so for that one job). Jobs asked for by an outside node never go outside."""
        if jtype not in ("ping", "generate"):
            raise ValueError("unsupported job type")
        if ext_ok is None:
            ext_ok = jtype == "generate" and self.get_setting("ext_help") == "1"
        ext_ok = bool(ext_ok) and requester == "owner"
        if ext_ok and jtype == "generate":
            # 11-17 review: this path used to send prompts to someone else's computer with no check at all. Same rule as tasks: personal details and
            # secrets are removed; school work or a private key keeps the job at home (it runs only on this owner's own computers).
            ok, clean, why, found = policy.check_dispatch("shared", str((payload or {}).get("prompt", "")), True)
            if ok:
                payload = {**payload, "prompt": clean}
                if found:
                    self.store.audit("job-cleaned-for-outside", kinds=",".join(found))
            else:
                ext_ok = False
                self.store.audit("job-kept-home", reason=why)
        jid = "j-" + uuid.uuid4().hex[:10]
        self.store.x("INSERT INTO jobs(id,project,type,payload,requires,status,created,requester,ext_ok) VALUES(?,?,?,?,?,'queued',?,?,?)",
                     (jid, project, jtype, json.dumps(payload), json.dumps(requires or {}), time.time(), requester, int(ext_ok)))
        self.store.audit("job-submitted", job=jid, type=jtype, requester=requester, ext_ok=ext_ok)
        return jid

    # ---- a node asks for work from the others (a partner AI's side of the exchange) ----
    def node_submit(self, node, obj):
        if obj.get("type") != "generate":
            return None, "Only model requests (generate) can be asked of the Mesh."
        if not node["lan"]:
            bal = self.exchange_balance(node["owner"])
            open_n = self.store.one("SELECT COUNT(*) c FROM jobs WHERE requester=? AND status IN ('queued','assigned')", (node["id"],))["c"]
            if open_n >= MAX_OPEN_JOBS:
                return None, "You already have the most requests waiting that are allowed. Wait for some to finish."
            if bal["owed_s"] > DEBT_CAP_S:
                return None, "Over your allowance: your side has used more of the Mesh than it has given. Let your computer help with a job, then ask again."
        need = obj.get("requires") or {}
        payload = obj.get("payload") or {}
        if not payload.get("model") or not payload.get("prompt"):
            return None, "A request needs a model and a prompt."
        jid = self.submit_job("generate", {k: payload[k] for k in ("model", "prompt", "think") if k in payload}, "from-" + node["id"],
                              {"model": need.get("model") or payload["model"]}, requester=node["id"], ext_ok=False)
        return jid, None

    def node_job_view(self, node, jid):
        j = self.store.one("SELECT * FROM jobs WHERE id=? AND requester=?", (jid, node["id"]))      # a node sees only its own requests
        if not j:
            return None
        return {"id": j["id"], "status": j["status"], "result": json.loads(j["result"]) if j["result"] else None, "error": j["error"]}

    # ---- the sharing balance between owners ----
    def _owner(self, nid, nodes):
        return "owner" if nid == "owner" else (nodes.get(nid) or "unknown")

    def exchange(self, since_s=7 * 86400):
        """Seconds of finished work one owner's computers did for another owner. Only work across owners counts."""
        nodes = {n["id"]: n["owner"] for n in self.store.q("SELECT id, owner FROM nodes")}
        cut = time.time() - since_s
        flows = {}
        for j in self.store.q("SELECT node, requester, assigned, finished FROM jobs WHERE status='done' AND finished>?", (cut,)):
            a, b = self._owner(j["node"], nodes), self._owner(j["requester"], nodes)
            if a != b:
                f = flows.setdefault((a, b), {"from": a, "to": b, "seconds": 0.0, "jobs": 0})
                f["seconds"] += max(0.0, j["finished"] - j["assigned"]); f["jobs"] += 1
        return [{**f, "seconds": round(f["seconds"], 2)} for f in flows.values()]

    def exchange_balance(self, owner):
        """How much `owner` has given and used, across everyone else. owed_s > 0 means they have used more than they gave."""
        gave = used = 0.0
        for f in self.exchange():
            if f["from"] == owner:
                gave += f["seconds"]
            if f["to"] == owner:
                used += f["seconds"]
        return {"gave_s": round(gave, 2), "used_s": round(used, 2), "owed_s": round(used - gave, 2)}

    def reap(self):
        """Recover from disconnection: put work held by a silent node or a stuck job back in the queue."""
        now = time.time()
        for j in self.store.q("SELECT j.id, j.node, j.assigned, j.attempts, n.last_seen FROM jobs j "
                              "LEFT JOIN nodes n ON n.id=j.node WHERE j.status='assigned'"):
            silent = j["last_seen"] is None or now - j["last_seen"] > ONLINE_S
            if silent or now - j["assigned"] > JOB_TIMEOUT_S:
                if j["attempts"] >= MAX_ATTEMPTS:
                    self.store.x("UPDATE jobs SET status='failed', error='gave up after repeated failures' WHERE id=?", (j["id"],))
                else:
                    self.store.x("UPDATE jobs SET status='queued', node=NULL WHERE id=?", (j["id"],))
                self.store.audit("job-recovered", job=j["id"], node=j["node"], reason="node silent" if silent else "timeout")

    def ledger(self, since_s=86400):
        """Measured work per node: jobs done/failed and seconds of execution (assigned -> finished). Raw seconds only; they are NOT
        yet normalised by hardware capability, so they must not be compared across different machines as 'equal credit'."""
        cut = time.time() - since_s
        out = []
        for n in self.store.q("SELECT id, name FROM nodes ORDER BY created"):
            rows = self.store.q("SELECT status, assigned, finished FROM jobs WHERE node=? AND finished>?", (n["id"], cut))
            done = [r for r in rows if r["status"] == "done"]
            out.append({"node": n["id"], "name": n["name"], "window_s": since_s, "jobs_done": len(done),
                        "jobs_failed": sum(1 for r in rows if r["status"] == "failed"),
                        "busy_seconds": round(sum(max(0.0, r["finished"] - r["assigned"]) for r in done), 2)})
        return out

    FRIENDLY = {"node-enrolled": "{name} joined the Mesh", "join-approved": "{name} was let in", "job-recovered": "A job moved to another computer",
                "node-paused": "{name} was paused", "identity-conflict": "{name} was paused: a copy of it appeared on another computer", "duplicate-retired": "{name} was replaced by the same computer joining again", "node-revoked": "{name} was removed", "node-approved": "{name} was approved"}

    def overview(self, node):
        """What one computer may see of the whole Mesh, for its dashboard. A computer on the home network sees everything (names, hardware, projects);
        a computer owned by someone else sees only itself, plus plain totals. Nothing here contains a secret, an id that grants access, or any prompt."""
        lan = bool(node["lan"])
        now = time.time()
        st = self.status()
        led = {r["node"]: r for r in self.ledger()}
        total_busy = sum(r["busy_seconds"] for r in led.values()) or 0.0
        nodes = []
        for n in st["nodes"]:
            if n["status"] != "approved" or (not lan and n["id"] != node["id"]):
                continue
            inv, load = n["inventory"], n["load"]
            gov = load.get("governor") or {}
            nodes.append({"name": n["name"], "type": n["node_type"], "role": inv.get("role") if inv.get("role") in ROLES else None, "online": n["online"], "is_me": n["id"] == node["id"], "owner": n["owner"] if lan else "",
                          "threads": inv.get("cpu_threads"), "ram_gb": round((inv.get("ram_bytes") or 0) / 2**30, 1) or None,
                          "gpu": (inv.get("gpus") or [{}])[0].get("name") if inv.get("gpus") else None,
                          "models": list((inv.get("ollama") or {}).get("models") or [])[:8] if lan or n["id"] == node["id"] else [],
                          "load1": load.get("load1"), "rtt_ms": load.get("rtt_ms"), "limit": gov.get("share_limit_pct"), "used": gov.get("used_pct"),
                          "share_of_work": round(100.0 * led.get(n["id"], {}).get("busy_seconds", 0) / total_busy, 1) if total_busy and lan else None,
                          "ai_agreed": (inv.get("ai_handshake") or {}).get("accepted") if lan else None})
        led24 = list(led.values())
        names = {n["id"]: n["name"] for n in st["nodes"]}
        events = []
        for a in self.store.q("SELECT ts, event, detail FROM audit ORDER BY id DESC LIMIT 200"):
            d = json.loads(a["detail"] or "{}")
            who = names.get(d.get("node")) or d.get("name") or "A computer"
            if a["event"] == "job-done":
                text = f"{who} finished a job"
            elif a["event"] in self.FRIENDLY:
                text = self.FRIENDLY[a["event"]].format(name=who)
            elif lan and a["event"] in ("task-accepted", "task-review", "task-running"):
                text = {"task-accepted": "A task was accepted", "task-review": "A task is ready for review", "task-running": "A task started"}[a["event"]]
            else:
                continue
            if not lan and d.get("node") != node["id"]:
                continue                                              # outside computers only hear about themselves
            events.append({"ts": a["ts"], "text": text})
            if len(events) >= 12:
                break
        projects = []
        if lan:
            for p in self.tasks.projects()[:5]:
                running = self.store.one("SELECT title FROM tasks WHERE project=? AND status='running' ORDER BY started LIMIT 1", (p["id"],))
                projects.append({"title": p["title"], "tasks": p["tasks"], "accepted": p["accepted"], "waiting_for_you": p["waiting_for_you"], "running": running["title"] if running else None})
        return {"now": now, "mesh": {"version": VERSION, "uptime_s": round(now - self.started), "primary": self.get_setting("primary_name", "Mac mini")},
                "nodes": nodes, "projects": projects, "events": events,
                "jobs": {"queued": st["jobs"]["queued"], "running": st["jobs"]["assigned"], "done_24h": sum(r["jobs_done"] for r in led24), "failed_24h": sum(r["jobs_failed"] for r in led24)},
                "balance": self.exchange_balance(node["owner"]) if not lan else None}

    def status(self):
        now = time.time()
        nodes = []
        for n in self.store.q("SELECT * FROM nodes ORDER BY created"):
            nodes.append({"id": n["id"], "name": n["name"], "node_type": n["node_type"], "owner": n["owner"],
                          "status": n["status"], "lan": bool(n["lan"]), "share_limit": n["share_limit"],
                          "online": bool(n["last_seen"]) and now - n["last_seen"] < ONLINE_S and n["status"] == "approved",
                          "last_seen_s": None if not n["last_seen"] else round(now - n["last_seen"], 1),
                          "load": json.loads(n["load"] or "{}"), "inventory": json.loads(n["inventory"] or "{}")})
        counts = {s: (self.store.one("SELECT COUNT(*) c FROM jobs WHERE status=?", (s,)) or {"c": 0})["c"]
                  for s in ("queued", "assigned", "done", "failed")}
        return {"version": VERSION, "fingerprint": self.fp, "nodes": nodes, "jobs": counts}

    # ---- servers ----
    def start(self):
        coord, admin, beacon = self.ports
        me = self
        class NodeH(_Handler):
            c = me
            admin = False
        class AdminH(_Handler):
            c = me
            admin = True
        ns = ThreadingHTTPServer((self.bind, coord), NodeH)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.crt, self.key)
        ns.socket = ctx.wrap_socket(ns.socket, server_side=True)
        ads = ThreadingHTTPServer(("127.0.0.1", admin), AdminH)
        self.ports = (ns.server_address[1], ads.server_address[1], beacon)
        self.servers = [ns, ads]
        for s in self.servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        threading.Thread(target=self._reaper, daemon=True).start()
        threading.Thread(target=self._beacon, daemon=True).start()
        return self

    def _reaper(self):
        while not self.stop.wait(5):
            try:
                self.reap()
                self.tasks.advance()
            except Exception as e:
                print("reaper:", e, file=sys.stderr)

    def _beacon(self):
        """UDP discovery: answer 'ECHOMESH?' with where to connect. Nodes still must pair with a code."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.bind, self.ports[2]))
        except OSError as e:
            print("discovery beacon not started:", e, file=sys.stderr)
            return
        self.ports = (self.ports[0], self.ports[1], s.getsockname()[1])
        s.settimeout(1)
        addrs = local_ipv4s()[:6]                        # looked up once: the lookup can be slow, and a beacon answer must be fast
        last = time.time()
        try:
            while not self.stop.is_set():
                if time.time() - last > 300:
                    addrs, last = local_ipv4s()[:6], time.time()
                try:
                    data, addr = s.recvfrom(256)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if data.strip() == b"ECHOMESH?":
                    s.sendto(json.dumps({"echomesh": VERSION, "port": self.ports[0], "fingerprint_prefix": self.fp[:12], "addrs": addrs}).encode(), addr)
        finally:
            s.close()

    def shutdown(self):
        self.stop.set()
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self.store.db.close()


class _Handler(BaseHTTPRequestHandler):
    c: Coordinator
    admin = False
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        data = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(n, 4_000_000)) if n else b""

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        try:
            u = urlparse(self.path)
            body = self._body()
            obj = json.loads(body) if body else {}
            (self._admin if self.admin else self._node)(method, u, obj, body)
        except (ValueError, KeyError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": "server error"})
            print("handler:", repr(e), file=sys.stderr)

    def _node(self, method, u, obj, body):
        c = self.c
        if method == "POST" and u.path == "/enroll":
            r = c.enroll(obj.get("code"), str(obj.get("name", ""))[:60], obj.get("inventory", {}), self.client_address[0])
            return self._send(200, r) if r else self._send(403, {"error": "code not accepted"})
        if method == "POST" and u.path == "/join-request":
            r, why = c.join_request(str(obj.get("name", ""))[:60], obj.get("inventory", {}), self.client_address[0])
            if r:
                return self._send(200, r)
            return self._send(403 if why == "outside" else 429, {"error": "use a pairing code" if why == "outside" else "too many waiting requests"})
        if method == "GET" and u.path.startswith("/join-request/"):
            r = c.join_poll(u.path.split("/")[2], parse_qs(u.query).get("token", [""])[0])
            return self._send(200, r) if r else self._send(404, {"error": "unknown request"})
        node, err = c.authenticate(method, self.path, self.headers, body)
        if err in ("unauthorized", "replay"):
            return self._send(401, {"error": err})
        if err:   # known node that may not work yet
            return self._send(403, {"error": err, "status": err})
        if method == "POST" and u.path == "/heartbeat":
            return self._send(200, c.heartbeat(node, obj))
        if method == "GET" and u.path == "/jobs/next":
            wait = min(float(parse_qs(u.query).get("wait", ["0"])[0]), 25)
            job = c.next_job(node, wait)
            return self._send(200, job) if job else self._send(204)
        if method == "GET" and u.path == "/overview":
            return self._send(200, c.overview(node))
        if method == "GET" and u.path == "/brain":
            have = int(parse_qs(u.query).get("have", ["0"])[0] or 0)
            pkg = c._brain_pkg()
            return self._send(200, pkg if pkg and pkg["manifest"]["version"] > have else {"version": c.brain_latest_version()})
        if method == "GET" and u.path == "/boards":
            return self._send(200, c.boards.boards_for(node))
        if u.path.startswith("/boards/"):
            bid = u.path.split("/")[2]
            if method == "GET":
                v = c.boards.read(node, bid, float(parse_qs(u.query).get("since", ["0"])[0]))
                return self._send(200 if v is not None else 404, v if v is not None else {"error": "not your board"})
            if method == "POST":
                pid, why = c.boards.node_post(node, bid, obj.get("body"))
                return self._send(200, {"id": pid}) if pid else self._send(403, {"error": why})
        if method == "POST" and u.path == "/jobs":
            jid, why = c.node_submit(node, obj)
            return self._send(200, {"id": jid}) if jid else self._send(429, {"error": why})
        if method == "GET" and u.path.startswith("/jobs/") and not u.path.endswith("/next"):
            v = c.node_job_view(node, u.path.split("/")[2])
            return self._send(200, v) if v else self._send(404, {"error": "not your request"})
        if method == "POST" and u.path.startswith("/jobs/") and u.path.endswith("/result"):
            ok = c.job_result(node, u.path.split("/")[2], bool(obj.get("ok")), obj.get("result"), obj.get("error"))
            return self._send(200 if ok else 409, {"accepted": ok})
        self._send(404, {"error": "not found"})

    def _client(self, method, u, obj):
        """What the client token may do, and nothing else: see how many computers/models are online, hand over `generate` jobs
        (model + prompt + think only, always kept on the owner's own computers, never sent outside), and read back ITS OWN jobs."""
        c = self.c
        if method == "GET" and u.path == "/offload/status":
            on = [n for n in c.status()["nodes"] if n["online"]]
            models = {}
            for n in on:
                for m in ((n.get("inventory") or {}).get("ollama") or {}).get("models") or []:
                    models[m] = models.get(m, 0) + 1
            return self._send(200, {"online": len(on), "models": models})
        if method == "POST" and u.path == "/jobs":
            if obj.get("type") != "generate":
                return self._send(403, {"error": "the client token may only send generate jobs"})
            pl = obj.get("payload") or {}
            model, prompt = str(pl.get("model", "")), str(pl.get("prompt", ""))
            if not model or not prompt or len(prompt) > policy.MAX_PROMPT_CHARS:
                return self._send(400, {"error": "a job needs a model and a prompt (up to %d characters)" % policy.MAX_PROMPT_CHARS})
            if c.store.one("SELECT COUNT(*) c FROM jobs WHERE project='echo-offload' AND status IN ('queued','assigned')")["c"] >= 30:
                return self._send(429, {"error": "too many offload jobs are waiting"})
            clean = {"model": model, "prompt": prompt, **({"think": bool(pl["think"])} if "think" in pl else {})}
            jid = c.submit_job("generate", clean, "echo-offload", {"model": model}, ext_ok=False)      # never an outside computer
            return self._send(200, {"id": jid})
        if method == "GET" and u.path.startswith("/jobs/"):
            j = c.store.one("SELECT id,status,result,error,project FROM jobs WHERE id=?", (u.path.split("/")[2],))
            if not j or j["project"] != "echo-offload":
                return self._send(404, {"error": "not found"})
            return self._send(200, {"id": j["id"], "status": j["status"], "result": j["result"], "error": j["error"]})
        c.store.audit("client-refused", method=method, path=u.path)
        return self._send(403, {"error": "the client token may not do that"})

    def _admin(self, method, u, obj, body):
        c = self.c
        got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(got.encode(), c.admin_token.encode()):
            if got and hmac.compare_digest(got.encode(), c.client_token.encode()):
                return self._client(method, u, obj)          # the limited token: only the few routes in _client
            c.store.audit("admin-refused", path=u.path)
            return self._send(401, {"error": "admin token required"})
        if method == "GET" and u.path == "/status":
            return self._send(200, c.status())
        if method == "GET" and u.path == "/jobs":
            return self._send(200, c.store.q("SELECT id,project,type,status,node,attempts,created,finished FROM jobs ORDER BY created DESC LIMIT 50"))
        if method == "GET" and u.path.startswith("/jobs/"):
            j = c.store.one("SELECT * FROM jobs WHERE id=?", (u.path.split("/")[2],))
            return self._send(200 if j else 404, j)
        if method == "GET" and u.path == "/projects":
            return self._send(200, c.tasks.projects())
        if method == "POST" and u.path == "/projects":
            return self._send(200, {"id": c.tasks.new_project(obj["title"], obj.get("owner", "owner"))})
        if method == "GET" and u.path.startswith("/projects/"):
            parts = u.path.split("/")
            v = c.tasks.workspace(parts[2]) if len(parts) > 3 and parts[3] == "workspace" else c.tasks.project_view(parts[2])
            return self._send(200 if v is not None else 404, v)
        if method == "POST" and u.path == "/tasks":
            try:
                return self._send(200, {"id": c.tasks.add_task(**{k: v for k, v in obj.items() if k in ("project", "title", "description", "requires", "deps", "classification", "priority", "budget_s", "deadline", "needs_approval", "exclusive_group")})})
            except (ValueError, TypeError) as e:
                return self._send(400, {"error": str(e)})
        if method == "POST" and u.path.startswith("/tasks/"):
            tid, act = u.path.split("/")[2], u.path.split("/")[3]
            fn = {"approve": lambda: c.tasks.approve(tid), "accept": lambda: c.tasks.accept(tid), "reject": lambda: c.tasks.reject(tid, obj.get("feedback", "")), "cancel": lambda: c.tasks.cancel(tid)}.get(act)
            return self._send(200, {"ok": fn()}) if fn else self._send(404, {"error": "unknown action"})
        if method == "GET" and u.path.startswith("/tasks/"):
            v = c.tasks.task_view(u.path.split("/")[2])
            return self._send(200 if v else 404, v)
        if method == "GET" and u.path == "/requests":
            return self._send(200, c.pending_requests())
        if method == "POST" and u.path in ("/requests/approve", "/requests/deny"):
            res = c.decide_request(obj["request_id"], u.path.endswith("approve"), number=obj.get("number"), require_number=True)
            if res == "mismatch":
                return self._send(400, {"ok": False, "error": "number does not match"})
            return self._send(200, {"ok": res})
        if method == "POST" and u.path == "/brain/publish":
            try:
                return self._send(200, {"version": c.brain_publish(obj.get("files") or {}, obj.get("notes", ""))})
            except (brainpkg.BrainError, TypeError, ValueError) as e:
                return self._send(400, {"error": str(e)})
        if method == "GET" and u.path == "/brain":
            p = c._brain_pkg()
            return self._send(200, {"version": c.brain_latest_version(), "notes": (p or {}).get("manifest", {}).get("notes", ""), "files": sorted(((p or {}).get("files") or {}))})
        if method == "GET" and u.path == "/boards":
            return self._send(200, {"boards": c.boards.all_boards(), "held": c.boards.held()})
        if method == "POST" and u.path == "/boards":
            try:
                return self._send(200, {"id": c.boards.create(obj.get("title"), obj.get("members", []), obj.get("approval", True))})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if u.path.startswith("/boards/"):
            parts = u.path.split("/"); bid = parts[2]
            if method == "GET":
                return self._send(200, c.boards.read_all(bid, float(parse_qs(u.query).get("since", ["0"])[0])))
            act = parts[3] if len(parts) > 3 else ""
            if method == "POST" and act == "post":
                pid, st, why = c.boards.owner_post(bid, str(obj.get("author", "owner"))[:20], obj.get("body"))
                return self._send(200 if pid else 403, {"id": pid, "status": st, "note": why})
            if method == "POST" and act in ("approve", "reject"):
                return self._send(200, {"ok": c.boards.decide(obj.get("post"), act == "approve")})
            if method == "POST" and act == "approval":
                return self._send(200, {"ok": c.boards.set_approval(bid, obj.get("on", True))})
            if method == "POST" and act == "member":
                try:
                    c.boards.add_member(bid, obj.get("node_id")); return self._send(200, {"ok": True})
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if method == "POST" and act == "close":
                return self._send(200, {"ok": c.boards.close(bid)})
        if method == "GET" and u.path == "/exchange":
            return self._send(200, {"flows": c.exchange(), "ext_help": c.get_setting("ext_help") == "1"})
        if method == "POST" and u.path == "/settings":
            if "ext_help" in obj:
                c.set_setting("ext_help", "1" if obj["ext_help"] else "0")
            return self._send(200, {"ext_help": c.get_setting("ext_help") == "1"})
        if method == "GET" and u.path == "/ledger":
            return self._send(200, c.ledger(float(parse_qs(u.query).get("since", ["86400"])[0])))
        if method == "GET" and u.path == "/audit":
            return self._send(200, c.store.q("SELECT ts,event,detail FROM audit ORDER BY id DESC LIMIT 100"))
        if method == "POST" and u.path == "/pairing":
            ttl = float(obj.get("ttl_s", CODE_TTL_S))
            code = c.new_pairing_code(obj.get("node_type", "compute"), str(obj.get("owner", "owner"))[:30], bool(obj.get("lan", True)), ttl)
            return self._send(200, {"code": code, "expires_in_s": max(60, min(3600, ttl)), "fingerprint": c.fp})
        if method == "POST" and u.path == "/approve":
            return self._send(200, {"ok": c.approve(obj["node_id"])})
        if method == "POST" and u.path in ("/revoke", "/pause", "/resume"):
            st = {"/revoke": "revoked", "/pause": "paused", "/resume": "approved"}[u.path]
            return self._send(200, {"ok": c.set_status(obj["node_id"], st)})
        if method == "POST" and u.path == "/jobs":
            return self._send(200, {"id": c.submit_job(obj["type"], obj.get("payload", {}), obj.get("project", "default"), obj.get("requires"), ext_ok=obj.get("allow_external"))})
        self._send(404, {"error": "not found"})


def main():
    d = os.environ.get("ECHO_MESH_DIR", os.path.expanduser("~/.echo-mesh"))
    c = Coordinator(d).start()
    print(f"Echo Mesh coordinator up. nodes:{c.ports[0]} admin(127.0.0.1):{c.ports[1]} fingerprint:{c.fp[:16]}…", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        c.shutdown()


if __name__ == "__main__":
    main()
