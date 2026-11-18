"""Echo Mesh node agent. Runs on every participating machine. Connects OUT to the coordinator.
Runs only whitelisted job types (ping, generate). It never executes shell commands from the network."""
import argparse, collections, json, os, platform, socket, sys, threading, time, urllib.request
from . import COORD_PORT, BEACON_PORT
from .common import Client, PinError, confirm_number, ROLES, private_dir
from . import inventory
from .governor import Governor
from . import policy as policy_mod
from .brainlet import Brainlet, ALLOWED_JOBS
from . import brainpkg, cognition
from .boardbot import BoardBot



def _directed_broadcasts():
    """x.y.z.255 for each network this computer is on (a /24 guess: good for home networks)."""
    from .common import local_ipv4s
    return sorted({".".join(ip.split(".")[:3]) + ".255" for ip in local_ipv4s()})


def hint_hosts(cfg_dir=None):
    """Addresses the owner or an earlier visit told us about: ECHOMESH_HINTS (comma list) and known-hosts.txt in the agent folder. Another network is reachable only this way."""
    out = [h.strip() for h in os.environ.get("ECHOMESH_HINTS", "").split(",") if h.strip()]
    for d in [cfg_dir, os.path.expanduser("~/.echo-mesh-agent")]:
        try:
            out += [l.strip() for l in open(os.path.join(d, "known-hosts.txt")) if l.strip() and not l.startswith("#")]
        except (OSError, TypeError):
            pass
    return list(dict.fromkeys(out))


def discover_info(timeout=2.0, port=BEACON_PORT, target="255.255.255.255", hints=None):
    """Find the Mesh. Asks the whole-network broadcast, each directly attached network, and every known address (the way across to another network), all at once.
    -> {"host", "port", "fingerprint_prefix", "addrs"} or None."""
    targets = [target]
    if target == "255.255.255.255":
        targets += _directed_broadcasts() + list(hints if hints is not None else hint_hosts())
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)
    try:
        for t in dict.fromkeys(targets):
            try:
                s.sendto(b"ECHOMESH?", (t, port))
            except OSError:
                pass
        data, addr = s.recvfrom(1024)
        info = json.loads(data)
        return {"host": addr[0], "port": info["port"], "fingerprint_prefix": info.get("fingerprint_prefix"), "addrs": [a for a in info.get("addrs", []) if isinstance(a, str)][:6]}
    except (socket.timeout, OSError, ValueError, KeyError):
        return None
    finally:
        s.close()


def discover(timeout=2.0, port=BEACON_PORT, target="255.255.255.255", hints=None):
    i = discover_info(timeout, port, target, hints)
    return (i["host"], i["port"], i["fingerprint_prefix"]) if i else None


class Agent:
    def __init__(self, cfg_dir, ollama_host="http://127.0.0.1:11434"):
        self.dir = cfg_dir
        private_dir(cfg_dir)
        self.path = os.path.join(cfg_dir, "identity.json")
        self.ollama_host = ollama_host
        self.stop = threading.Event()
        try:
            self.ident = json.load(open(self.path))
        except FileNotFoundError:
            self.ident = None
        except PermissionError:       # os.path.exists() would say False here and the agent would claim it was never enrolled
            raise RuntimeError(f"This user may not open {self.path}. The folder was probably made from an administrator shell (owner: Administrators). "
                               "Give this user access to the folder (Windows: icacls <folder> /grant <user>:(OI)(CI)F /T), or run the agent as the user who enrolled it.")
        self._gov_seen = None
        self.running_job = None
        pol = self._policy()
        rt = pol.get("runtime") or {}
        self.kind = rt.get("kind", "ollama")                 # which kind of local AI the owner connected: ollama or openai (OpenAI-style)
        self.ollama_host = rt.get("host", ollama_host)
        self.events = collections.deque(maxlen=40)           # (time, text) for the dashboard's activity list
        self.jobs_done = self.jobs_failed = 0
        self.busy_s = 0.0
        self.models = []                                     # the models of the connected AI, as last reported
        self._was = None
        self.rtts = collections.deque(maxlen=40)             # measured round-trip times to the Mesh (ms), for the latency graph
        self.overview = None                                 # the Mesh's latest overview for the dashboard
        self.last_hb_ok = None                               # when the Mesh last answered a heartbeat
        self.hb_status = None                                # the last answer (200 ok, 403 waiting for approval/paused/revoked, None unreachable)
        self.balance = None                                  # what the Mesh says this owner has given and used (outside computers)
        self.local_limit = float(pol.get("share_limit_pct", 100))     # the owner's own cap (policy.json); the coordinator's cap also applies
        self.gov = Governor(share_limit_pct=self.local_limit, window_s=float(pol.get("window_s", 600)),
                            max_concurrent=int(pol.get("max_concurrent", 1)), busy_load=float(pol.get("busy_load", 0.8)))
        self.gov.paused = bool(pol.get("paused", False))
        self.bot = BoardBot(self)                            # the AI's seat at a message board (off unless the owner's policy.json turns it on)

    def _policy(self):
        try:
            return json.load(open(os.path.join(self.dir, "policy.json")))
        except (OSError, ValueError):
            return {}

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self.ident, f)
        os.chmod(self.path, 0o600)

    def enroll(self, host, port, code, name=None, pin=None):
        c = Client(host, port, pin=pin)
        st, r = c.request("POST", "/enroll", {"code": code, "name": name or socket.gethostname(),
                                              "inventory": self._inventory()}, signed=False)
        if st != 200:
            raise RuntimeError(f"enrollment refused ({st})")
        self.ident = {"host": host, "port": port, "pin": c.seen_fp, "node_id": r["node_id"], "secret": r["secret"],
                      "status": r["status"], "node_type": r["node_type"], "brain_key": r.get("brain_key")}
        self._save()
        return r

    def join_lan(self, host, port, name=None, on_number=None, timeout=600, poll=2.0):
        """Ask a coordinator on the home network to let this computer in. `on_number(n)` shows the 4-digit number the owner must
        see matching on the Mac. Returns the node info when approved; raises RuntimeError if denied, expired or timed out."""
        c = Client(host, port)
        st, r = c.request("POST", "/join-request", {"name": name or socket.gethostname(), "inventory": self._inventory()}, signed=False)
        if st != 200:
            raise RuntimeError("The Mesh did not accept a join request from here." if st == 403 else "The Mesh is busy; try again in a few minutes.")
        if on_number:
            on_number(confirm_number(c.seen_fp, r["request_id"]))
        end = time.time() + timeout
        while time.time() < end and not self.stop.is_set():
            st, d = c.request("GET", f"/join-request/{r['request_id']}?token={r['poll_token']}", signed=False)
            if st == 200 and d["status"] == "approved":
                self.ident = {"host": host, "port": port, "pin": c.seen_fp, "node_id": d["node_id"], "secret": d["secret"],
                              "status": "approved", "node_type": d["node_type"], "brain_key": d.get("brain_key")}
                self._save()
                return d
            if st != 200 or d["status"] in ("denied", "expired"):
                raise RuntimeError("The join request was " + (d["status"] if d else "refused") + ".")
            self.stop.wait(poll)
        raise RuntimeError("Timed out waiting for approval.")

    def client(self):
        i = self.ident
        return Client(i["host"], i["port"], pin=i["pin"], node_id=i["node_id"], secret=i["secret"])

    def machine_id(self):
        """This computer's id. ECHOMESH_SPLIT_ID is for tests only: several pretend computers on one real one, told apart by their own folder."""
        m = inventory.machine_id_cached()
        if os.environ.get("ECHOMESH_SPLIT_ID") and m:
            import hashlib
            m = hashlib.sha256((m + os.path.abspath(self.dir)).encode()).hexdigest()[:24]
        return m

    def heartbeat_once(self, full=False):
        body = {"load": {**inventory.load(), "governor": self.gov.report(), **({"rtt_ms": round(self.rtts[-1], 1)} if self.rtts else {})}}
        body["machine_id"] = self.machine_id()    # every heartbeat: lets the Mesh notice a copied install on another computer
        if full:
            body["inventory"] = self._inventory()
        t0 = time.time()
        try:
            st, r = self.client().request("POST", "/heartbeat", body, timeout=10)
            self.rtts.append((time.time() - t0) * 1000.0)
        except Exception:
            self.hb_status = None
            if self._was is not None:
                self._was = None
                self.note("Lost contact with the Mesh")
            raise
        self.hb_status = st
        if st == 200:
            self.last_hb_ok = time.time()
        if st != self._was:
            self._was = st
            self.note({200: "Connected to the Mesh", 403: "Waiting for the Mesh owner's approval"}.get(st, "Lost contact with the Mesh"))
        g = body["load"]["governor"]
        seen = (st, g.get("taking_work"), g.get("reason"), g.get("share_limit_pct"))
        if st == 200 and seen != self._gov_seen:         # only when something changed, so the log stays small
            self._gov_seen = seen
            self.log(f"Heartbeat ok: share limit {g.get('share_limit_pct')}%, using {g.get('used_pct')}%, " + ("taking work" if g.get("taking_work") else f"resting ({g.get('reason') or 'at its limit'})"))
        if st == 200 and r and r.get("share_limit") is not None:
            self.gov.set_limit(min(self.local_limit, float(r["share_limit"])))     # the stricter cap wins
            self.balance = r.get("owner_balance")
        return st, r

    def brainlet(self):
        """The policy engine + AI adapter for this computer (mesh/brainlet.py); built fresh so a changed AI address or policy applies at once."""
        return Brainlet(self._effective_policy, self.kind, self.ollama_host,
                        lambda: {"node": self.ident["node_id"], "host": socket.gethostname(), "os": platform.system()},
                        self.cognition_profile)

    def cognition_profile(self):
        """Echo cognition text: the live brain's cognition/*.md files where present, the built-in defaults otherwise."""
        try:
            b = self.brain_store()
            return cognition.load(b.read if b and b.version() else None)
        except Exception:
            return cognition.load()

    def _effective_policy(self):
        own, brain = self._policy(), self.brain_policy()
        eff = dict(own)
        if isinstance(brain.get("max_prompt_chars"), int):
            eff["max_prompt_chars"] = min(int(own.get("max_prompt_chars", brain["max_prompt_chars"])), brain["max_prompt_chars"])
        if isinstance(brain.get("allowed_models"), list) and not own.get("allowed_models"):
            eff["allowed_models"] = [str(m) for m in brain["allowed_models"]]
        return eff

    def execute(self, job):
        return self.brainlet().run(job)

    # ---- finding the Mesh again when its address changed or this computer moved to another network ----
    def rediscover(self, timeout=3.0, port=BEACON_PORT):
        """Ask again (broadcast, attached networks, known addresses incl. every address the Mesh once listed). Only a Mesh whose certificate matches the pinned one is accepted. -> True if the address was updated."""
        i = self.ident or {}
        hints = list(dict.fromkeys([i.get("host")] + list(i.get("alts") or []) + hint_hosts(self.dir)))
        found = discover_info(timeout=timeout, port=port, hints=[h for h in hints if h])
        if not found or not str(i.get("pin", "")).startswith(found.get("fingerprint_prefix") or "\0"):
            return False
        changed = found["host"] != i.get("host") or found["port"] != i.get("port")
        i.update(host=found["host"], port=found["port"], alts=list(dict.fromkeys(found["addrs"] + list(i.get("alts") or [])))[:8])
        self._save()
        if changed:
            self.note(f"Found the Mesh again at {found['host']}")
        return changed

    # ---- signed brain updates ----
    def brain_store(self):
        k = (self.ident or {}).get("brain_key")
        return brainpkg.BrainStore(os.path.join(self.dir, "brain"), k) if k else None

    def check_brain(self):
        """Ask the Mesh for a newer brain; verify, apply with a health check, roll back if it fails. -> message or None. Needs the owner's public key given at join."""
        bs = self.brain_store()
        if not bs:
            return None
        st, d = self.client().request("GET", f"/brain?have={bs.version()}", timeout=20)
        if st != 200 or "manifest" not in (d or {}):
            return None
        ok, msg = bs.apply(d, health=lambda p: isinstance(json.load(open(os.path.join(p, "policy.json"))), dict) if os.path.exists(os.path.join(p, "policy.json")) else True)
        self.note(("Brain updated: " if ok else "Brain update refused: ") + msg)
        return msg

    def brain_policy(self):
        """Defaults from the live brain. They can only make this computer MORE careful; the owner's own policy.json always wins."""
        try:
            b = self.brain_store()
            return json.loads(b.read("policy.json")) if b and b.version() else {}
        except Exception:
            return {}

    # ---- message boards (the same API the Mac plugin uses) ----
    def boards(self):
        st, d = self.client().request("GET", "/boards", timeout=10)
        return d if st == 200 else []

    def board_posts(self, bid, since=0):
        st, d = self.client().request("GET", f"/boards/{bid}?since={since}", timeout=10)
        return d if st == 200 else []

    def board_post(self, bid, body):
        st, d = self.client().request("POST", f"/boards/{bid}", {"body": body}, timeout=10)
        return st == 200, (d or {}).get("error", "")

    def refresh_overview(self):
        try:
            st, d = self.client().request("GET", "/overview", timeout=10)
            if st == 200:
                self.overview = d
        except Exception:
            pass
        return self.overview

    def note(self, text):
        self.events.append((time.strftime("%H:%M"), text))
        self.log(text)

    def log(self, text):
        """One timestamped line on standard output. The Windows launcher and the Linux service already send that to agent.log / the journal.
        Never raises: with no console (pythonw) there may be nowhere to write."""
        try:
            print(time.strftime("[%Y-%m-%d %H:%M:%S] ") + text, flush=True)
        except Exception:
            pass

    def _inventory(self):
        inv = inventory.collect(self.ollama_host, self.kind)
        self.models = list((inv.get("ollama") or {}).get("models") or [])
        inv["machine_id"] = self.machine_id()
        role = self._policy().get("role")
        if role in ROLES:
            inv["role"] = role                                   # what the installer asked for; the owner of the Mesh still decides (a join can only ever create a compute node)
        h = self._policy().get("handshake")
        if isinstance(h, dict):
            inv["ai_handshake"] = {"accepted": h.get("accepted"), "reply": str(h.get("reply", ""))[:120], "owner_confirmed": bool(h.get("owner_confirmed"))}
        return inv

    def set_local_limit(self, pct, outside=False, base=None):
        """The owner moves their own share limit (the Mesh's cap still applies on top). Outside computers can never go above 30%."""
        from . import winapp_core as W
        self.local_limit = W.clamp_limit(pct, outside)
        self.gov.set_limit(self.local_limit)
        return self.local_limit

    def state(self):
        """A plain picture of what this computer is doing, for the Windows status window."""
        g = self.gov.report()
        if self.hb_status is None and not self.last_hb_ok:
            conn = "connecting"
        elif self.hb_status == 200 or (self.last_hb_ok and time.time() - self.last_hb_ok < 40):
            conn = "connected"
        elif self.hb_status == 403:
            conn = "waiting"          # not approved yet, paused or removed by the Mesh owner
        else:
            conn = "unreachable"
        if self.running_job:
            now = "running a job for the Mesh"
        elif self.gov.paused:
            now = "paused by you"
        elif not g["taking_work"]:
            now = "resting: " + (g["reason"] or "at its limit")
        else:
            now = "waiting for work"
        return {"connection": conn, "now": now, "limit": g["share_limit_pct"], "used": g["used_pct"], "balance": self.balance,
                "paused": self.gov.paused, "running": bool(self.running_job), "jobs_done": self.jobs_done, "jobs_failed": self.jobs_failed,
                "busy_s": self.busy_s, "events": list(self.events),
                "ai": {"kind": self.kind, "host": self.ollama_host, "models": list(self.models)}}

    def run(self, hb_every=10):
        last_full = 0
        def hb():
            nonlocal last_full
            while not self.stop.wait(hb_every):
                try:
                    full = time.time() - last_full > 300
                    st, _ = self.heartbeat_once(full)
                    if full and st == 200:
                        last_full = time.time()
                        self.check_brain()
                except PinError:
                    print("coordinator certificate changed; refusing to talk to it", file=sys.stderr)
                except Exception:
                    fails = getattr(self, "_fails", 0) + 1
                    self._fails = fails
                    if fails >= 3 and fails % 3 == 0:          # unreachable for a while: maybe its address changed or we are on another network
                        try:
                            self.rediscover()
                        except Exception:
                            pass
                else:
                    self._fails = 0
        threading.Thread(target=hb, daemon=True).start()
        def ov():
            while not self.stop.wait(5):
                if self.hb_status == 200:
                    self.refresh_overview()
        threading.Thread(target=ov, daemon=True).start()
        def bb():
            while not self.stop.wait(20):
                if self.hb_status == 200:
                    try:
                        r = self.bot.step()
                        if r in ("posted", "draft waiting for the owner"):
                            self.note("Board: " + r)
                    except Exception:
                        pass
        threading.Thread(target=bb, daemon=True).start()
        try:
            self.heartbeat_once(True)
        except Exception:
            pass
        while not self.stop.is_set():
            try:
                may, why, wait = self.gov.may_take()
                if not may:                    # over the limit, owner busy, or paused: do not even ask for work
                    self.stop.wait(min(max(wait, 1.0), 10.0))
                    continue
                st, job = self.client().request("GET", "/jobs/next?wait=20", timeout=40)
                if st == 200 and job:
                    self.running_job = job["id"]
                    tok = self.gov.start()
                    t0 = time.time()
                    self.note("Started a job for the Mesh")
                    try:
                        ok, res, err = self.execute(job)
                    except Exception as e:
                        ok, res, err = False, None, f"{type(e).__name__}: {e}"
                    finally:
                        self.gov.end(tok)
                    self.running_job = None
                    dt = time.time() - t0
                    if ok:
                        self.jobs_done += 1; self.busy_s += dt
                        self.note(f"Finished a job ({dt:.0f} s)")
                    else:
                        self.jobs_failed += 1
                        self.note("A job could not be done")
                    self.client().request("POST", f"/jobs/{job['id']}/result", {"ok": ok, "result": res, "error": err})
                elif st in (401, 403):
                    self.stop.wait(10)   # pending approval, paused or revoked: wait, do nothing
            except PinError:
                print("coordinator certificate changed; refusing to talk to it", file=sys.stderr)
                self.stop.wait(30)
            except Exception:
                self.stop.wait(5)      # coordinator unreachable: retry


def main():
    ap = argparse.ArgumentParser(prog="python -m mesh.agent")
    ap.add_argument("cmd", choices=["enroll", "join", "run", "status"])
    ap.add_argument("--host"); ap.add_argument("--port", type=int, default=COORD_PORT)
    ap.add_argument("--code"); ap.add_argument("--name")
    ap.add_argument("--dir", default=os.path.expanduser("~/.echo-mesh-agent"))
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    a = ap.parse_args()
    ag = Agent(a.dir, a.ollama)
    if a.cmd == "join":
        host, port = a.host, a.port
        if not host:
            found = discover()
            if not found:
                sys.exit("No Echo Mesh found on this network. Is the Mac on and on the same network?")
            host, port, _ = found
        print(f"Found the Mesh at {host}. Waiting for approval on the Mac...")
        r = ag.join_lan(host, port, a.name, on_number=lambda n: print(f"Check that the Mac shows the number {n}, then approve."))
        print(f"Joined as {r['node_id']} ({r['node_type']}).")
    elif a.cmd == "enroll":
        host, port = a.host, a.port
        if not host:
            found = discover()
            if not found:
                sys.exit("No Echo Mesh coordinator found on this network. Pass --host <address>.")
            host, port, _ = found
            print(f"Found a coordinator at {host}:{port}")
        r = ag.enroll(host, port, a.code, a.name)
        print(f"Enrolled as {r['node_id']} ({r['node_type']}), status: {r['status']}.")
        print(f"Coordinator fingerprint (pinned): {ag.ident['pin'][:16]}…")
    elif a.cmd == "run":
        if not ag.ident:
            sys.exit("Not enrolled yet. Run: enroll --code <4-digit code>")
        print("Echo Mesh agent running. Ctrl+C to stop.")
        try:
            ag.run()
        except KeyboardInterrupt:
            ag.stop.set()
    else:
        print(json.dumps({k: v for k, v in (ag.ident or {}).items() if k != "secret"}, indent=1))


if __name__ == "__main__":
    main()
