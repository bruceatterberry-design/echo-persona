import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mesh.governor import Governor


class Clock:
    t = 1000.0
    def __call__(self): return self.t


class T(unittest.TestCase):
    def setUp(self):
        self.c = Clock()
        self.g = Governor(share_limit_pct=10, window_s=100, clock=self.c, load_fn=lambda: 0.1)

    def run_job(self, secs):
        tok = self.g.start(); self.c.t += secs; self.g.end(tok)

    def test_fresh_node_takes_work(self):
        self.assertTrue(self.g.may_take()[0])

    def test_limit_is_enforced_then_released_as_time_passes(self):
        self.run_job(10)                                  # 10 s of a 100 s window = 10% = at the limit
        ok, reason, wait = self.g.may_take()
        self.assertFalse(ok); self.assertIn("10%", reason); self.assertGreater(wait, 0)
        self.c.t += 95                                    # the job ages out of the window
        self.assertTrue(self.g.may_take()[0])

    def test_running_job_counts_and_concurrency_cap(self):
        tok = self.g.start()
        ok, reason, _ = self.g.may_take()
        self.assertFalse(ok); self.assertIn("maximum", reason)
        self.c.t += 30                                    # still running: 30% used and counted
        self.assertEqual(self.g.used_pct(), 30.0)
        self.g.end(tok)

    def test_under_the_limit_still_allowed(self):
        self.run_job(5)
        self.assertTrue(self.g.may_take()[0])             # 5% < 10%

    def test_owner_priority_beats_everything(self):
        g = Governor(share_limit_pct=100, window_s=100, clock=self.c, load_fn=lambda: 0.95)
        ok, reason, _ = g.may_take()
        self.assertFalse(ok); self.assertIn("owner", reason)

    def test_load_not_measurable_is_not_guessed(self):
        g = Governor(clock=self.c, load_fn=lambda: None)
        self.assertTrue(g.may_take()[0]); self.assertFalse(g.report()["owner_load_measured"])

    def test_pause_and_zero_limit(self):
        self.g.paused = True
        self.assertFalse(self.g.may_take()[0])
        self.g.paused = False; self.g.set_limit(0)
        self.assertFalse(self.g.may_take()[0])

    def test_limit_is_clamped(self):
        self.g.set_limit(500); self.assertEqual(self.g.limit, 100)
        self.g.set_limit(-5); self.assertEqual(self.g.limit, 0)

    def test_report_shape(self):
        r = self.g.report()
        self.assertEqual(set(r), {"share_limit_pct", "used_pct", "taking_work", "reason", "owner_load_measured"})


if __name__ == "__main__":
    unittest.main()


class TestAgentEnforcement(unittest.TestCase):
    """A real coordinator and agent: a node over its limit leaves work waiting instead of taking it."""
    def test_agent_rests_after_a_job_and_takes_the_next_when_under_the_limit(self):
        import tempfile, threading, json
        from mesh.coordinator import Coordinator
        from mesh.agent import Agent
        tmp = tempfile.TemporaryDirectory()
        c = Coordinator(tmp.name + "/c", coord_port=0, admin_port=0, beacon_port=0, bind="127.0.0.1").start()
        a = Agent(tmp.name + "/a")
        a.enroll("127.0.0.1", c.ports[0], c.new_pairing_code("compute"), "n")
        a.local_limit = 10                                                          # the owner's cap; the stricter of owner and coordinator wins
        a.gov = Governor(share_limit_pct=10, window_s=6, load_fn=lambda: 0.0)     # 10% of 6 s = 0.6 s allowed
        real = a.execute
        def slow(job):
            time.sleep(1.0); return real(job)
        a.execute = slow
        threading.Thread(target=a.run, kwargs={"hb_every": 1}, daemon=True).start()
        j1, j2 = c.submit_job("ping", {}), c.submit_job("ping", {})
        st = lambda j: c.store.one("SELECT status FROM jobs WHERE id=?", (j,))["status"]
        t0 = time.time()
        while st(j1) != "done" and time.time() - t0 < 15: time.sleep(0.2)
        self.assertEqual(st(j1), "done")
        time.sleep(2)
        self.assertNotEqual(st(j2), "done")               # over its limit: it is resting, the job waits
        t0 = time.time()
        while st(j2) != "done" and time.time() - t0 < 20: time.sleep(0.5)
        self.assertEqual(st(j2), "done")                  # once the first job aged out, it took the second
        a.stop.set(); c.shutdown(); tmp.cleanup()

    def test_coordinator_cap_applies_to_an_outside_node(self):
        import tempfile
        from mesh.coordinator import Coordinator
        from mesh.agent import Agent
        tmp = tempfile.TemporaryDirectory()
        c = Coordinator(tmp.name + "/c", coord_port=0, admin_port=0, beacon_port=0, bind="127.0.0.1").start()
        a = Agent(tmp.name + "/a")
        a.enroll("127.0.0.1", c.ports[0], c.new_pairing_code("compute", lan=False), "outside")
        c.approve(a.ident["node_id"])
        a.heartbeat_once()
        self.assertEqual(a.gov.limit, 30.0)               # outside participants start at 30% even if the owner allows 100%
        c.shutdown(); tmp.cleanup()
