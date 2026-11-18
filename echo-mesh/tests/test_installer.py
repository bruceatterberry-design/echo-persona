"""Runs installers/arch/install.sh in a sandbox: a throw-away home folder, a fake systemctl, and a throw-away coordinator."""
import json, os, stat, subprocess, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh.coordinator import Coordinator

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(ROOT, "installers", "arch", "install.sh")


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name + "/home"; os.makedirs(self.home)
        self.bin = self.tmp.name + "/bin"; os.makedirs(self.bin)
        self.log = self.tmp.name + "/calls.log"
        for name in ("systemctl", "loginctl"):     # record instead of touching the real system
            p = f"{self.bin}/{name}"
            open(p, "w").write(f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\n[ "$2" = "is-active" ] && echo active\n[ "$1 $2" = "--user is-active" ] && echo active\nexit 0\n')
            os.chmod(p, 0o755)
        self.env = {**os.environ, "HOME": self.home, "PATH": self.bin + ":" + os.environ["PATH"], "USER": "tester", "ECHOMESH_NO_MODEL_CHECK": "1"}
        self.c = Coordinator(self.tmp.name + "/c", coord_port=0, admin_port=0, beacon_port=0, bind="127.0.0.1").start()

    def tearDown(self):
        self.c.shutdown(); self.tmp.cleanup()

    def calls(self):
        return open(self.log).read() if os.path.exists(self.log) else ""

    def test_automatic_join_then_service_is_installed(self):
        p = subprocess.Popen(["sh", SCRIPT, "--accept-terms", "--role", "worker", "--host", "127.0.0.1", "--port", str(self.c.ports[0])], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        end = time.time() + 30
        while not self.c.pending_requests() and time.time() < end: time.sleep(0.2)
        req = self.c.pending_requests()[0]
        self.assertTrue(self.c.decide_request(req["id"], True))
        out, _ = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, out)
        self.assertIn(req["confirm"], out)                                              # the number it showed matches the Mac's
        self.assertIn("Echo Mesh agent: active", out)
        d = self.home + "/.local/share/echo-mesh/mesh"
        self.assertEqual(sorted(f for f in os.listdir(d) if f != "__pycache__"), sorted(["__init__.py", "common.py", "inventory.py", "governor.py", "policy.py", "brainlet.py", "cognition.py", "speedup.py", "brainpkg.py", "ed25519.py", "boardbot.py", "agent.py", "agreement.py", "cli_setup.py", "winapp_core.py"]))   # only the agent, not the coordinator
        ident = json.load(open(self.home + "/.echo-mesh-agent/identity.json"))
        self.assertEqual(ident["node_type"], "compute")
        unit = open(self.home + "/.config/systemd/user/echomesh-agent.service").read()
        self.assertIn("-m mesh.agent run --dir " + self.home + "/.echo-mesh-agent", unit); self.assertIn("Restart=always", unit)
        self.assertIn("--user enable --now echomesh-agent", self.calls())
        # running it again updates the files, keeps the identity, and does not ask to join again
        out2 = subprocess.run(["sh", SCRIPT], env=self.env, capture_output=True, text=True, timeout=30).stdout
        self.assertIn("Already joined", out2); self.assertEqual(json.load(open(self.home + "/.echo-mesh-agent/identity.json"))["node_id"], ident["node_id"])

    def test_autostart_no_installs_the_service_but_does_not_enable_it_at_boot(self):
        code = self.c.new_pairing_code("compute", lan=True)
        r = subprocess.run(["sh", SCRIPT, "--accept-terms", "--autostart", "no", "--host", "127.0.0.1", "--port", str(self.c.ports[0]), "--code", code], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(json.load(open(self.home + "/.echo-mesh-agent/policy.json"))["autostart"])
        calls = self.calls()
        self.assertIn("--user start echomesh-agent", calls); self.assertNotIn("enable --now echomesh-agent", calls); self.assertNotIn("enable-linger", calls)
        self.assertIn("Not set to start at boot", r.stdout)

    def test_role_is_saved_and_declining_the_terms_installs_nothing(self):
        code = self.c.new_pairing_code("compute", lan=True)
        r = subprocess.run(["sh", SCRIPT, "--accept-terms", "--role", "peer", "--host", "127.0.0.1", "--port", str(self.c.ports[0]), "--code", code], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pol = json.load(open(self.home + "/.echo-mesh-agent/policy.json")); self.assertEqual(pol["role"], "peer"); self.assertIn("terms_accepted", pol)
        home2 = self.tmp.name + "/home2"; os.makedirs(home2)
        r2 = subprocess.run(["sh", SCRIPT], env={**self.env, "HOME": home2}, input="no\n", capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r2.returncode, 0); self.assertIn("declined", r2.stdout.lower())
        self.assertFalse(os.path.exists(home2 + "/.echo-mesh-agent/identity.json")); self.assertFalse(os.path.exists(home2 + "/.local/share/echo-mesh"))

    def test_pairing_code_route_and_uninstall(self):
        code = self.c.new_pairing_code("compute", lan=True)
        r = subprocess.run(["sh", SCRIPT, "--accept-terms", "--host", "127.0.0.1", "--port", str(self.c.ports[0]), "--code", code], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self.home + "/.echo-mesh-agent/identity.json"))
        u = subprocess.run(["sh", SCRIPT, "--uninstall"], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(u.returncode, 0); self.assertIn("removed", u.stdout)
        for gone in (self.home + "/.local/share/echo-mesh", self.home + "/.echo-mesh-agent", self.home + "/.config/systemd/user/echomesh-agent.service"):
            self.assertFalse(os.path.exists(gone), gone)
        self.assertIn("disable --now echomesh-agent", self.calls())

    def test_a_wrong_code_stops_the_install_with_nothing_left_running(self):
        r = subprocess.run(["sh", SCRIPT, "--accept-terms", "--host", "127.0.0.1", "--port", str(self.c.ports[0]), "--code", "0000"], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.home + "/.config/systemd/user/echomesh-agent.service"))
        self.assertNotIn("enable --now", self.calls())


if __name__ == "__main__":
    unittest.main()
