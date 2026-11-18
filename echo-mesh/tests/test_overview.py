import json, os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.test_phase1 import Base
from tests.test_exchange import outside, run_one


class T(Base):
    def setUp(self):
        super().setUp()
        self.a = self.agent(); self.a.heartbeat_once(True)
        self.b = self.agent(); self.b.heartbeat_once(True)
        self.r = outside(self)                                          # someone else's computer

    def ov(self, agent):
        st, d = agent.client().request("GET", "/overview")
        self.assertEqual(st, 200); return d

    def test_a_home_computer_sees_the_whole_mesh(self):
        d = self.ov(self.a)
        names = sorted(n["name"] for n in d["nodes"])
        self.assertEqual(names, ["node1", "node2", "partner-pc"])
        me = [n for n in d["nodes"] if n["is_me"]]; self.assertEqual(len(me), 1)
        self.assertIn("fake:1b", [m for n in d["nodes"] for m in n["models"]])
        self.assertEqual(d["mesh"]["primary"], "Mac mini"); self.assertGreaterEqual(d["mesh"]["uptime_s"], 0)

    def test_an_outside_computer_sees_only_itself_and_totals(self):
        d = self.ov(self.r)
        self.assertEqual([n["name"] for n in d["nodes"]], ["partner-pc"])
        self.assertEqual(d["projects"], [])                              # none of the owner's projects
        self.assertIsNotNone(d["balance"])                                # but its own sharing balance
        blob = json.dumps(d)
        for secret in (self.a.ident["node_id"], self.a.ident["secret"], "node1", "node2"):
            self.assertNotIn(secret, blob)

    def test_nothing_secret_or_access_granting_is_ever_in_it(self):
        pid = self.c.tasks.new_project("private plan"); self.c.tasks.add_task(pid, "t", "my secret prompt text", requires={"model": "fake:1b"})
        for who in (self.a, self.r):
            blob = json.dumps(self.ov(who))
            self.assertNotIn("my secret prompt text", blob); self.assertNotIn(self.a.ident["secret"], blob); self.assertNotIn("n-", blob.replace("n-aaaa", ""))

    def test_unapproved_and_removed_computers_are_hidden(self):
        pend = outside(self); self.c.set_status(pend.ident["node_id"], "revoked")
        self.assertNotIn("revoked", json.dumps(self.ov(self.a)))
        self.assertEqual(len(self.ov(self.a)["nodes"]), 3)

    def test_activity_is_friendly_and_an_outside_computer_hears_only_about_itself(self):
        jid = self.c.submit_job("ping", {}); run_one(self, self.a)
        self.assertIn("node1 finished a job", [e["text"] for e in self.ov(self.a)["events"]])
        self.assertNotIn("node1 finished a job", [e["text"] for e in self.ov(self.r)["events"]])
        for e in self.ov(self.a)["events"]:
            self.assertNotRegex(e["text"], r"j-[0-9a-f]{6}|n-[0-9a-f]{6}")

    def test_share_of_work_adds_up_and_projects_show_for_home_only(self):
        for who, secs in ((self.a, 30), (self.b, 10)):
            self.c.submit_job("ping", {}); jid = run_one(self, who, secs=secs)
        d = self.ov(self.a)
        shares = {n["name"]: n["share_of_work"] for n in d["nodes"] if n["share_of_work"] is not None}
        self.assertAlmostEqual(sum(shares.values()), 100.0, delta=0.2); self.assertGreater(shares["node1"], shares["node2"])
        pid = self.c.tasks.new_project("Newsletter"); self.c.tasks.add_task(pid, "a", "b", requires={"model": "fake:1b"})
        self.assertEqual(self.ov(self.a)["projects"][0]["title"], "Newsletter"); self.assertEqual(self.ov(self.r)["projects"], [])

    def test_the_agent_measures_its_own_latency_and_reads_the_overview(self):
        before = len(self.a.rtts)                                        # setUp already sent one heartbeat
        self.a.heartbeat_once(); self.a.heartbeat_once()
        self.assertEqual(len(self.a.rtts), before + 2); self.assertGreater(self.a.rtts[-1], 0)
        self.a.heartbeat_once()
        node = [n for n in self.c.status()["nodes"] if n["name"] == "node1"][0]
        self.assertIn("rtt_ms", node["load"])                            # reported so the Mesh can show it
        self.assertEqual(len(self.a.refresh_overview()["nodes"]), 3)

    def test_it_needs_a_valid_signature(self):
        from mesh.common import Client
        st, _ = Client("127.0.0.1", self.c.ports[0], pin=self.a.ident["pin"], node_id=self.a.ident["node_id"], secret="00" * 32).request("GET", "/overview")
        self.assertEqual(st, 401)


if __name__ == "__main__":
    unittest.main()
