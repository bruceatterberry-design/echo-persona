#!/usr/bin/env python3
"""meshmcp: gives an interactive Claude Code session the Echo Mesh as extra hands (a tiny MCP server over stdio, standard library only).

mclaude starts this for you; it is not meant to be run by hand. Four tools:
  mesh_status    which computers are online and which models they have
  mesh_try_patch hand a small, well-tested piece of work to the mesh: several attempts are written in parallel on your computers, each one is tried in a
                 scratch COPY of the project, and the project's TESTS pick a passing one. Returns the diff. NOTHING in the real folder is changed.
  mesh_fanout    run one prompt template over many items in parallel (docstrings, summaries, test ideas, boilerplate) -> drafts
  mesh_ask_all   the same question to every computer -> several quick, unreliable second opinions
Rules built in: answers are DRAFTS from small models (3b/7b); the test command is fixed by whoever started mclaude (MESHMCP_TESTS), never chosen by a
tool call; secret-looking files and prompts are refused; the mesh receives only text and never runs anything; if the mesh is down every tool says so
in one line, and Claude simply carries on alone.
"""
import json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fanout as F
import meshcode as M

SECRET_TEXT = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(sk|pk|ghp|gho|xox[bap])-?[A-Za-z0-9_]{20,}|(?i:password|passwd|api[_-]?key|secret|token)\s*[:=]\s*\S{6,}")
DEFAULT_MODEL = "qwen2.5-coder:3b"
DOWN = "The Echo Mesh is not available right now. Do this yourself."
RAW_NOTE = "These are DRAFTS from small local models. Check every line before using it."


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def mesh_up():
    """-> (list of online node dicts, None) or ([], reason). Quick: a 3-second look at the coordinator."""
    try:
        import urllib.request
        token = open(F.TOKEN_FILE).read().strip()
        req = urllib.request.Request(F.ADMIN + "/status", headers={"Authorization": "Bearer " + token})
        nodes = json.loads(urllib.request.urlopen(req, timeout=3).read())["nodes"]
    except Exception as e:
        return [], f"{type(e).__name__}"
    on = [n for n in nodes if n["online"]]
    return on, None if on else "no computer is online"


def models_of(n):
    return (n.get("inventory", {}).get("ollama") or {}).get("models") or []


def t_status(args):
    on, why = mesh_up()
    if not on:
        return DOWN + f" ({why})"
    lines = [f"{n['name']}: {', '.join(models_of(n)) or 'no models'}" for n in on]
    return f"{len(on)} computer(s) online. " + RAW_NOTE + "\n" + "\n".join(lines)


def t_try_patch(args):
    on, why = mesh_up()
    if not on:
        return DOWN
    files = [str(f) for f in (args.get("files") or [])]
    task = str(args.get("task", "")).strip()
    root = os.environ.get("MESHMCP_ROOT") or os.getcwd()
    if not task or not (files or args.get("split")):
        return "Give a task and the files to change (names relative to the project folder)."
    bad = [f for f in files if M.SECRET_NAMES.search(f) or not M.safe_rel(root, f)]
    if bad:
        return f"Refused: {', '.join(bad)} is outside the project or looks like a secret."
    if SECRET_TEXT.search(task):
        return "Refused: the task text looks like it contains a secret."
    split = bool(args.get("split"))
    if not split:
        missing = [f for f in files if not os.path.isfile(os.path.join(root, f))]
        if missing:
            return f"No such file: {', '.join(missing)}"
    a = M.make_opts(task=task, files=files, tests=os.environ.get("MESHMCP_TESTS") or None, backend="mesh", split=split,
                    n=max(2, min(int(args.get("attempts", 6) or 6), 10)), rounds=2, mesh_timeout=150)
    lines = []
    t0 = time.time()
    try:
        res = M.run_task(a, root, lambda m: lines.append(str(m).strip()), None)
    except Exception as e:
        return f"The mesh run failed ({type(e).__name__}). Do this yourself."
    sp = M.speed_report(a, {"mesh": {"ok": res["ok"], "t": time.time() - t0, "state": "passed" if res["ok"] else "failed"}}, time.time() - t0)
    nodes = ", ".join(f"{n} x{v['n']}" for n, v in sp["mesh_nodes"].items())
    if not res["ok"] and res.get("partial"):
        return (f"NOT PASSING YET: the mesh got closer but did not finish ({res['note']}). Took {sp['wall_s']}s. Below is its closest attempt as a HEAD START: "
                f"it does not pass the project's tests, so read it, fix what is wrong and finish the work yourself. Nothing real was changed.\n{RAW_NOTE}\n\n{res['partial']['diff']}")
    if not res["ok"]:
        return f"The mesh did not find a change that passes the project's tests ({res['note']}). Took {sp['wall_s']}s. Do this yourself.\n" + "\n".join(lines[-6:])
    return (f"PASSING CANDIDATE from the mesh ({res['note']}). {sp['wall_s']}s, {sp['mesh_answered']}/{sp['mesh_attempts']} attempts answered by {nodes}.\n"
            f"The project's tests pass with this change in a scratch copy. NOTHING in the real folder has been changed: read the diff, then apply it yourself if it is right.\n{RAW_NOTE}\n\n{res['diff']}")


def t_fanout(args):
    on, why = mesh_up()
    if not on:
        return DOWN
    tmpl, items = str(args.get("prompt", "")), [str(i) for i in (args.get("items") or [])][:40]
    if "{item}" not in tmpl or not items:
        return "Give a prompt containing {item} and a list of items."
    if SECRET_TEXT.search(tmpl) or any(SECRET_TEXT.search(i) for i in items):
        return "Refused: the prompt or an item looks like it contains a secret."
    model = str(args.get("model") or DEFAULT_MODEL)
    if not any(model in models_of(n) for n in on):
        return f"No online computer has {model}."
    jobs, t0 = {}, time.time()
    for it in items:
        p = tmpl.replace("{item}", it)
        if len(p) > 19000:
            continue
        jobs[F.api("POST", "/jobs", {"type": "generate", "payload": {"model": model, "prompt": p}, "project": "mclaude", "requires": {"model": model}})["id"]] = it
    res = {}
    while len(res) < len(jobs) and time.time() - t0 < 120:
        for j in jobs:
            if j not in res:
                r = F.api("GET", f"/jobs/{j}")
                if r["status"] in ("done", "failed"):
                    res[j] = r
        time.sleep(0.5)
    out = [f"{len(res)}/{len(jobs)} answered in {time.time() - t0:.0f}s. {RAW_NOTE}"]
    for j, it in jobs.items():
        r = res.get(j)
        txt = json.loads(r["result"]).get("response", "").strip()[:1500] if r and r["status"] == "done" else "(no answer)"
        out.append(f"--- {it[:80]}\n{txt}")
    return "\n".join(out)


def t_ask_all(args):
    on, why = mesh_up()
    if not on:
        return DOWN
    q = str(args.get("question", "")).strip()
    if not q or SECRET_TEXT.search(q):
        return "Give a question (without secrets)."
    model = str(args.get("model") or DEFAULT_MODEL)
    jobs, t0 = {}, time.time()
    for n in on:
        if model in models_of(n):
            jobs[F.api("POST", "/jobs", {"type": "generate", "payload": {"model": model, "prompt": q}, "project": "mclaude", "requires": {"node": n["id"], "model": model}})["id"]] = n["name"]
    res = {}
    while len(res) < len(jobs) and time.time() - t0 < 90:
        for j in jobs:
            if j not in res:
                r = F.api("GET", f"/jobs/{j}")
                if r["status"] in ("done", "failed"):
                    res[j] = r
        time.sleep(0.5)
    out = [f"Second opinions from {len(res)} computer(s) in {time.time() - t0:.0f}s. Small models often agree and are still wrong: " + RAW_NOTE]
    for j, name in jobs.items():
        r = res.get(j)
        out.append(f"--- {name}\n" + (json.loads(r["result"]).get("response", "").strip()[:900] if r and r["status"] == "done" else "(no answer)"))
    return "\n".join(out)


TOOLS = {
    "mesh_status": (t_status, "Which Echo Mesh computers are online and which models they have. Cheap; call it first to see if the mesh can help.", {}),
    "mesh_try_patch": (t_try_patch, "Hand a SMALL, well-specified change to the user's Echo Mesh (several computers write attempts in parallel). Each attempt is tried in a scratch copy "
                       "and the project's own tests pick a passing one; you get the diff back and NOTHING real is changed. Best for one function or a few small files that have tests. "
                       "Several calls in the same message run side by side. If it returns 'did not find', do the work yourself.",
                       {"task": {"type": "string", "description": "exactly what to change, with the names the tests expect"}, "files": {"type": "array", "items": {"type": "string"}, "description": "files to change, relative to the project folder, each under about 6,000 characters"},
                        "split": {"type": "boolean", "description": "true to spread one task across several files (the mesh plans one job per file)"}, "attempts": {"type": "integer", "description": "parallel attempts per round, 2-10 (default 6)"}}),
    "mesh_fanout": (t_fanout, "Run one prompt template over many items in parallel on the Echo Mesh (docstrings, short summaries, test-case ideas, boilerplate, renames). Returns DRAFTS from small models; verify them.",
                    {"prompt": {"type": "string", "description": "prompt containing {item}"}, "items": {"type": "array", "items": {"type": "string"}, "description": "up to 40 items"}, "model": {"type": "string"}}),
    "mesh_ask_all": (t_ask_all, "Ask every online Echo Mesh computer the same question: several quick second opinions. Unreliable; never use for correctness or security decisions.",
                     {"question": {"type": "string"}, "model": {"type": "string"}}),
}
REQUIRED = {"mesh_try_patch": ["task"], "mesh_fanout": ["prompt", "items"], "mesh_ask_all": ["question"]}


def handle(msg):
    """One JSON-RPC message -> a response dict, or None for notifications."""
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        pv = (msg.get("params") or {}).get("protocolVersion") or "2024-11-05"
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": pv, "capabilities": {"tools": {}}, "serverInfo": {"name": "echo-mesh", "version": "1"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": n, "description": d, "inputSchema": {"type": "object", "properties": props, "required": REQUIRED.get(n, [])}} for n, (_, d, props) in TOOLS.items()]}}
    if method == "tools/call":
        p = msg.get("params") or {}
        fn = TOOLS.get(p.get("name"))
        if not fn:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
        try:
            text, err = fn[0](p.get("arguments") or {}), False
        except Exception as e:                               # a tool must never crash the server or Claude's session
            text, err = f"The mesh tool failed ({type(e).__name__}). Do this yourself.", True
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": err}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
    return None


def main():
    import threading
    lock = threading.Lock()

    def reply(msg):
        try:
            resp = handle(msg)
        except Exception:
            return
        if resp is not None:
            with lock:
                print(json.dumps(resp), flush=True)
    running = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("method") == "tools/call":
            t = threading.Thread(target=reply, args=(msg,), daemon=True)      # several calls in one message run side by side
            t.start(); running.append(t)
        else:
            reply(msg)
    for t in running:                                                           # stdin closed: let calls in flight finish and answer
        t.join(200)


if __name__ == "__main__":
    main()
