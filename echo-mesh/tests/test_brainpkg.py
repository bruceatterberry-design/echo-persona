import base64, copy, json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh import brainpkg as BP, ed25519 as E

SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
SEED2 = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pk = BP.public_key_hex(SEED)
        self.s = BP.BrainStore(self.tmp.name + "/brain", self.pk)

    def tearDown(self):
        self.tmp.cleanup()

    def pkg(self, v, files=None):
        return BP.build("brain", v, files or {"policy.json": '{"max_prompt_chars": 4000}', "notes/readme.md": "hi"}, SEED)

    def test_rfc8032_vectors(self):
        self.assertEqual(E.public_key(SEED).hex(), "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        self.assertEqual(E.sign(SEED, b"").hex(), "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertEqual(E.sign(SEED2, bytes([0x72])).hex()[:16], "92a009a9f0d4cab8")
        self.assertFalse(E.verify(E.public_key(SEED), b"x", E.sign(SEED, b"")))

    def test_apply_then_read_and_not_older(self):
        ok, msg = self.s.apply(self.pkg(1)); self.assertTrue(ok, msg)
        self.assertEqual(json.loads(self.s.read("policy.json"))["max_prompt_chars"], 4000)
        self.assertFalse(self.s.apply(self.pkg(1))[0])          # same version again
        self.assertTrue(self.s.apply(self.pkg(2))[0]); self.assertEqual(self.s.version(), 2)

    def test_wrong_key_tampered_file_extra_file_all_refused(self):
        other = BP.build("brain", 5, {"a.json": "{}"}, SEED2)
        self.assertIn("signature", self.s.apply(other)[1])
        p = self.pkg(3); p["files"]["policy.json"] = base64.b64encode(b'{"evil":1}').decode()
        self.assertIn("hash", self.s.apply(p)[1])
        p = self.pkg(3); p["files"]["extra.json"] = base64.b64encode(b"{}").decode()
        self.assertIn("list", self.s.apply(p)[1])
        p = self.pkg(3); p["manifest"]["version"] = 99
        self.assertIn("signature", self.s.apply(p)[1])
        self.assertEqual(self.s.version(), 0)

    def test_code_and_path_tricks_cannot_be_packaged_or_applied(self):
        for bad in ("run.py", "../x.json", "/etc/x.json", "a\\b.json", "x.sh", ".hidden.json", "a//b.json"):
            with self.assertRaises(BP.BrainError, msg=bad):
                BP.build("b", 1, {bad: "x"}, SEED)

    def test_failed_health_check_rolls_back_at_once(self):
        self.s.apply(self.pkg(1))
        ok, msg = self.s.apply(self.pkg(2), health=lambda d: False)
        self.assertFalse(ok); self.assertEqual(self.s.version(), 1); self.assertIn("is back", msg)
        self.assertFalse(os.path.exists(self.s.path(2)))
        def boom(d): raise RuntimeError("x")
        self.assertFalse(self.s.apply(self.pkg(3), health=boom)[0]); self.assertEqual(self.s.version(), 1)

    def test_manual_rollback_and_pruning(self):
        for v in range(1, 6):
            self.assertTrue(self.s.apply(self.pkg(v))[0])
        self.assertEqual(sorted(os.listdir(self.tmp.name + "/brain/versions")), ["3", "4", "5"])
        self.assertTrue(self.s.rollback()[0]); self.assertEqual(self.s.version(), 4)
        self.assertFalse(self.s.rollback()[0])                  # one step back only; previous is cleared
        self.assertIn("applied", open(self.tmp.name + "/brain/history.log").read())

    def test_garbage_is_refused_not_crashing(self):
        for g in ({}, {"manifest": 1}, {"manifest": {}, "signature": "zz", "files": {}}):
            self.assertFalse(self.s.apply(g)[0])


if __name__ == "__main__":
    unittest.main()


class Flow(unittest.TestCase):
    def test_publish_reaches_a_node_and_only_makes_it_more_careful(self):
        from mesh.coordinator import Coordinator
        from mesh.agent import Agent
        tmp = tempfile.TemporaryDirectory()
        c = Coordinator(tmp.name + "/c", coord_port=0, admin_port=0, beacon_port=0, bind="127.0.0.1").start()
        try:
            a = Agent(tmp.name + "/a"); a.enroll("127.0.0.1", c.ports[0], c.new_pairing_code("compute", "owner", True), "pc")
            self.assertEqual(a.ident["brain_key"], c.brain_key())
            self.assertIsNone(a.check_brain())                                   # nothing published yet
            self.assertEqual(c.brain_publish({"policy.json": '{"max_prompt_chars": 100, "allowed_models": ["m"]}'}, "first"), 1)
            self.assertIn("version 1 is live", a.check_brain())
            self.assertEqual(a._effective_policy()["max_prompt_chars"], 100)
            self.assertEqual(a._effective_policy()["allowed_models"], ["m"])
            open(a.dir + "/policy.json", "w").write('{"max_prompt_chars": 50, "allowed_models": ["own"]}')
            self.assertEqual(a._effective_policy()["max_prompt_chars"], 50)      # owner stricter: owner wins
            self.assertEqual(a._effective_policy()["allowed_models"], ["own"])
            c.brain_publish({"policy.json": "not json at all"})                  # fails its health check: rolled back
            self.assertIn("is back", a.check_brain()); self.assertEqual(a.brain_store().version(), 1)
            c2 = Coordinator(tmp.name + "/c2", coord_port=0, admin_port=0, beacon_port=0, bind="127.0.0.1").start()   # someone else's Mesh signs a package
            try:
                other = c2.brain_publish({"policy.json": "{}"}); pkg = c2._brain_pkg()
                self.assertIn("signature", a.brain_store().apply(pkg)[1])
            finally:
                c2.shutdown()
        finally:
            c.shutdown(); tmp.cleanup()
