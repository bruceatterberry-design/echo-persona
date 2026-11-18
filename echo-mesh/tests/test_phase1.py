import json, os, sys, tempfile, threading, time, unittest, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh.coordinator import Coordinator
from mesh.agent import Agent, discover
from mesh.common import Client, PinError


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _j(self, o):
        d = json.dumps(o).encode(); self.send_response(200); self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)
    def do_GET(self):
        self._j({"models": [{"name": "fake:1b"}]} if self.path == "/api/tags" else {"version": "0.0-fake"})
    def do_POST(self):
        n = int(self.headers["Content-Length"]); b = json.loads(self.rfile.read(n))
        self._j({"response": "echo:" + b["prompt"], "eval_count": 3, "eval_duration": 1000})


def admin(c, method, path, obj=None):
    r = urllib.request.Request(f"http://127.0.0.1:{c.ports[1]}{path}", method=method,
                               data=None if obj is None else json.dumps(obj).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + c.admin_token})
    with urllib.request.urlopen(r, timeout=10) as f:
        return json.load(f)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Coordinator(self.tmp.name + "/c", coord_port=0, admin_port=0, beacon_port=0, bind="127.0.0.1").start()
        time.sleep(0.2)
        self.oll = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=self.oll.serve_forever, daemon=True).start()
        self.ollama = f"http://127.0.0.1:{self.oll.server_address[1]}"
        self.n = 0

    def tearDown(self):
        self.c.shutdown(); self.oll.shutdown(); self.tmp.cleanup()

    def agent(self, node_type="compute", lan=True):
        self.n += 1
        a = Agent(f"{self.tmp.name}/a{self.n}", self.ollama)
        code = self.c.new_pairing_code(node_type, lan=lan)
        a.enroll("127.0.0.1", self.c.ports[0], code, f"node{self.n}")
        return a

    def start(self, a):
        threading.Thread(target=a.run, kwargs={"hb_every": 1}, daemon=True).start()

    def wait_job(self, jid, secs=15):
        end = time.time() + secs
        while time.time() < end:
            j = admin(self.c, "GET", f"/jobs/{jid}")
            if j["status"] in ("done", "failed"):
                return j
            time.sleep(0.2)
        self.fail("job did not finish")


class TestEnrollment(Base):
    def test_wrong_code_then_lockout(self):
        code = self.c.new_pairing_code()
        wrong = "0000" if code != "0000" else "1111"
        a = Agent(self.tmp.name + "/x", self.ollama)
        for _ in range(5):
            with self.assertRaises(RuntimeError):
                a.enroll("127.0.0.1", self.c.ports[0], wrong)
        with self.assertRaises(RuntimeError):      # the right code is burned after 5 failures
            a.enroll("127.0.0.1", self.c.ports[0], code)

    def test_expired_and_single_use(self):
        code = self.c.new_pairing_code(ttl=-1)
        a = Agent(self.tmp.name + "/x", self.ollama)
        with self.assertRaises(RuntimeError):
            a.enroll("127.0.0.1", self.c.ports[0], code)
        code = self.c.new_pairing_code()
        a.enroll("127.0.0.1", self.c.ports[0], code)
        with self.assertRaises(RuntimeError):
            Agent(self.tmp.name + "/y", self.ollama).enroll("127.0.0.1", self.c.ports[0], code)

    def test_types_never_escalate(self):
        a = self.agent("compute")
        self.assertEqual(a.ident["node_type"], "compute")
        b = self.agent("brain")
        self.assertEqual(b.ident["node_type"], "brain")

    def test_external_needs_approval(self):
        a = self.agent(lan=False)
        self.assertEqual(a.ident["status"], "pending")
        st, _ = a.heartbeat_once()
        self.assertEqual(st, 403)
        admin(self.c, "POST", "/approve", {"node_id": a.ident["node_id"]})
        st, _ = a.heartbeat_once()
        self.assertEqual(st, 200)

    def test_discovery(self):
        got = discover(target="127.0.0.1", port=self.c.ports[2])
        self.assertIsNotNone(got)
        self.assertEqual(got[1], self.c.ports[0])


class TestSecurity(Base):
    def test_unsigned_and_bad_signature_rejected(self):
        a = self.agent()
        cl = Client("127.0.0.1", self.c.ports[0], pin=a.ident["pin"], node_id=a.ident["node_id"], secret="00" * 32)
        st, _ = cl.request("POST", "/heartbeat", {})
        self.assertEqual(st, 401)
        st, _ = Client("127.0.0.1", self.c.ports[0], pin=a.ident["pin"]).request("GET", "/status", signed=False)
        self.assertEqual(st, 401)

    def test_replay_rejected(self):
        a = self.agent()
        cl = a.client()
        import http.client, ssl, secrets
        from mesh.common import sign
        ts, nonce, body = str(int(time.time())), secrets.token_hex(8), b"{}"
        hdr = {"X-Mesh-Node": a.ident["node_id"], "X-Mesh-Ts": ts, "X-Mesh-Nonce": nonce,
               "X-Mesh-Sig": sign(a.ident["secret"], "POST", "/heartbeat", ts, nonce, body)}
        codes = []
        for _ in range(2):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection("127.0.0.1", self.c.ports[0], context=ctx)
            conn.request("POST", "/heartbeat", body=body, headers=hdr); codes.append(conn.getresponse().status); conn.close()
        self.assertEqual(codes, [200, 401])

    def test_pin_mismatch_refused(self):
        a = self.agent()
        bad = Client("127.0.0.1", self.c.ports[0], pin="ab" * 32, node_id=a.ident["node_id"], secret=a.ident["secret"])
        with self.assertRaises(PinError):
            bad.request("POST", "/heartbeat", {})

    def test_admin_needs_token(self):
        import urllib.error
        for hdr in ({}, {"Authorization": "Bearer wrong"}):
            r = urllib.request.Request(f"http://127.0.0.1:{self.c.ports[1]}/pairing", method="POST", data=b"{}", headers=hdr)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(r, timeout=5)
            self.assertEqual(cm.exception.code, 401)
        self.assertEqual(self.c.store.q("SELECT COUNT(*) c FROM pairing")[0]["c"], 0)   # nothing was created

    def test_revoked_node_blocked(self):
        a = self.agent()
        self.assertEqual(a.heartbeat_once()[0], 200)
        admin(self.c, "POST", "/revoke", {"node_id": a.ident["node_id"]})
        self.assertEqual(a.heartbeat_once()[0], 403)

    def test_unsupported_job_type_refused(self):
        with self.assertRaises(ValueError):
            self.c.submit_job("shell", {"cmd": "ls"})
        a = self.agent()
        ok, _, err = a.execute({"type": "shell", "payload": {"cmd": "ls"}})
        self.assertFalse(ok)


class TestJobs(Base):
    def test_ledger_counts_measured_work_per_node(self):
        a, b = self.agent(), self.agent()
        a.heartbeat_once(True); b.heartbeat_once(True)
        jid = self.c.submit_job("ping", {})
        st, job = a.client().request("GET", "/jobs/next")
        time.sleep(0.3)
        a.client().request("POST", f"/jobs/{jid}/result", {"ok": True, "result": {}})
        led = {r["name"]: r for r in admin(self.c, "GET", "/ledger")}
        self.assertEqual(led["node1"]["jobs_done"], 1)
        self.assertGreaterEqual(led["node1"]["busy_seconds"], 0.3)
        self.assertEqual(led["node2"]["jobs_done"], 0)

    def test_heartbeat_shows_online_and_inventory(self):
        a = self.agent()
        a.heartbeat_once(True)
        n = admin(self.c, "GET", "/status")["nodes"][0]
        self.assertTrue(n["online"])
        self.assertIn("fake:1b", n["inventory"]["ollama"]["models"])

    def test_ping_job_round_trip(self):
        a = self.agent(); self.start(a)
        j = self.wait_job(admin(self.c, "POST", "/jobs", {"type": "ping"})["id"])
        self.assertEqual(j["status"], "done")
        a.stop.set()

    def test_generate_job_needs_model_present(self):
        a = self.agent(); self.start(a)
        miss = admin(self.c, "POST", "/jobs", {"type": "generate", "payload": {"model": "nope", "prompt": "x"}, "requires": {"model": "nope"}})["id"]
        hit = admin(self.c, "POST", "/jobs", {"type": "generate", "payload": {"model": "fake:1b", "prompt": "hi"}, "requires": {"model": "fake:1b"}})["id"]
        j = self.wait_job(hit)
        self.assertEqual(json.loads(j["result"])["response"], "echo:hi")
        self.assertEqual(admin(self.c, "GET", f"/jobs/{miss}")["status"], "queued")   # nobody can run it: stays queued, not lost
        a.stop.set()

    def test_failover_node_dies_mid_job(self):
        a, b = self.agent(), self.agent()
        a.heartbeat_once(True); b.heartbeat_once(True)
        jid = admin(self.c, "POST", "/jobs", {"type": "ping", "requires": {}})["id"]
        st, job = a.client().request("GET", "/jobs/next")     # node A takes the job, then 'dies' without a result
        self.assertEqual(job["id"], jid)
        self.c.store.x("UPDATE nodes SET last_seen=? WHERE id=?", (time.time() - 100, a.ident["node_id"]))
        self.c.reap()
        self.assertEqual(admin(self.c, "GET", f"/jobs/{jid}")["status"], "queued")
        t0 = time.time(); self.start(b)
        j = self.wait_job(jid)
        self.assertEqual(j["status"], "done")
        self.assertEqual(j["node"], b.ident["node_id"])
        b.stop.set()

    def test_late_result_from_dead_node_ignored(self):
        a, b = self.agent(), self.agent()
        jid = admin(self.c, "POST", "/jobs", {"type": "ping"})["id"]
        a.client().request("GET", "/jobs/next")
        self.c.store.x("UPDATE nodes SET last_seen=? WHERE id=?", (time.time() - 100, a.ident["node_id"]))
        self.c.reap()
        b.client().request("GET", "/jobs/next")
        st, r = a.client().request("POST", f"/jobs/{jid}/result", {"ok": True, "result": {}})
        self.assertEqual(st, 409)


if __name__ == "__main__":
    unittest.main()
