"""Agent folder permissions on Windows, a readable error when the identity can't be opened, and the agent's own log lines (2026-11-18)."""
import contextlib, io, json, os, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh import common
from mesh.agent import Agent


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_private_dir_uses_700_off_windows_and_no_mode_on_windows(self):
        with mock.patch.object(common.os, "makedirs") as mk:
            with mock.patch.object(common, "IS_WINDOWS", False):
                common.private_dir("/x")
            mk.assert_called_with("/x", mode=0o700, exist_ok=True)
            with mock.patch.object(common, "IS_WINDOWS", True):
                common.private_dir("C:/x")
            mk.assert_called_with("C:/x", exist_ok=True)               # no mode: Python 3.12 would lock the normal user out

    def test_private_dir_really_makes_the_folder(self):
        p = os.path.join(self.tmp.name, "a", "b")
        common.private_dir(p); common.private_dir(p)                   # twice is fine
        self.assertTrue(os.path.isdir(p))
        if os.name != "nt":
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o700)

    def test_unreadable_identity_says_so_instead_of_not_enrolled(self):
        d = os.path.join(self.tmp.name, "ag"); os.makedirs(d)
        open(os.path.join(d, "identity.json"), "w").write("{}")
        real = open
        def deny(path, *a, **k):
            if str(path).endswith("identity.json"):
                raise PermissionError(13, "denied")
            return real(path, *a, **k)
        with mock.patch("builtins.open", deny):
            with self.assertRaises(RuntimeError) as cm:
                Agent(d)
        self.assertIn("administrator", str(cm.exception)); self.assertIn("icacls", str(cm.exception))

    def test_no_identity_is_still_just_not_enrolled(self):
        self.assertIsNone(Agent(os.path.join(self.tmp.name, "new")).ident)

    def test_note_writes_a_timestamped_line_and_never_raises(self):
        a = Agent(os.path.join(self.tmp.name, "n"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            a.note("Connected to the Mesh")
        self.assertRegex(buf.getvalue(), r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] Connected to the Mesh\n$")
        self.assertEqual(a.events[-1][1], "Connected to the Mesh")      # the dashboard list is unchanged
        with mock.patch("builtins.print", side_effect=ValueError("closed")):
            a.note("still fine")                                       # no console (pythonw) must not break the agent

    def test_heartbeat_logs_the_governor_only_when_it_changes(self):
        a = Agent(os.path.join(self.tmp.name, "h"))
        a.ident = {"node_id": "n-x", "host": "127.0.0.1", "port": 1, "secret": "00" * 16, "pin": "p"}
        class C:
            def request(self, *a, **k):
                return 200, {"ok": True, "share_limit": 100.0}
        a.client = lambda: C()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            a.heartbeat_once(); a.heartbeat_once(); a.heartbeat_once()
            a.gov.paused = True
            a.heartbeat_once()
        lines = [l for l in buf.getvalue().splitlines() if "Heartbeat ok" in l]
        self.assertEqual(len(lines), 2, buf.getvalue())                # first success, then the pause; the two repeats say nothing
        self.assertIn("taking work", lines[0]); self.assertIn("resting", lines[1])
        self.assertEqual(buf.getvalue().count("Connected to the Mesh"), 1)
        self.assertNotIn("00" * 16, buf.getvalue())                     # never the device secret


if __name__ == "__main__":
    unittest.main()
