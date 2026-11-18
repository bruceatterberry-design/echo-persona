import io, json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh import cli_setup as C


class Setup(unittest.TestCase):
    def setUp(self): self.d = tempfile.mkdtemp()

    def answers(self, *a):
        it = iter(a); return lambda prompt="": next(it)

    def test_agree_and_choose_worker_by_default(self):
        out = io.StringIO()
        self.assertTrue(C.run(self.d, ask=self.answers("i agree", "", ""), out=out, pause=0, check=False))
        pol = C.load(self.d); self.assertEqual(pol["role"], "worker"); self.assertEqual(pol["terms_accepted"]["version"], C.TERMS_VERSION)
        self.assertIn("Distributed AI. Infinite Possibilities.", out.getvalue()); self.assertIn("not liable", out.getvalue())

    def test_the_boot_question_defaults_to_yes_and_n_turns_it_off(self):
        C.run(self.d, ask=self.answers("I AGREE", "1", ""), out=io.StringIO(), pause=0, check=False)
        self.assertIs(C.load(self.d)["autostart"], True)
        d2 = tempfile.mkdtemp(); out = io.StringIO()
        C.run(d2, ask=self.answers("I AGREE", "1", "n"), out=out, pause=0, check=False)
        self.assertIs(C.load(d2)["autostart"], False); self.assertIn("starts only when you start it", out.getvalue())
        d3 = tempfile.mkdtemp()
        C.run(d3, role="worker", accept_terms=True, out=io.StringIO(), check=False, autostart=False)     # the flag answers it: no question
        self.assertIs(C.load(d3)["autostart"], False)

    def test_declining_saves_nothing(self):
        self.assertFalse(C.run(self.d, ask=self.answers("no"), out=io.StringIO(), pause=0, check=False))
        self.assertEqual(C.load(self.d), {})

    def test_peer_choice(self):
        C.run(self.d, ask=self.answers("I AGREE", "2", ""), out=io.StringIO(), pause=0, check=False)
        self.assertEqual(C.load(self.d)["role"], "peer")

    def test_noninteractive_flags_and_no_primary(self):
        self.assertTrue(C.run(self.d, role="worker", accept_terms=True, out=io.StringIO(), check=False))
        with self.assertRaises(ValueError): C.record(self.d, role="primary")
        self.assertEqual(C.load(self.d)["role"], "worker")                             # the refused request changed nothing

    def test_model_check_lists_models_tests_one_and_never_downloads(self):
        from mesh import winapp_core as W
        fake = {"kind": "ollama", "host": "http://x", "label": "Ollama", "models": ["a:1b", W.STARTER_MODEL]}
        orig = (W.detect_runtimes, W.test_runtime)
        W.detect_runtimes = lambda *a, **k: [fake]
        asked = []
        W.test_runtime = lambda rt, **k: (asked.append(W.pick_model(rt["models"])) or (True, "It answered."))
        try:
            out = io.StringIO()
            self.assertTrue(C.check_ai(out, mesh_models={W.STARTER_MODEL}))
        finally:
            W.detect_runtimes, W.test_runtime = orig
        self.assertEqual(asked, [W.STARTER_MODEL])                                    # the Mesh's shared small model is tested when installed
        self.assertIn("2 models", out.getvalue()); self.assertIn("Other Mesh computers also have", out.getvalue())

    def test_model_check_messages_when_ollama_is_stopped_or_empty(self):
        from mesh import winapp_core as W
        orig = (W.detect_runtimes, W.find_ollama_exe)
        try:
            W.detect_runtimes = lambda *a, **k: []; W.find_ollama_exe = lambda: "/usr/bin/ollama"
            o = io.StringIO(); self.assertFalse(C.check_ai(o)); self.assertIn("installed but not running", o.getvalue())
            W.detect_runtimes = lambda *a, **k: [{"kind": "ollama", "host": "h", "label": "Ollama", "models": []}]
            o = io.StringIO(); self.assertFalse(C.check_ai(o)); self.assertIn("downloads nothing", o.getvalue())
            W.detect_runtimes = lambda *a, **k: []; W.find_ollama_exe = lambda: None
            o = io.StringIO(); self.assertFalse(C.check_ai(o)); self.assertIn("No AI found", o.getvalue())
        finally:
            W.detect_runtimes, W.find_ollama_exe = orig

    def test_agent_reports_the_saved_role(self):
        from mesh.agent import Agent
        C.record(self.d, "peer", True)
        self.assertEqual(Agent(self.d)._inventory().get("role"), "peer")


if __name__ == "__main__":
    unittest.main()
