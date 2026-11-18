import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh import brainlet as B, cognition as C
from tests.test_brainlet import serve


class T(unittest.TestCase):
    def test_defaults_have_all_three_and_no_personal_words(self):
        txt = C.load()
        for w in ("Awareness:", "Conversation:", "Style:"):
            self.assertIn(w, txt)
        self.assertLessEqual(len(txt), C.MAX_CHARS)
        for bad in ("the owner", "Alex", "Pat", "Echo", "Springfield"):
            self.assertNotIn(bad, txt)

    def test_brain_files_override_and_fall_back(self):
        files = {"cognition/style.md": "Style: be brief."}
        def read(rel):
            if rel in files: return files[rel]
            raise FileNotFoundError(rel)
        txt = C.load(read)
        self.assertIn("Style: be brief.", txt)
        self.assertIn("Awareness:", txt)             # missing file -> default

    def test_job_uses_profile_only_when_asked_and_allowed(self):
        s, host = serve("ollama")
        try:
            run = lambda pol, payload: B.Brainlet(lambda: pol, "ollama", host, profile_fn=lambda: "PROFILE").run({"type": "generate", "payload": payload})
            ok, r, _ = run({}, {"model": "m1", "prompt": "q", "cognition": True})
            self.assertTrue(ok); self.assertEqual(r["response"], "hi PROFILE\n\n---\nq")
            self.assertEqual(run({}, {"model": "m1", "prompt": "q"})[1]["response"], "hi q")                      # not asked
            self.assertEqual(run({"cognition": False}, {"model": "m1", "prompt": "q", "cognition": True})[1]["response"], "hi q")   # owner switch wins
            bad = B.Brainlet(lambda: {}, "ollama", host, profile_fn=lambda: 1 / 0)
            self.assertTrue(bad.run({"type": "generate", "payload": {"model": "m1", "prompt": "q", "cognition": True}})[0])      # broken profile never stops a job
        finally:
            s.shutdown()


if __name__ == "__main__":
    unittest.main()
