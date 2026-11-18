import json, os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.test_phase1 import Base, admin
from mesh import coordinator as CO


def outside(t, owner="partner"):
    """An outside computer: joins by pairing code, owned by someone else, so it is not 'LAN'."""
    t.n += 1
    from mesh.agent import Agent
    a = Agent(f"{t.tmp.name}/o{t.n}", t.ollama)
    code = t.c.new_pairing_code("compute", owner=owner, lan=False)
    a.enroll("127.0.0.1", t.c.ports[0], code, "partner-pc")
    t.c.approve(a.ident["node_id"])
    a.heartbeat_once(True)
    return a


def run_one(t, agent, secs=0.0):
    """The agent asks for work, runs it and reports (like its main loop, one step)."""
    st, job = agent.client().request("GET", "/jobs/next")
    if st != 200 or not job:
        return None
    ok, res, err = agent.execute(job)
    agent.client().request("POST", f"/jobs/{job['id']}/result", {"ok": ok, "result": res, "error": err})
    if secs:
        t.c.store.x("UPDATE jobs SET assigned=finished-? WHERE id=?", (secs, job["id"]))
    return job["id"]


class T(Base):
    def req(self, agent, prompt="hi"):
        return agent.client().request("POST", "/jobs", {"type": "generate", "payload": {"model": "fake:1b", "prompt": prompt}})

    def test_all_workers_help_the_outside_computer_and_it_sees_only_its_own_result(self):
        r, w = outside(self), self.agent()
        w.heartbeat_once(True)
        st, d = self.req(r)
        self.assertEqual(st, 200)
        jid = run_one(self, w, secs=20)                       # one of the owner's workers answers it (20 s of work)
        self.assertEqual(jid, d["id"])
        st, v = r.client().request("GET", f"/jobs/{jid}")
        self.assertEqual(v["status"], "done"); self.assertEqual(v["result"]["response"], "echo:hi")
        st, _ = w.client().request("GET", f"/jobs/{jid}")      # nobody else can read someone's request
        self.assertEqual(st, 404)
        flows = {(f["from"], f["to"]): f["seconds"] for f in self.c.exchange()}
        self.assertEqual(flows, {("owner", "partner"): 20.0})

    def test_a_node_never_runs_its_own_request(self):
        r = outside(self)
        self.req(r)
        self.assertIsNone(run_one(self, r))

    def test_outside_computer_only_gets_work_bruce_chose_to_send(self):
        r = outside(self)
        self.c.submit_job("generate", {"model": "fake:1b", "prompt": "private-ish"}, requires={"model": "fake:1b"})
        self.assertIsNone(run_one(self, r))                   # default: the owner's jobs stay on the owner's computers
        self.c.submit_job("generate", {"model": "fake:1b", "prompt": "shared"}, requires={"model": "fake:1b"}, ext_ok=True)
        self.assertIsNotNone(run_one(self, r, secs=10))       # this one he allowed
        self.assertEqual(self.c.exchange()[0]["from"], "partner")

    def test_switch_makes_bruces_model_jobs_available_to_outside_help(self):
        r = outside(self)
        self.assertEqual(admin(self.c, "POST", "/settings", {"ext_help": True})["ext_help"], True)
        self.c.submit_job("generate", {"model": "fake:1b", "prompt": "a"}, requires={"model": "fake:1b"})
        self.assertIsNotNone(run_one(self, r))
        admin(self.c, "POST", "/settings", {"ext_help": False})
        self.c.submit_job("generate", {"model": "fake:1b", "prompt": "b"}, requires={"model": "fake:1b"})
        self.assertIsNone(run_one(self, r))
        self.c.submit_job("ping", {})                          # pings are never sent out by the switch
        self.assertIsNone(run_one(self, r))

    def test_an_outside_nodes_request_never_goes_outside(self):
        j = self.c.submit_job("generate", {"model": "m", "prompt": "p"}, requester="n-0000000000", ext_ok=True)
        self.assertEqual(self.c.store.one("SELECT ext_ok FROM jobs WHERE id=?", (j,))["ext_ok"], 0)

    def test_allowance_stops_one_sided_use(self):
        r, w = outside(self), self.agent()
        w.heartbeat_once(True)
        for _ in range(2):
            self.req(r); run_one(self, w, secs=CO.DEBT_CAP_S)  # the outside side has now used 2x the cap and given nothing
        st, d = self.req(r)
        self.assertEqual(st, 429); self.assertIn("allowance", d["error"])
        # now their computer helps with one of the owner's jobs: balance recovers and requests work again
        self.c.submit_job("generate", {"model": "fake:1b", "prompt": "x"}, requires={"model": "fake:1b"}, ext_ok=True)
        run_one(self, r, secs=CO.DEBT_CAP_S * 2)
        self.assertEqual(self.req(r)[0], 200)

    def test_open_request_cap_and_job_types(self):
        r = outside(self)
        for _ in range(CO.MAX_OPEN_JOBS):
            self.assertEqual(self.req(r)[0], 200)
        self.assertEqual(self.req(r)[0], 429)
        st, d = r.client().request("POST", "/jobs", {"type": "ping"})
        self.assertEqual(st, 429)
        st, d = r.client().request("POST", "/jobs", {"type": "shell", "payload": {}})
        self.assertEqual(st, 429)

    def test_lan_nodes_are_exempt_from_the_allowance(self):
        w = self.agent()
        for _ in range(8):
            self.assertEqual(w.client().request("POST", "/jobs", {"type": "generate", "payload": {"model": "m", "prompt": "p"}})[0], 200)

    def test_balance_numbers(self):
        r, w = outside(self), self.agent()
        w.heartbeat_once(True)
        self.req(r); run_one(self, w, secs=30)
        b = self.c.exchange_balance("partner"); self.assertEqual((b["gave_s"], b["used_s"], b["owed_s"]), (0, 30.0, 30.0))
        b = self.c.exchange_balance("owner"); self.assertEqual((b["gave_s"], b["used_s"]), (30.0, 0))


if __name__ == "__main__":
    unittest.main()
