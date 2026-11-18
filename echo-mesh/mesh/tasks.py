"""Projects and tasks: split a piece of work into steps with dependencies, send each step out under the policy rules, and have a
person (or Echo) review each result before it counts. Every task is run as an ordinary Mesh job, so all earlier protections still apply."""
import json, time, uuid
from . import policy

OPEN = ("waiting", "ready", "needs-approval", "running", "review")
MAX_TRIES = 2          # a rejected task is tried again this many times in total
FIELDS = ("title", "description", "requires", "deps", "classification", "priority", "budget_s", "deadline", "needs_approval", "exclusive_group")


class Tasks:
    def __init__(self, coord):
        self.c = coord
        self.s = coord.store

    # ---- creating ----
    def new_project(self, title, owner="owner"):
        pid = "p-" + uuid.uuid4().hex[:8]
        self.s.x("INSERT INTO projects VALUES(?,?,?,?,?)", (pid, str(title)[:120], owner, "open", time.time()))
        self.s.audit("project-created", project=pid)
        return pid

    def add_task(self, project, title, description, requires=None, deps=(), classification="private", priority=5,
                 budget_s=300.0, deadline=None, needs_approval=False, exclusive_group=""):
        if not self.s.one("SELECT 1 FROM projects WHERE id=?", (project,)):
            raise ValueError("no such project")
        if classification not in policy.CLASSES:
            raise ValueError("classification must be 'private' or 'shared'")
        for d in deps:
            if not self.s.one("SELECT 1 FROM tasks WHERE id=? AND project=?", (d, project)):
                raise ValueError("a dependency is not in this project")
        if not str(title).strip() or not str(description).strip():
            raise ValueError("a task needs a title and a description")
        tid = "t-" + uuid.uuid4().hex[:8]
        self.s.x("INSERT INTO tasks(id,project,title,description,requires,deps,classification,priority,budget_s,deadline,needs_approval,exclusive_group,"
                 "status,feedback,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (tid, project, str(title)[:120], str(description), json.dumps(requires or {}), json.dumps(list(deps)), classification, int(priority),
                  float(budget_s), deadline, int(bool(needs_approval)), str(exclusive_group)[:40], "waiting", "[]", time.time()))
        self.advance()
        return tid

    # ---- the engine: called after every change and every few seconds ----
    def advance(self):
        for t in self.s.q("SELECT * FROM tasks WHERE status IN ('waiting','ready','running') ORDER BY priority, created"):
            getattr(self, "_" + t["status"].replace("-", "_"))(t)

    def _waiting(self, t):
        deps = [self.s.one("SELECT status FROM tasks WHERE id=?", (d,)) for d in json.loads(t["deps"])]
        if any(d["status"] in ("failed", "blocked", "cancelled") for d in deps):
            return self._set(t, "blocked", report="a task it depends on did not finish")
        if all(d["status"] == "accepted" for d in deps):
            self._set(t, "ready")
            self._ready(self.s.one("SELECT * FROM tasks WHERE id=?", (t["id"],)))

    def _ready(self, t):
        if t["exclusive_group"] and self.s.one("SELECT 1 FROM tasks WHERE project=? AND exclusive_group=? AND status='running' AND id<>?",
                                               (t["project"], t["exclusive_group"], t["id"])):
            return                                   # incompatible work (the same group) never runs at the same time
        prompt, notes = self._assemble(t)
        shared = t["classification"] == "shared"
        ok, text, why, found = policy.check_dispatch(t["classification"], prompt, to_outside=shared)
        if not ok:
            return self._set(t, "blocked", report="Not sent: " + why)
        if t["needs_approval"] and not t["approved"]:
            return self._set(t, "needs-approval", report="Waiting for your approval before it is sent.")
        req = json.loads(t["requires"] or "{}")
        model = req.get("model")
        if not model:
            return self._set(t, "blocked", report="Not sent: no model was named for this task.")
        jid = self.c.submit_job("generate", {"model": model, "prompt": text}, project=t["project"], requires={"model": model},
                                ext_ok=shared)
        rep = ("; ".join(notes + ([f"removed from the text before sending: {', '.join(found)}"] if found else [])))
        self._set(t, "running", job_id=jid, started=time.time(), tries=t["tries"] + 1, report=rep)

    def _needs_approval(self, t):      # not advanced automatically: waits for a person
        return

    def _running(self, t):
        j = self.s.one("SELECT * FROM jobs WHERE id=?", (t["job_id"],))
        if j and j["status"] == "done":
            res = json.loads(j["result"] or "{}").get("response", "")
            return self._set(t, "review", result=res, assigned_to=j["node"], finished=time.time())
        if j and j["status"] == "failed":
            return self._set(t, "failed", report="The job failed: " + str(j["error"] or "unknown reason"))
        started = j["assigned"] or t["started"] if j else t["started"]
        if started and t["budget_s"] and time.time() - started > t["budget_s"]:
            if j:
                self.s.x("UPDATE jobs SET status='failed', error='over its time budget' WHERE id=?", (j["id"],))   # a late result is then refused
            return self._set(t, "failed", report=f"It took longer than its budget of {t['budget_s']:g} seconds and was stopped.")

    def _assemble(self, t):
        """The text sent out: the description, any feedback from an earlier review, and the accepted results of the tasks it depends on
        (a private result is never included in a task that may leave your computers)."""
        parts, notes = [t["description"]], []
        for fb in json.loads(t["feedback"] or "[]"):
            parts.append("A reviewer asked for this change: " + fb)
        ctx = []
        for d in json.loads(t["deps"]):
            dep = self.s.one("SELECT title, classification, result FROM tasks WHERE id=?", (d,))
            if dep["classification"] == "private" and t["classification"] == "shared":
                notes.append(f"the private result of '{dep['title']}' was not included")
                continue
            ctx.append(f"[{dep['title']}]\n{dep['result'] or ''}")
        if ctx:
            parts.append("Results of earlier steps:\n" + "\n\n".join(ctx))
        return "\n\n".join(parts), notes

    def _set(self, t, status, **kw):
        sets = ", ".join([f"status=?"] + [f"{k}=?" for k in kw])
        self.s.x(f"UPDATE tasks SET {sets} WHERE id=?", [status, *kw.values(), t["id"]])
        self.s.audit("task-" + status, task=t["id"], project=t["project"])

    # ---- people decide ----
    def approve(self, tid):
        t = self.s.one("SELECT * FROM tasks WHERE id=? AND status='needs-approval'", (tid,))
        if not t:
            return False
        self.s.x("UPDATE tasks SET approved=1, status='ready' WHERE id=?", (tid,))
        self.advance()
        return True

    def accept(self, tid):
        t = self.s.one("SELECT * FROM tasks WHERE id=? AND status='review'", (tid,))
        if not t:
            return False
        self._set(t, "accepted")
        self._close_project(t["project"])
        self.advance()
        return True

    def reject(self, tid, feedback=""):
        t = self.s.one("SELECT * FROM tasks WHERE id=? AND status='review'", (tid,))
        if not t:
            return False
        if t["tries"] >= MAX_TRIES:
            self._set(t, "failed", report="Rejected after the last allowed try.")
        else:
            fb = json.loads(t["feedback"] or "[]") + ([str(feedback)[:500]] if feedback else [])
            self.s.x("UPDATE tasks SET feedback=?, result=NULL WHERE id=?", (json.dumps(fb), tid))
            self._set(t, "ready")
        self.advance()
        return True

    def cancel(self, tid):
        t = self.s.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not t or t["status"] not in OPEN:
            return False
        if t["job_id"]:
            self.s.x("UPDATE jobs SET status='failed', error='cancelled' WHERE id=? AND status IN ('queued','assigned')", (t["job_id"],))
        self._set(t, "cancelled")
        return True

    def _close_project(self, pid):
        left = self.s.one("SELECT COUNT(*) c FROM tasks WHERE project=? AND status NOT IN ('accepted')", (pid,))["c"]
        self.s.x("UPDATE projects SET status=? WHERE id=?", ("done" if not left else "open", pid))

    # ---- looking ----
    def preview(self, t):
        """Exactly what would be sent out for this task (after secrets are removed). Shown to the person before they approve."""
        prompt, _ = self._assemble(t)
        shared = t["classification"] == "shared"
        ok, text, why, found = policy.check_dispatch(t["classification"], prompt, to_outside=shared)
        return {"text": text if ok else "", "refused": "" if ok else why, "removed": found}

    def projects(self):
        out = []
        for p in self.s.q("SELECT * FROM projects ORDER BY created DESC LIMIT 30"):
            ts = self.s.q("SELECT status FROM tasks WHERE project=?", (p["id"],))
            out.append({**p, "tasks": len(ts), "accepted": sum(1 for t in ts if t["status"] == "accepted"),
                        "waiting_for_you": sum(1 for t in ts if t["status"] in ("needs-approval", "review"))})
        return out

    def project_view(self, pid):
        p = self.s.one("SELECT * FROM projects WHERE id=?", (pid,))
        if not p:
            return None
        tasks = self.s.q("SELECT id,title,description,status,classification,priority,deps,assigned_to,report,tries,budget_s,needs_approval,result,requires,project FROM tasks WHERE project=? ORDER BY created", (pid,))
        for t in tasks:
            t["deps"] = json.loads(t["deps"])
            t["model"] = json.loads(t.pop("requires") or "{}").get("model")
            if t["status"] == "needs-approval":
                t["preview"] = self.preview(self.s.one("SELECT * FROM tasks WHERE id=?", (t["id"],)))
            if t["result"] and len(t["result"]) > 4000:
                t["result"] = t["result"][:4000] + " …(shortened)"
        done = sum(1 for t in tasks if t["status"] == "accepted")
        for t in tasks:
            t.pop("project", None)
        return {**p, "tasks": tasks, "progress": f"{done}/{len(tasks)}"}

    def workspace(self, pid):
        """The shared project workspace: the accepted results, in order."""
        return [{"task": t["id"], "title": t["title"], "result": t["result"], "by": t["assigned_to"]}
                for t in self.s.q("SELECT id,title,result,assigned_to FROM tasks WHERE project=? AND status='accepted' ORDER BY finished", (pid,))]

    def task_view(self, tid):
        return self.s.one("SELECT * FROM tasks WHERE id=?", (tid,))
