"""The limited client token (for Echo's background offload): it can hand over text jobs and read its own answers, nothing else (2026-11-18)."""
import json, os, sys, tempfile, unittest, urllib.request, urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh.coordinator import Coordinator


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Coordinator(self.tmp.name + "/c", coord_port=0, admin_port=0, beacon_port=0, bind="127.0.0.1").start()
        self.base = f"http://127.0.0.1:{self.c.ports[1]}"
        code = self.c.new_pairing_code("compute")
        self.n = self.c.enroll(code, "w1", {"ollama": {"models": ["m1"]}})
        import time; self.c.store.x("UPDATE nodes SET last_seen=? WHERE id=?", (time.time(), self.n["node_id"]))

    def tearDown(self):
        self.c.shutdown(); self.tmp.cleanup()

    def call(self, method, path, body=None, tok=None):
        req = urllib.request.Request(self.base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + (tok or self.c.client_token), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_token_file_is_private_and_different_from_admin(self):
        p = os.path.join(self.c.dir, "client.token")
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600); self.assertNotEqual(self.c.client_token, self.c.admin_token)
        self.assertEqual(self.call("GET", "/status", tok="wrong")[0], 401)

    def test_it_can_see_models_and_send_a_generate_job_and_read_it_back(self):
        st, d = self.call("GET", "/offload/status")
        self.assertEqual((st, d), (200, {"online": 1, "models": {"m1": 1}}))
        st, d = self.call("POST", "/jobs", {"type": "generate", "payload": {"model": "m1", "prompt": "hi", "think": False, "system": "x", "format": {}}, "allow_external": True, "project": "evil"})
        self.assertEqual(st, 200)
        row = self.c.store.one("SELECT * FROM jobs WHERE id=?", (d["id"],))
        self.assertEqual((row["project"], row["ext_ok"], row["type"]), ("echo-offload", 0, "generate"))       # project fixed, never outside
        self.assertEqual(json.loads(row["payload"]), {"model": "m1", "prompt": "hi", "think": False})          # extra keys dropped
        self.assertEqual(json.loads(row["requires"]), {"model": "m1"})
        self.assertEqual(self.call("GET", "/jobs/" + d["id"])[1]["status"], "queued")

    def test_it_cannot_do_anything_else(self):
        for method, path, body in (("GET", "/status", None), ("GET", "/audit", None), ("GET", "/boards", None), ("POST", "/pause", {"node_id": self.n["node_id"]}),
                                   ("POST", "/revoke", {"node_id": self.n["node_id"]}), ("POST", "/pairing", {}), ("POST", "/settings", {"ext_help": True}), ("GET", "/projects", None)):
            self.assertEqual(self.call(method, path, body)[0], 403, path)
        self.assertEqual(self.call("POST", "/jobs", {"type": "ping", "payload": {}})[0], 403)
        self.assertEqual(self.c.store.one("SELECT status FROM nodes")["status"], "approved")

    def test_it_sees_only_its_own_jobs_and_is_capped(self):
        other = self.c.submit_job("generate", {"model": "m1", "prompt": "secret admin job"}, "mine")
        self.assertEqual(self.call("GET", "/jobs/" + other)[0], 404)
        for _ in range(30):
            self.assertEqual(self.call("POST", "/jobs", {"type": "generate", "payload": {"model": "m1", "prompt": "x"}})[0], 200)
        self.assertEqual(self.call("POST", "/jobs", {"type": "generate", "payload": {"model": "m1", "prompt": "x"}})[0], 429)
        self.assertEqual(self.call("POST", "/jobs", {"type": "generate", "payload": {"model": "m1", "prompt": "x" * 30000}}, )[0], 400)

    def test_the_admin_token_still_works_everywhere(self):
        self.assertEqual(self.call("GET", "/status", tok=self.c.admin_token)[0], 200)


if __name__ == "__main__":
    unittest.main()
