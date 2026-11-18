import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh import speedup as S

G = 1024 ** 3


class T(unittest.TestCase):
    def test_family_and_size(self):
        self.assertEqual(S.family("llama3.2:1b"), "llama3.2")
        self.assertEqual(S.family("Llama-3.3-70B-Instruct-Q4_K_M.gguf"), "llama-3.3")
        self.assertEqual(S.size_b("Llama-3.2-1B-Instruct"), 1.0)
        self.assertEqual(S.size_b("qwen2.5-coder:3b"), 3.0)

    def test_plan_picks_smallest_that_fits(self):
        sz = {"llama3.3:70b": 40 * G, "llama3.2:3b": 2 * G, "llama3.2:1b": 1 * G, "qwen2.5:7b": 4 * G}
        p = S.plan("llama3.3:70b", list(sz), 44 * G, sz.get)       # 3b does not leave room? 40+1(1b)+1.5 = 42.5 fits; sorted smallest first
        self.assertEqual((p["mode"], p["draft"]), ("speculative", "llama3.2:1b"))
        p = S.plan("llama3.3:70b", list(sz), 41 * G, sz.get)
        self.assertEqual(p["mode"], "tuning-only"); self.assertIn("skipped", p["reason"])

    def test_plan_without_a_same_family_smaller_model(self):
        p = S.plan("llama3.2:3b", ["llama3.2:3b", "qwen3:1b", "llama3.2:7b"], 99 * G, lambda n: G)
        self.assertEqual(p["mode"], "tuning-only"); self.assertIsNone(p["draft"])

    def test_mtp_is_only_reported(self):
        self.assertIn("vLLM", S.mtp_note("Qwen3-Next-80B-A3B-Instruct"))
        self.assertIsNone(S.mtp_note("llama3.2:1b"))

    def test_commands_are_argument_lists(self):
        c = S.server_command("/x/llama-server", "/m/main.gguf", "/m/draft.gguf", settings={"threads": 4, "batch": 4096, "ubatch": 1024, "gpu_layers": 93, "flash": 1})
        self.assertIsInstance(c, list)
        for flag in ("-md", "--draft-max", "--draft-min", "-ngld", "--flash-attn", "--batch-size"):
            self.assertIn(flag, c)
        self.assertEqual(c[c.index("-ngl") + 1], "93")
        self.assertNotIn("-md", S.server_command("/x/llama-server", "/m/main.gguf"))
        self.assertEqual(S.parse_bench(json.dumps([{"n_gen": 64, "avg_ts": 73.5}, {"n_gen": 0, "avg_ts": 999}])), 73.5)
        self.assertIsNone(S.parse_bench("garbage"))

    def test_tune_keeps_improvements_and_drops_warmup(self):
        calls = []
        def trial(s):
            calls.append(dict(s))
            return 10 + s["batch"] / 1000 + (5 if s["flash"] else 0)
        space = {"batch": (512, 1024, 4096), "flash": (0, 1)}
        r = S.tune(trial, space)
        self.assertEqual(r["settings"], {"batch": 4096, "flash": 1})
        self.assertGreater(r["tps"], r["baseline"])
        self.assertEqual(calls[0], calls[1])                      # warm-up repeated the baseline, its number was discarded
        self.assertEqual(r["trials"], 1 + 2 + 1)

    def test_tune_skips_failures_and_respects_the_limit(self):
        r = S.tune(lambda s: None if s["batch"] == 1024 else 5.0, {"batch": (512, 1024, 2048)})
        self.assertEqual(r["settings"]["batch"], 512)
        self.assertIsNone(S.tune(lambda s: None, {"batch": (1, 2)})["settings"])
        self.assertEqual(S.tune(lambda s: 1.0, {"batch": tuple(range(1, 50))}, max_trials=5)["trials"], 5)

    def test_ollama_blob_and_saved_file(self):
        with tempfile.TemporaryDirectory() as d:
            m = os.path.join(d, "manifests", "registry.ollama.ai", "library", "llama3.2"); os.makedirs(m); os.makedirs(os.path.join(d, "blobs"))
            open(os.path.join(d, "blobs", "sha256-abc"), "w").write("x")
            json.dump({"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:abc"}]}, open(os.path.join(m, "1b"), "w"))
            self.assertTrue(S.ollama_blob("llama3.2:1b", d).endswith("sha256-abc"))
            self.assertIsNone(S.ollama_blob("llama3.2:9b", d))
            S.save(d, {"enabled": True}); self.assertEqual(S.load(d), {"enabled": True}); self.assertEqual(S.load(os.path.join(d, "nope")), {})


if __name__ == "__main__":
    unittest.main()


class Offer(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.root = os.path.join(self.d, "o"); self.bin = os.path.join(self.d, "bin"); os.makedirs(self.bin)
        os.makedirs(os.path.join(self.root, "blobs"))
        for name, tag, size in (("llama3.3", "70b", 40), ("llama3.2", "1b", 1)):
            m = os.path.join(self.root, "manifests", "registry.ollama.ai", "library", name); os.makedirs(m, exist_ok=True)
            open(os.path.join(self.root, "blobs", f"sha256-{tag}"), "wb").write(b"x" * size)
            json.dump({"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": f"sha256:{tag}"}]}, open(os.path.join(m, tag), "w"))
        os.environ["OLLAMA_MODELS"] = self.root
        open(os.path.join(self.bin, "llama-server"), "w").close(); open(os.path.join(self.bin, "llama-bench"), "w").close()

    def tearDown(self):
        os.environ.pop("OLLAMA_MODELS", None)

    def answers(self, *a):
        it = iter(a); return lambda prompt="": next(it)

    def run_offer(self, ask, **kw):
        import io
        out = io.StringIO()
        r = S.offer(self.d, ask, out, models=["llama3.3:70b", "llama3.2:1b"], find=lambda: os.path.join(self.bin, "llama-server"), mem=(10 * G, "GPU"), **kw)
        return r, out.getvalue()

    def test_default_answer_changes_nothing(self):
        r, txt = self.run_offer(self.answers(""))
        self.assertIsNone(r); self.assertFalse(os.path.exists(os.path.join(self.d, S.FILE))); self.assertIn("Skipped", txt)

    def test_yes_plans_tunes_and_saves_without_starting_anything(self):
        r, txt = self.run_offer(self.answers("y", "y"), trial_for=lambda b, g: (lambda s: 10.0 + s["batch"] / 1000))
        self.assertEqual((r["mode"], r["draft"]), ("speculative", "llama3.2:1b"))
        self.assertIn("-md", r["command"]); self.assertGreater(r["tuned_tps"], r["baseline_tps"])
        self.assertTrue(S.load(self.d)["enabled"]); self.assertIn("does not start it", txt)

    def test_no_llama_server_explains_and_saves_nothing(self):
        import io
        out = io.StringIO()
        r = S.offer(self.d, self.answers("y"), out, models=["llama3.2:1b"], find=lambda: None)
        self.assertIsNone(r); self.assertIn("does not install", out.getvalue()); self.assertEqual(S.load(self.d), {})
