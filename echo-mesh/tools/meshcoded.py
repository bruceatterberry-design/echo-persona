#!/usr/bin/env python3
"""meshcoded: the small local service behind the Echo Mesh plugin's Coder screen.

Why it exists: a coding run takes minutes (Claude Code, then the mesh), and a plugin call is a short sandboxed process that cannot wait for
one. This service runs meshcode.run_task in the background, keeps the log, the diff and the speed numbers, and applies a result only when asked.

  127.0.0.1:47840 only, bearer token = the Mesh admin token (~/.echo-mesh/admin.token). One run at a time.
  GET  /status | /projects | /projects/<id>/files | /runs | /runs/<id>
  POST /projects {path, tests, name} | /projects/<id>/remove | /runs {project, task, files, backend, split, full}
       /runs/<id>/cancel | /runs/<id>/apply | /runs/<id>/discard
Safety rules (enforced here, not by a prompt):
  * a project must be a folder under your home folder that you registered; the test command is stored WITH the project, never taken from a run
  * a run works in scratch copies; your real files change only on /apply, and only if they are still exactly what they were when the run started
  * secret-looking files are never sent or copied; the mesh only ever receives text and never runs anything
"""
import hashlib, hmac, json, os, re, shutil, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meshcode as M

PORT = int(os.environ.get("MESHCODED_PORT", "47840"))
STATE = os.path.expanduser(os.environ.get("MESHCODED_DIR", "~/.echo-mesh/coder"))
TOKEN_FILE = os.path.expanduser(os.environ.get("ECHO_MESH_TOKEN", "~/.echo-mesh/admin.token"))
HOME = os.path.realpath(os.path.expanduser("~"))
FORBIDDEN = [os.path.join(HOME, d) for d in (".ssh", ".echo-mesh", "Library", ".gnupg", ".aws", ".config")]
BACKENDS = ("race", "auto", "mesh", "claude")
MAX_LOG = 400


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Coder:
    def __init__(self, state_dir, runner=None):
        self.dir = state_dir
        os.makedirs(os.path.join(state_dir, "runs"), exist_ok=True)
        self.lock = threading.RLock()
        self.runner = runner or M.run_task            # tests swap this for a fake
        self.projects = self._load("projects.json", {})
        self.runs, self.current, self.cancel = {}, None, None
        for fn in sorted(os.listdir(os.path.join(state_dir, "runs"))):
            if fn.endswith(".json"):
                r = self._load(os.path.join("runs", fn), None)
                if r:
                    if r["status"] == "running":
                        r["status"] = "interrupted"
                    self.runs[r["id"]] = r

    # ---- storage ----
    def _load(self, rel, default):
        try:
            return json.load(open(os.path.join(self.dir, rel)))
        except (OSError, ValueError):
            return default

    def _save(self, rel, obj):
        tmp = os.path.join(self.dir, rel + ".tmp")
        json.dump(obj, open(tmp, "w"))
        os.replace(tmp, os.path.join(self.dir, rel))

    def _save_run(self, r):
        self._save(os.path.join("runs", r["id"] + ".json"), r)

    # ---- projects ----
    def add_project(self, path, tests="", name=""):
        p = os.path.realpath(os.path.expanduser(str(path or "")))
        if not os.path.isdir(p) or not p.startswith(HOME + os.sep) or p == HOME:
            raise ValueError("The project must be an existing folder inside your home folder (not the home folder itself).")
        if any(p == f or p.startswith(f + os.sep) for f in FORBIDDEN):
            raise ValueError("That folder is not allowed.")
        tests = str(tests or "").strip()
        if len(tests) > 300 or "\n" in tests:
            raise ValueError("The test command is one line, up to 300 characters.")
        with self.lock:
            for pr in self.projects.values():
                if pr["path"] == p:
                    pr.update(tests=tests, name=(name or pr["name"])[:60]); self._save("projects.json", self.projects); return pr["id"]
            pid = "c-" + uuid.uuid4().hex[:8]
            self.projects[pid] = {"id": pid, "name": (str(name).strip() or os.path.basename(p))[:60], "path": p, "tests": tests}
            self._save("projects.json", self.projects)
        return pid

    def remove_project(self, pid):
        with self.lock:
            ok = self.projects.pop(pid, None) is not None
            self._save("projects.json", self.projects)
        return ok

    def project_files(self, pid):
        pr = self.projects.get(pid)
        if not pr:
            raise KeyError("no such project")
        out = []
        for f in M.project_files(pr["path"]):
            if M.SECRET_NAMES.search(f) or f.startswith(".meshcode/") or "/__pycache__/" in "/" + f:
                continue
            try:
                out.append({"path": f, "size": os.path.getsize(os.path.join(pr["path"], f))})
            except OSError:
                pass
            if len(out) >= 400:
                break
        return out

    # ---- runs ----
    def start_run(self, req):
        pr = self.projects.get(str(req.get("project", "")))
        if not pr:
            raise ValueError("Pick a registered project first.")
        task = str(req.get("task", "")).strip()
        if not task or len(task) > 2000:
            raise ValueError("Write the task (up to 2,000 characters).")
        backend = str(req.get("backend", "race"))
        if backend not in BACKENDS:
            raise ValueError("Unknown mode.")
        split = bool(req.get("split"))
        files = [str(f) for f in (req.get("files") or [])][:12]
        for f in files:
            if M.SECRET_NAMES.search(f) or not M.safe_rel(pr["path"], f):
                raise ValueError(f"'{f}' is not allowed (outside the project, or it looks like a secret).")
            if not split and not os.path.isfile(os.path.join(pr["path"], f)):
                raise ValueError(f"No such file in the project: {f}")
        if backend != "claude" and not files and not split:
            raise ValueError("Choose the files the task is about (the mesh needs them).")
        with self.lock:
            if self.current:
                raise RuntimeError("busy")
            rid = "r-" + uuid.uuid4().hex[:8]
            base = {}
            for f in files:
                fp = os.path.join(pr["path"], f)
                base[f] = sha(open(fp, encoding="utf-8").read()) if os.path.exists(fp) else ""
            r = {"id": rid, "project": pr["id"], "project_name": pr["name"], "task": task, "files": files, "backend": backend, "split": split, "full": bool(req.get("full")),
                 "status": "running", "started": time.time(), "finished": None, "log": [], "result": None, "base": base, "note": "", "applied": False}
            self.runs[rid] = r
            self.current, self.cancel = rid, threading.Event()
            self._save_run(r)
        threading.Thread(target=self._work, args=(rid, pr, self.cancel), daemon=True).start()
        return rid

    def _work(self, rid, pr, cancel):
        r = self.runs[rid]

        def log(msg):
            with self.lock:
                r["log"].append(f"{time.strftime('%H:%M:%S')} {str(msg).rstrip()}"[:400])
                del r["log"][:-MAX_LOG]
        try:
            a = M.make_opts(task=r["task"], files=r["files"], tests=pr["tests"] or None, backend=r["backend"], split=r["split"], full=r["full"])
            res = self.runner(a, pr["path"], log, cancel)
            with self.lock:
                r["result"] = {k: res.get(k) for k in ("ok", "used", "note", "files", "diff", "speed")}
                r["note"] = res.get("note", "")
                r["status"] = "cancelled" if cancel.is_set() else ("done" if res.get("ok") else "failed")
        except Exception as e:                                  # a crash must show up as a failed run, never kill the service
            with self.lock:
                r["status"], r["note"] = "failed", f"{type(e).__name__}: {e}"
        finally:
            with self.lock:
                r["finished"] = time.time()
                self._save_run(r)
                self.current = None

    def cancel_run(self, rid):
        with self.lock:
            if self.current == rid and self.cancel:
                self.cancel.set(); return True
        return False

    def apply_run(self, rid):
        with self.lock:
            r = self.runs.get(rid)
            if not r or r["status"] != "done" or r["applied"] or not r.get("result") or not r["result"].get("files"):
                raise ValueError("That run has no result waiting to be applied.")
            pr = self.projects.get(r["project"])
            if not pr:
                raise ValueError("The project is no longer registered.")
            files = r["result"]["files"]
            for f in files:
                if not M.safe_rel(pr["path"], f):
                    raise ValueError(f"Refusing to write {f}.")
                fp = os.path.join(pr["path"], f)
                now = sha(open(fp, encoding="utf-8").read()) if os.path.exists(fp) else ""
                if f in r["base"] and now != r["base"][f] or f not in r["base"] and os.path.exists(fp) and now != sha(files[f]):
                    raise RuntimeError(f"{f} has changed since this run started. Run it again.")
            for f, text in files.items():
                fp = os.path.join(pr["path"], f)
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                open(fp, "w", encoding="utf-8").write(text)
            r["applied"] = True
            self._save_run(r)
        return sorted(files)

    def discard_run(self, rid):
        with self.lock:
            r = self.runs.get(rid)
            if not r or r["status"] == "running":
                return False
            r["result"] = None if not r["result"] else {**r["result"], "files": {}, "diff": ""}
            r["status"] = "discarded" if r["status"] == "done" else r["status"]
            self._save_run(r)
        return True

    # ---- views ----
    def summary(self, r):
        res = r.get("result") or {}
        return {"id": r["id"], "project": r["project_name"], "task": r["task"][:140], "status": r["status"], "backend": r["backend"], "split": r["split"],
                "started": r["started"], "finished": r["finished"], "used": res.get("used"), "note": (r.get("note") or "")[:300], "applied": r["applied"],
                "files": sorted(res.get("files") or {}), "speed": res.get("speed") or {}}

    def detail(self, rid):
        r = self.runs.get(rid)
        if not r:
            return None
        with self.lock:
            return {**self.summary(r), "log": list(r["log"][-200:]), "diff": ((r.get("result") or {}).get("diff") or "")[:60000]}

    def listing(self):
        with self.lock:
            return [self.summary(r) for r in sorted(self.runs.values(), key=lambda x: -x["started"])[:20]]


class Handler(BaseHTTPRequestHandler):
    coder = None
    token = ""

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)

    def _auth(self):
        got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return hmac.compare_digest(got.encode(), self.token.encode())

    def _go(self, method):
        if not self._auth():
            return self._send(401, {"error": "token required"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(min(n, 200000)) or b"{}") if n else {}
        except ValueError:
            return self._send(400, {"error": "bad request"})
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        c = self.coder
        try:
            if method == "GET" and parts == ["status"]:
                busy = c.current
                return self._send(200, {"ok": True, "busy": busy, "claude": bool(shutil.which("claude")), "python": sys.executable})
            if method == "GET" and parts == ["projects"]:
                return self._send(200, list(c.projects.values()))
            if method == "POST" and parts == ["projects"]:
                return self._send(200, {"id": c.add_project(body.get("path"), body.get("tests"), body.get("name", ""))})
            if len(parts) == 3 and parts[0] == "projects" and parts[2] == "files" and method == "GET":
                return self._send(200, c.project_files(parts[1]))
            if len(parts) == 3 and parts[0] == "projects" and parts[2] == "remove" and method == "POST":
                return self._send(200, {"ok": c.remove_project(parts[1])})
            if method == "GET" and parts == ["runs"]:
                return self._send(200, c.listing())
            if method == "POST" and parts == ["runs"]:
                return self._send(200, {"id": c.start_run(body)})
            if len(parts) >= 2 and parts[0] == "runs":
                if method == "GET" and len(parts) == 2:
                    d = c.detail(parts[1])
                    return self._send(200, d) if d else self._send(404, {"error": "no such run"})
                if method == "POST" and len(parts) == 3:
                    if parts[2] == "cancel":
                        return self._send(200, {"ok": c.cancel_run(parts[1])})
                    if parts[2] == "apply":
                        return self._send(200, {"ok": True, "written": c.apply_run(parts[1])})
                    if parts[2] == "discard":
                        return self._send(200, {"ok": c.discard_run(parts[1])})
            return self._send(404, {"error": "not found"})
        except KeyError as e:
            return self._send(404, {"error": str(e)})
        except RuntimeError as e:
            return self._send(409, {"error": "Another run is still going. Wait for it or cancel it." if str(e) == "busy" else str(e)})
        except ValueError as e:
            return self._send(400, {"error": str(e)})

    def do_GET(self):
        self._go("GET")

    def do_POST(self):
        self._go("POST")


def serve(port=PORT, state=STATE, token=None, runner=None):
    tok = token or open(TOKEN_FILE).read().strip()
    H = type("H", (Handler,), {"coder": Coder(state, runner), "token": tok})
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    return srv, H.coder


if __name__ == "__main__":
    srv, _ = serve()
    print(f"meshcoded up on 127.0.0.1:{srv.server_address[1]}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
