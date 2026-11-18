"""11-17 review: what may leave the Mac. Personal details are removed before text goes to someone else's computer; school work stays home."""
import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.test_phase1 import Base
from mesh import policy


class Scrub(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.f = os.path.join(self.d, "private-terms.json")
        json.dump({"terms": ["Zelda Quimby", "Sprocket", "Plainville"]}, open(self.f, "w"))
        policy.set_terms(self.f)

    def tearDown(self):
        policy.set_terms(None)

    def test_generic_personal_details(self):
        txt, found = policy.redact("Call 555-123-4567 or mail a.b@example.com, at 42 Elm Street, box 192.168.1.20, host mac.tail1234.ts.net, mac aa:bb:cc:dd:ee:ff")
        for k in ("phone number", "email address", "street address", "home network address"):
            self.assertIn(k, found)
        for leaked in ("555-123-4567", "a.b@example.com", "42 Elm Street", "192.168.1.20", "tail1234.ts.net", "aa:bb:cc:dd:ee:ff"):
            self.assertNotIn(leaked, txt)

    def test_private_terms_from_the_owners_file(self):
        txt, found = policy.redact("Zelda Quimby walked sprocket near Plainville.")
        self.assertIn("private name", found)
        self.assertNotIn("Zelda", txt); self.assertNotIn("Sprocket".lower(), txt.lower()); self.assertNotIn("Plainville", txt)

    def test_terms_are_whole_words_and_reload_on_change(self):
        self.assertEqual(policy.redact("Sprocketing along")[1], [])   # not a whole word
        json.dump({"terms": ["Sprocketing"]}, open(self.f, "w")); os.utime(self.f, (1, 2e9))
        self.assertIn("private name", policy.redact("Sprocketing along")[1])

    def test_no_file_means_only_generic_scrub(self):
        policy.set_terms(os.path.join(self.d, "missing.json"))
        self.assertEqual(policy.redact("Plainville is nice")[1], [])

    def test_school_work_is_refused_not_cleaned(self):
        ok, _, why, found = policy.check_dispatch("shared", "Draft an IEP goal for my student", True)
        self.assertFalse(ok); self.assertIn("school work", found); self.assertIn("stays home", why)

    def test_private_never_goes_outside_and_inside_is_untouched(self):
        self.assertFalse(policy.check_dispatch("private", "hello", True)[0])
        ok, text, _, found = policy.check_dispatch("shared", "Zelda Quimby 555-123-4567", False)
        self.assertTrue(ok); self.assertEqual((text, found), ("Zelda Quimby 555-123-4567", []))   # staying on the owner's own computers


class OutsideJobs(Base):
    def test_outside_job_is_cleaned_and_school_work_stays_home(self):
        policy.set_terms(None)
        self.c.submit_job("generate", {"model": "fake:1b", "prompt": "email me at x@y.com about Q3"}, requires={"model": "fake:1b"}, ext_ok=True)
        j = self.c.store.q("SELECT payload, ext_ok FROM jobs ORDER BY created DESC LIMIT 1")[0]
        self.assertEqual(j["ext_ok"], 1); self.assertNotIn("x@y.com", j["payload"])
        self.c.submit_job("generate", {"model": "fake:1b", "prompt": "Write IEP goals for her students"}, requires={"model": "fake:1b"}, ext_ok=True)
        j = self.c.store.q("SELECT ext_ok FROM jobs ORDER BY created DESC LIMIT 1")[0]
        self.assertEqual(j["ext_ok"], 0)   # kept at home


if __name__ == "__main__":
    unittest.main()
