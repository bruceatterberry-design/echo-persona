import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh.coordinator import Coordinator


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Coordinator(self.tmp.name + "/c", coord_port=0, admin_port=0, beacon_port=0, bind="127.0.0.1").start()
        self.b = self.c.boards
        self.home = self.node("home-pc", True); self.out = self.node("partner-pc", False); self.other = self.node("stranger", False)

    def tearDown(self):
        self.c.shutdown(); self.tmp.cleanup()

    def node(self, name, lan):
        nid = "n-" + name
        self.c.store.x("INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (nid, name, "compute", "x", "s", "approved", int(lan), "{}", "{}", 0, 100, 0))
        return self.c.store.one("SELECT * FROM nodes WHERE id=?", (nid,))

    def board(self, **kw):
        return self.b.create("Echo and a partner AI", [self.home["id"], self.out["id"]], **kw)

    def test_owner_post_waits_for_approval_then_other_side_reads_it(self):
        bid = self.board()
        pid, st, _ = self.b.owner_post(bid, "echo", "Hello a partner AI")
        self.assertEqual(st, "held")
        self.assertEqual(self.b.read(self.out, bid), [])                       # not visible yet
        self.assertEqual(len(self.b.read_all(bid)), 1)                          # the owner sees it
        self.assertTrue(self.b.decide(pid, True))
        self.assertEqual(self.b.read(self.out, bid)[0]["body"], "Hello a partner AI")

    def test_rejected_post_never_reaches_the_other_side(self):
        bid = self.board(); pid, _, _ = self.b.owner_post(bid, "echo", "no")
        self.b.decide(pid, False)
        self.assertEqual(self.b.read(self.out, bid), [])

    def test_approval_off_posts_at_once(self):
        bid = self.board(approval=False)
        self.assertEqual(self.b.owner_post(bid, "echo", "hi")[1], "posted")

    def test_outbound_filter_cleans_or_refuses(self):
        bid = self.board(approval=False)
        pid, st, note = self.b.owner_post(bid, "echo", "mail me at a@b.com")
        self.assertIn("email", note); self.assertNotIn("a@b.com", self.b.read(self.out, bid)[0]["body"])
        self.assertIsNone(self.b.owner_post(bid, "echo", "-----BEGIN RSA PRIVATE KEY-----\nabc")[0])
        self.assertIsNone(self.b.owner_post(bid, "echo", "about her students and IEP goals")[0])

    def test_outside_computer_posts_and_only_members_read(self):
        bid = self.board()
        self.assertIsNotNone(self.b.node_post(self.out, bid, "hi from a partner AI")[0])
        self.assertEqual(self.b.read(self.home, bid)[0]["author_name"], "partner-pc")
        self.assertIsNone(self.b.read(self.other, bid))                         # not a member
        self.assertIsNone(self.b.node_post(self.other, bid, "let me in")[0])
        self.assertEqual(self.b.boards_for(self.other), [])
        self.assertEqual(len(self.b.boards_for(self.out)), 1)

    def test_text_only_clean_and_limited_and_rate_limited(self):
        bid = self.board()
        pid, _ = self.b.node_post(self.out, bid, "a\x00b\x07c" + "z" * 5000)
        body = self.b.read_all(bid)[0]["body"]
        self.assertNotIn("\x00", body); self.assertLessEqual(len(body), 2000)
        self.assertEqual(self.b.node_post(self.out, bid, "   ")[1], "empty post")
        for _ in range(25):
            r = self.b.node_post(self.out, bid, "spam")
        self.assertIn("too fast", r[1])

    def test_only_approved_computers_join_and_closed_boards_refuse(self):
        bid = self.board()
        with self.assertRaises(ValueError):
            self.b.add_member(bid, "n-nope")
        self.b.close(bid)
        self.assertIsNone(self.b.node_post(self.out, bid, "x")[0])
        self.assertEqual(self.b.owner_post(bid, "echo", "x")[1], "refused")

    def test_home_computer_writing_to_an_outside_reader_is_filtered(self):
        bid = self.board()
        self.b.node_post(self.home, bid, "call 555-123-4567")
        self.assertNotIn("555-123", self.b.read_all(bid)[0]["body"])


if __name__ == "__main__":
    unittest.main()


class HTTP(unittest.TestCase):
    def test_a_real_agent_reads_and_posts_over_the_node_api(self):
        from mesh.agent import Agent
        tmp = tempfile.TemporaryDirectory()
        c = Coordinator(tmp.name + "/c", coord_port=0, admin_port=0, beacon_port=0, bind="127.0.0.1").start()
        try:
            code = c.new_pairing_code("compute", "owner", True)
            a = Agent(tmp.name + "/a"); a.enroll("127.0.0.1", c.ports[0], code, "pc")
            bid = c.boards.create("Talk", [a.ident["node_id"]], approval=False)
            self.assertEqual(a.boards()[0]["title"], "Talk")
            c.boards.owner_post(bid, "echo", "hello")
            self.assertEqual(a.board_posts(bid)[0]["body"], "hello")
            self.assertTrue(a.board_post(bid, "hi back")[0])
            self.assertEqual([p["body"] for p in c.boards.read_all(bid)], ["hello", "hi back"])
            self.assertFalse(a.board_post("b-nope", "x")[0])
        finally:
            c.shutdown(); tmp.cleanup()
