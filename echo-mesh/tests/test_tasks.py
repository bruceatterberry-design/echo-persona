import json, os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.test_phase1 import Base, admin
from tests.test_exchange import outside, run_one
from mesh import tasks as TK, policy


class T(Base):
    def setUp(self):
        super().setUp()
        self.t = self.c.tasks
        self.p = self.t.new_project("demo")
        self.w = self.agent(); self.w.heartbeat_once(True)

    def add(self, title="t", desc="do it", **kw):
        kw.setdefault("requires", {"model": "fake:1b"})
        return self.t.add_task(self.p, title, desc, **kw)

    def status(self, tid): return self.t.task_view(tid)["status"]

    def work(self, agent=None):
        jid = run_one(self, agent or self.w)
        self.t.advance()
        return jid

    def test_dependency_order_and_context_passing(self):
        a = self.add("research", "find facts")
        b = self.add("write", "write it up", deps=[a])
        self.assertEqual((self.status(a), self.status(b)), ("running", "waiting"))     # b cannot start before a is accepted
        self.work(); self.assertEqual(self.status(a), "review")
        self.assertEqual(self.status(b), "waiting")                                     # reviewed is not accepted yet
        self.t.accept(a)
        self.assertEqual(self.status(b), "running")
        sent = json.loads(self.c.store.one("SELECT payload FROM jobs WHERE id=?", (self.t.task_view(b)["job_id"],))["payload"])["prompt"]
        self.assertIn("write it up", sent); self.assertIn("echo:find facts", sent)       # a's accepted result went into b
        self.work(); self.t.accept(b)
        self.assertEqual(self.t.project_view(self.p)["status"], "done")
        self.assertEqual([x["title"] for x in self.t.workspace(self.p)], ["research", "write"])

    def test_private_tasks_never_leave_and_shared_ones_are_cleaned(self):
        r = outside(self)
        priv = self.add("private", "my secret plan", classification="private")
        self.assertIsNone(run_one(self, r))                                               # an outside computer cannot take a private task
        self.work(); self.t.accept(priv)
        sh = self.add("shared", "use key sk-abcdefghijklmnop1234 and password: hunter2222 for it", classification="shared")
        sent = json.loads(self.c.store.one("SELECT payload FROM jobs WHERE id=?", (self.t.task_view(sh)["job_id"],))["payload"])["prompt"]
        self.assertNotIn("sk-abc", sent); self.assertNotIn("hunter2222", sent)
        self.assertIn("removed from the text before sending", self.t.task_view(sh)["report"])
        self.assertIsNotNone(run_one(self, r))                                            # this one may go outside

    def test_private_key_blocks_a_shared_task_and_dependents(self):
        a = self.add("leaky", "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----", classification="shared")
        b = self.add("after", "next", deps=[a])
        self.assertEqual(self.status(a), "blocked"); self.assertIn("private key", self.t.task_view(a)["report"])
        self.t.advance(); self.assertEqual(self.status(b), "blocked")
        self.assertEqual(self.c.store.q("SELECT COUNT(*) c FROM jobs")[0]["c"], 0)       # nothing was ever queued

    def test_a_private_result_is_not_handed_to_a_shared_task(self):
        a = self.add("private-step", "private thing", classification="private")
        b = self.add("shared-step", "public thing", classification="shared", deps=[a])
        self.work(); self.t.accept(a)
        sent = json.loads(self.c.store.one("SELECT payload FROM jobs WHERE id=?", (self.t.task_view(b)["job_id"],))["payload"])["prompt"]
        self.assertNotIn("private thing", sent)
        self.assertIn("was not included", self.t.task_view(b)["report"])

    def test_approval_gate(self):
        a = self.add("risky", "needs a yes", needs_approval=True)
        self.assertEqual(self.status(a), "needs-approval")
        self.assertIsNone(run_one(self, self.w))                                          # nothing runs without the yes
        self.assertTrue(self.t.approve(a)); self.assertEqual(self.status(a), "running")
        self.assertFalse(self.t.approve(a))                                               # a yes is used once

    def test_reject_with_feedback_retries_then_gives_up(self):
        a = self.add("draft", "write a poem"); b = self.add("after", "x", deps=[a])
        self.work(); self.t.reject(a, "make it shorter")
        self.assertEqual(self.status(a), "running")
        sent = json.loads(self.c.store.one("SELECT payload FROM jobs WHERE id=?", (self.t.task_view(a)["job_id"],))["payload"])["prompt"]
        self.assertIn("make it shorter", sent)
        self.work(); self.t.reject(a, "still no")
        self.assertEqual(self.status(a), "failed")                                        # MAX_TRIES reached
        self.t.advance(); self.assertEqual(self.status(b), "blocked")

    def test_budget_stops_a_slow_task_and_a_late_result_is_refused(self):
        a = self.add("slow", "take forever", budget_s=1)
        st, job = self.w.client().request("GET", "/jobs/next")                            # the node takes it and sits on it
        self.c.store.x("UPDATE jobs SET assigned=? WHERE id=?", (time.time() - 5, job["id"]))
        self.t.advance()
        self.assertEqual(self.status(a), "failed"); self.assertIn("budget", self.t.task_view(a)["report"])
        st, _ = self.w.client().request("POST", f"/jobs/{job['id']}/result", {"ok": True, "result": {"response": "late"}})
        self.assertEqual(st, 409)

    def test_incompatible_work_never_runs_at_the_same_time(self):
        a = self.add("edit-1", "x", exclusive_group="file-a"); b = self.add("edit-2", "y", exclusive_group="file-a")
        c = self.add("other", "z", exclusive_group="file-b")
        self.assertEqual((self.status(a), self.status(b), self.status(c)), ("running", "ready", "running"))
        self.work(); self.t.accept(a)
        self.assertEqual(self.status(b), "running")

    def test_bad_input_is_refused(self):
        with self.assertRaises(ValueError): self.add(classification="secret")
        with self.assertRaises(ValueError): self.add(deps=["t-nope"])
        with self.assertRaises(ValueError): self.t.add_task("p-none", "t", "d")
        with self.assertRaises(ValueError): self.add(title=" ")
        nm = self.add("no-model", "x", requires={}); self.assertEqual(self.status(nm), "blocked")

    def test_the_computers_own_rules_apply_even_to_approved_tasks(self):
        self.w.dir_policy = os.path.join(self.w.dir, "policy.json")
        json.dump({"allowed_models": ["other:7b"], "max_prompt_chars": 50}, open(self.w.dir_policy, "w"))
        ok, res, err = self.w.execute({"type": "generate", "payload": {"model": "fake:1b", "prompt": "hi"}})
        self.assertFalse(ok); self.assertIn("not allowed the model", err)
        self.assertTrue(self.w.execute({"type": "ping", "payload": {}})[0])               # pings are unaffected
        json.dump({"max_prompt_chars": 5}, open(self.w.dir_policy, "w"))
        self.assertIn("longer than", self.w.execute({"type": "generate", "payload": {"model": "fake:1b", "prompt": "way too long"}})[2])

    def test_over_the_admin_api(self):
        pid = admin(self.c, "POST", "/projects", {"title": "api"})["id"]
        tid = admin(self.c, "POST", "/tasks", {"project": pid, "title": "t", "description": "d", "requires": {"model": "fake:1b"}})["id"]
        self.assertEqual(admin(self.c, "GET", f"/tasks/{tid}")["status"], "running")
        run_one(self, self.w); self.t.advance()
        self.assertTrue(admin(self.c, "POST", f"/tasks/{tid}/accept", {})["ok"])
        self.assertEqual(admin(self.c, "GET", f"/projects/{pid}")["progress"], "1/1")
        self.assertEqual(admin(self.c, "GET", f"/projects/{pid}/workspace")[0]["result"], "echo:d")
        import urllib.request, urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:
            admin(self.c, "POST", "/tasks", {"project": pid, "title": "t", "description": "d", "classification": "bad"})
        self.assertEqual(cm.exception.code, 400)


class Policy(unittest.TestCase):
    def test_redaction_kinds(self):
        txt, found = policy.redact("Authorization: Bearer abcdefghijklmnop1234 and AKIAABCDEFGHIJKLMNOP and xoxb-1234567890-abc")
        self.assertEqual(sorted(found), ["Slack token", "access key", "bearer token"]); self.assertNotIn("AKIA", txt)
        self.assertEqual(policy.redact("nothing secret here"), ("nothing secret here", []))

    def test_too_long_is_refused(self):
        ok, _, why, _ = policy.check_dispatch("shared", "x" * (policy.MAX_PROMPT_CHARS + 1), True)
        self.assertFalse(ok); self.assertIn("too long", why)


if __name__ == "__main__":
    unittest.main()
