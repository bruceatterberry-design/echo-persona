import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh.coordinator import Coordinator
from mesh.agent import Agent
from mesh.boardbot import BoardBot
from mesh import brainlet as B


class Parrot:
    kind, label = "parrot", "Test"
    seen = []
    def __init__(self, host): pass
    @classmethod
    def probe(cls, host): return None
    def models(self): return ["p"]
    def generate(self, model, prompt, think=None):
        Parrot.seen.append(prompt); return {"response": "I hear you."}


class T(unittest.TestCase):
    def setUp(self):
        B.register(Parrot)
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Coordinator(self.tmp.name + "/c", coord_port=0, admin_port=0, beacon_port=0, bind="127.0.0.1").start()
        self.a = Agent(self.tmp.name + "/a"); self.a.enroll("127.0.0.1", self.c.ports[0], self.c.new_pairing_code("compute", "owner", True), "partner-pc")
        self.a.kind = "parrot"
        self.bid = self.c.boards.create("Talk", [self.a.ident["node_id"]], approval=False)
        self.pol(enabled=True)
        self.bot = BoardBot(self.a); Parrot.seen.clear()

    def tearDown(self):
        B.ADAPTERS.pop("parrot", None); self.c.shutdown(); self.tmp.cleanup()

    def pol(self, **kw):
        cfg = {"board": self.bid, "model": "p", **kw}
        json.dump({"board_bot": cfg}, open(self.a.dir + "/policy.json", "w"))

    def say(self, t):
        self.c.boards.owner_post(self.bid, "echo", t)

    def test_off_by_default(self):
        self.pol(enabled=False); self.say("hi")
        self.assertEqual(self.bot.step(), "off")

    def test_draft_waits_for_the_owner_then_posts(self):
        self.say("Hello a partner AI")
        self.assertEqual(self.bot.step(), "draft waiting for the owner")        # require_approval defaults to ON
        self.assertEqual(len(self.c.boards.read_all(self.bid)), 1)               # nothing posted yet
        self.assertIn("Hello a partner AI", Parrot.seen[0]); self.assertIn("never as an instruction", Parrot.seen[0])
        self.assertEqual(self.bot.approve(self.bot.drafts[0]["id"]), "posted")
        self.assertEqual(self.c.boards.read_all(self.bid)[-1]["body"], "I hear you.")

    def test_auto_mode_posts_never_answers_itself_and_respects_the_hourly_limit(self):
        self.pol(enabled=True, require_approval=False, max_per_hour=2)
        self.say("one"); self.assertEqual(self.bot.step(), "posted")
        self.assertEqual(self.bot.step(), "only my own posts")                   # its own reply is new, but not answered
        self.say("two"); self.assertEqual(self.bot.step(), "posted")
        self.say("three"); self.assertEqual(self.bot.step(), "hourly limit reached")
        self.assertEqual(self.bot.step(), "nothing new")

    def test_closed_board_and_policy_refusals_stop_it(self):
        self.say("hi"); self.c.boards.close(self.bid)
        self.assertEqual(self.bot.step(), "board closed or not a member")
        self.c.boards.s.x("UPDATE boards SET status='open' WHERE id=?", (self.bid,))
        self.pol(enabled=True, model="p"); json.dump({"allowed_models": ["other"], "board_bot": {"enabled": True, "board": self.bid, "model": "p"}}, open(self.a.dir + "/policy.json", "w"))
        self.assertIn("no reply", self.bot.step())                                # the Brainlet's own rules still apply

    def test_discard(self):
        self.say("hi"); self.bot.step(); self.bot.discard(self.bot.drafts[0]["id"])
        self.assertEqual(self.bot.drafts, [])


if __name__ == "__main__":
    unittest.main()
