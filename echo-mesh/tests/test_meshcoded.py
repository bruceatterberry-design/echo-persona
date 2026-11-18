"""meshcoded: the service behind the plugin's Coder screen. Uses a fake runner, so no Claude and no Mesh are needed."""
import json, os, sys, tempfile, threading, time, unittest, urllib.request, urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import meshcoded as D

HOME = os.path.expanduser("~")


class T(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.proj = tempfile.mkdtemp(prefix="mcd-", dir=HOME)           # must live under the home folder
        open(f"{self.proj}/sol.py", "w").write("OLD\n"); open(f"{self.proj}/.env", "w").write("K=1\n")
        self.gate = threading.Event()
        def fake(a, root, log, cancel):
            log("hello from the runner")
            if getattr(self, "hold", False):
                self.gate.wait(5)
            if cancel.is_set():
                return {"ok": False, "note": "Cancelled.", "files": {}}
            return {"ok": True, "used": "mesh", "note": "solved", "files": {"sol.py": "NEW\n"}, "diff": "-OLD\n+NEW\n", "speed": {"winner": "mesh", "wall_s": 1.0}}
        self.hold = False
        self.srv, self.coder = D.serve(port=0, state=self.state.name, token="tok", runner=fake)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.gate.set(); self.srv.shutdown(); self.state.cleanup()
        import shutil; shutil.rmtree(self.proj, ignore_errors=True)

    def call(self, method, path, body=None, token="tok"):
        req = urllib.request.Request(self.base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait_done(self, rid):
        for _ in range(100):
            st, d = self.call("GET", "/runs/" + rid)
            if d["status"] != "running":
                return d
            time.sleep(0.05)
        self.fail("run did not finish")

    def test_token_is_required(self):
        self.assertEqual(self.call("GET", "/status", token="nope")[0], 401)
        self.assertEqual(self.call("GET", "/status")[0], 200)

    def test_projects_must_be_real_folders_inside_home(self):
        self.assertEqual(self.call("POST", "/projects", {"path": "/etc"})[0], 400)
        self.assertEqual(self.call("POST", "/projects", {"path": HOME})[0], 400)
        self.assertEqual(self.call("POST", "/projects", {"path": os.path.join(HOME, ".ssh")})[0], 400)
        self.assertEqual(self.call("POST", "/projects", {"path": self.proj, "tests": "a\nb"})[0], 400)
        st, d = self.call("POST", "/projects", {"path": self.proj, "tests": "pytest -q"})
        self.assertEqual(st, 200)
        files = [f["path"] for f in self.call("GET", f"/projects/{d['id']}/files")[1]]
        self.assertIn("sol.py", files); self.assertNotIn(".env", files)          # secret-looking files are never even listed

    def test_a_run_needs_a_project_a_task_and_safe_files(self):
        pid = self.call("POST", "/projects", {"path": self.proj})[1]["id"]
        for body in ({"project": "c-nope", "task": "x", "files": ["sol.py"]}, {"project": pid, "task": "", "files": ["sol.py"]},
                     {"project": pid, "task": "x", "files": [".env"]}, {"project": pid, "task": "x", "files": ["../x.py"]},
                     {"project": pid, "task": "x", "files": ["nope.py"]}, {"project": pid, "task": "x", "files": []}, {"project": pid, "task": "x", "files": ["sol.py"], "backend": "evil"}):
            self.assertEqual(self.call("POST", "/runs", body)[0], 400, body)

    def test_run_apply_and_the_changed_since_guard(self):
        pid = self.call("POST", "/projects", {"path": self.proj})[1]["id"]
        rid = self.call("POST", "/runs", {"project": pid, "task": "do it", "files": ["sol.py"]})[1]["id"]
        d = self.wait_done(rid)
        self.assertEqual((d["status"], d["used"], d["files"]), ("done", "mesh", ["sol.py"]))
        self.assertIn("hello from the runner", " ".join(d["log"])); self.assertIn("+NEW", d["diff"])
        self.assertEqual(open(f"{self.proj}/sol.py").read(), "OLD\n")             # nothing written until apply
        open(f"{self.proj}/sol.py", "w").write("EDITED BY OWNER\n")
        st, e = self.call("POST", f"/runs/{rid}/apply")
        self.assertEqual(st, 409); self.assertIn("changed since", e["error"])      # never overwrite work done in between
        open(f"{self.proj}/sol.py", "w").write("OLD\n")
        self.assertEqual(self.call("POST", f"/runs/{rid}/apply")[1]["written"], ["sol.py"])
        self.assertEqual(open(f"{self.proj}/sol.py").read(), "NEW\n")
        self.assertEqual(self.call("POST", f"/runs/{rid}/apply")[0], 400)           # only once

    def test_one_run_at_a_time_and_cancel(self):
        pid = self.call("POST", "/projects", {"path": self.proj})[1]["id"]
        self.hold = True
        rid = self.call("POST", "/runs", {"project": pid, "task": "a", "files": ["sol.py"]})[1]["id"]
        st, e = self.call("POST", "/runs", {"project": pid, "task": "b", "files": ["sol.py"]})
        self.assertEqual(st, 409); self.assertIn("still going", e["error"])
        self.assertTrue(self.call("POST", f"/runs/{rid}/cancel")[1]["ok"]); self.gate.set()
        self.assertEqual(self.wait_done(rid)["status"], "cancelled")
        self.assertEqual(self.call("POST", f"/runs/{rid}/apply")[0], 400)

    def test_discard_and_history_survive_a_restart(self):
        pid = self.call("POST", "/projects", {"path": self.proj})[1]["id"]
        rid = self.call("POST", "/runs", {"project": pid, "task": "keep me", "files": ["sol.py"]})[1]["id"]
        self.wait_done(rid)
        self.assertTrue(self.call("POST", f"/runs/{rid}/discard")[1]["ok"])
        self.assertEqual(self.call("GET", "/runs/" + rid)[1]["status"], "discarded")
        again = D.Coder(self.state.name)
        self.assertIn(rid, again.runs); self.assertIn(pid, again.projects)


if __name__ == "__main__":
    unittest.main()
