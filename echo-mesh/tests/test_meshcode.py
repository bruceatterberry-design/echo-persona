"""meshcode: the parts that need no live Mesh or Claude (2026-11-18)."""
import os, subprocess, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import meshcode as M

TOOL = os.path.join(os.path.dirname(__file__), "..", "tools", "meshcode.py")


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = self.tmp.name
        open(f"{self.d}/sol.py", "w").write("def f():\n    raise NotImplementedError\n")
        open(f"{self.d}/test_sol.py", "w").write("from sol import f\ndef test_f():\n    assert f() == 1\n")
        open(f"{self.d}/.env", "w").write("TOKEN=abc\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_reply_needs_every_file_and_has_a_single_file_shortcut(self):
        two = "### FILE: a.py\n```python\nA\n```\n### FILE: b.py\n```\nB\n```"
        self.assertEqual(M.parse_reply(two, ["a.py", "b.py"]), {"a.py": "A\n", "b.py": "B\n"})
        self.assertEqual(M.parse_reply(two, ["a.py", "b.py", "c.py"]), {})             # a missing file = unusable answer
        self.assertEqual(M.parse_reply("sure!\n```python\nX\n```", ["only.py"]), {"only.py": "X\n"})
        self.assertEqual(M.parse_reply("no code here", ["only.py"]), {})

    def test_secret_looking_files_are_refused_before_anything_is_sent(self):
        r = subprocess.run([sys.executable, TOOL, "task", "--files", ".env", "--backend", "mesh"], cwd=self.d, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0); self.assertIn("looks like a secret", r.stdout + r.stderr)
        for name in (".env.local", "server.pem", "my_token.txt", "id_rsa", "credentials.json"):
            self.assertTrue(M.SECRET_NAMES.search(name), name)
        self.assertFalse(M.SECRET_NAMES.search("sol.py"))

    def test_the_model_is_shown_the_tests_but_never_the_secret_file(self):
        ctx = M.read_only_context(self.d, ["sol.py"], [".env"])
        self.assertIn("READ-ONLY FILE (do not change): test_sol.py", ctx)
        self.assertNotIn("TOKEN=abc", ctx); self.assertNotIn("### READ-ONLY FILE (do not change): sol.py", ctx)

    def test_changed_and_diff_only_report_real_changes(self):
        work = M.scratch_copy(self.d)
        self.assertEqual(M.changed(self.d, work), {})
        open(f"{work}/sol.py", "w").write("def f():\n    return 1\n")
        ch = M.changed(self.d, work)
        self.assertEqual(list(ch), ["sol.py"])
        self.assertIn("+    return 1", M.diff_text(self.d, ch))
        ok, _ = M.run_tests(f"{sys.executable} -c \"import sys; sys.path.insert(0,'.'); from sol import f; assert f()==1\"", work)
        self.assertTrue(ok)
        ok, out = M.run_tests(f"{sys.executable} -c \"import sys; sys.path.insert(0,'.'); from sol import f; f()\"", self.d)
        self.assertFalse(ok); self.assertIn("NotImplementedError", out)

    def test_claude_down_means_fallback_not_a_crash(self):
        class A: claude_cmd = "definitely-not-installed-xyz"; claude_timeout = 5
        ok, res, note, _ = M.try_claude("t", ["sol.py"], self.d, "true", A)
        self.assertFalse(ok); self.assertEqual(res, {}); self.assertIn("not installed", note)
        A.claude_cmd = "false"
        ok, _, note, _ = M.try_claude("t", ["sol.py"], self.d, "true", A)
        self.assertFalse(ok); self.assertIn("not available", note)

    def test_score_reads_the_summary_line_even_when_plugin_warnings_follow_it(self):
        out = "FAILED t::a\n3 failed, 2 passed in 0.02s\n\nwarnings.warn(PytestDeprecationWarning(...))\nThe event loop scope for asynchronous fixtures"
        self.assertEqual(M.score(False, out), 3)
        self.assertEqual(M.score(False, "1 failed, 3 passed, 2 errors in 0.5s"), 3)
        self.assertEqual(M.score(False, "boom"), 1); self.assertEqual(M.score(True, ""), 0)

    def test_fail_excerpt_drops_the_warnings_blob(self):
        out = "E   assert 1 == 2\nFAILED t.py::a\n1 failed in 0.1s\n\n=============== warnings summary ===============\nnoise " * 1
        self.assertEqual(M.fail_excerpt(out), "E   assert 1 == 2\nFAILED t.py::a\n1 failed in 0.1s")

    def test_race_first_passing_side_wins_and_the_other_is_stopped(self):
        import threading, time
        from unittest import mock
        seen = {}
        def slow_claude(task, files, root, tests, a, cancel=None):
            for _ in range(100):
                if cancel.is_set():
                    seen["claude_stopped"] = True; raise M.Cancelled()
                time.sleep(0.05)
            return True, {"x.py": "slow"}, "late", self.d
        def quick_mesh(a, root, log, cancel):
            return True, {"x.py": "fast"}, "mesh won"
        a = M.make_opts(task="t", files=["sol.py"], backend="race")
        with mock.patch.object(M, "try_claude", slow_claude), mock.patch.object(M, "mesh_job", quick_mesh):
            t0 = time.time(); res = M.run_task(a, self.d, lambda m: None)
        self.assertTrue(res["ok"]); self.assertEqual((res["used"], res["files"]), ("mesh", {"x.py": "fast"}))
        self.assertTrue(seen.get("claude_stopped")); self.assertLess(time.time() - t0, 4)       # did not wait for the slow side
        self.assertEqual(res["speed"]["winner"], "mesh"); self.assertEqual(res["speed"]["claude"]["state"], "stopped")

    def test_race_with_full_lets_both_finish_and_reports_who_was_faster(self):
        import time
        from unittest import mock
        def claude(task, files, root, tests, a, cancel=None):
            time.sleep(0.6); return True, {"x.py": "c"}, "claude done", self.d
        def mesh(a, root, log, cancel):
            return True, {"x.py": "m"}, "mesh done"
        a = M.make_opts(task="t", files=["sol.py"], backend="race", full=True)
        with mock.patch.object(M, "try_claude", claude), mock.patch.object(M, "mesh_job", mesh):
            res = M.run_task(a, self.d, lambda m: None)
        sp = res["speed"]
        self.assertEqual(sp["winner"], "mesh"); self.assertGreater(sp["saved_s"], 0.3)
        self.assertEqual((sp["claude"]["state"], sp["mesh"]["state"]), ("passed", "passed"))

    def test_race_survives_one_side_crashing_and_reports_when_both_fail(self):
        from unittest import mock
        def boom(*a, **k):
            raise RuntimeError("claude exploded")
        a = M.make_opts(task="t", files=["sol.py"], backend="race")
        with mock.patch.object(M, "try_claude", boom), mock.patch.object(M, "mesh_job", lambda *x: (True, {"x.py": "m"}, "mesh ok")):
            self.assertEqual(M.run_task(a, self.d, lambda m: None)["used"], "mesh")
        with mock.patch.object(M, "try_claude", boom), mock.patch.object(M, "mesh_job", lambda *x: (False, {}, "no luck")):
            res = M.run_task(a, self.d, lambda m: None)
        self.assertFalse(res["ok"]); self.assertIn("exploded", res["note"]); self.assertIn("no luck", res["note"])

    def test_a_runaway_test_is_killed_with_its_children(self):
        import time, subprocess
        t0 = time.time(); ok, out = M.run_tests("python3 -c 'while True: pass'", self.d, 1.5)
        self.assertFalse(ok); self.assertIn("endless loop", out); self.assertLess(time.time() - t0, 6)
        time.sleep(0.4)
        self.assertEqual(subprocess.run("pgrep -f 'while True: pass'", shell=True, capture_output=True, text=True).stdout.strip(), "")

    def test_a_failed_mesh_run_keeps_its_closest_attempt_as_a_partial_not_a_result(self):
        from unittest import mock
        a = M.make_opts(task="t", files=["sol.py"], backend="mesh")
        with mock.patch.object(M, "mesh_job", lambda *x: (False, {"sol.py": "def f():\n    return 0\n"}, "closest kept")):
            res = M.run_task(a, self.d, lambda m: None)
        self.assertFalse(res["ok"]); self.assertEqual(res["files"], {}); self.assertEqual(res["diff"], "")      # never applied by mistake
        self.assertIn("return 0", res["partial"]["diff"])


if __name__ == "__main__":
    unittest.main()
