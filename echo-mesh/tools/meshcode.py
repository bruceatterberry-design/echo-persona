#!/usr/bin/env python3
"""meshcode: ask Claude Code to do a coding task; if Claude is down, out of usage or too slow, the Echo Mesh does it instead.

  cd ~/some-project            # a folder with tests (pytest by default)
  python3 ~/echo-mesh/tools/meshcode.py "make parse_duration handle '1h30m'" --files sol.py
  python3 ~/echo-mesh/tools/meshcode.py "..." --files a.py b.py --tests "pytest -q tests/test_a.py"
  python3 ~/echo-mesh/tools/meshcode.py "build the CLI: parser, store and report" --split --files cli.py store.py report.py
  python3 ~/echo-mesh/tools/meshcode.py "..." --backend mesh      # skip Claude, force the mesh
  python3 ~/echo-mesh/tools/meshcode.py "..." --backend claude    # Claude only, no fallback

How it works
  1. Claude first (`claude -p`, in a scratch COPY of your project). If it succeeds and your tests pass, that is the answer.
  2. If Claude fails (error, usage limit, timeout), the mesh takes over: several attempts are written IN PARALLEL on your computers,
     each attempt is tried in its own scratch copy, and YOUR TESTS decide which one is good (a small model is a poor judge, tests are not).
     If none pass, the best failing one is sent back with its test errors for another round.
  3. You see the diff and the test result. Nothing in your real folder changes until you answer y (or pass --apply).
SPREAD MODE (--split): for one big task over several files. A planner model first splits the task into one job per file, with the exact
  function/class names the files use to talk to each other (the "contract"). Then EVERY file gets its own parallel attempts at once, the
  attempts are tried together in scratch copies, and the number of failing tests decides what to keep. Round after round, the tests pull the
  files into line. Needs tests (count of failing tests is the guide) and at most 6 files, each up to ~6,000 characters.
The mesh only ever receives text (the files you name, the task, test errors) and sends text back. It never runs anything: the tests run here.
Mesh mode needs real tests (it cannot tell good from bad without them) and edits whole small files (--files, each up to ~6,000 characters).
"""
import argparse, difflib, json, os, re, shlex, shutil, subprocess, sys, tempfile, threading, time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fanout as F      # the Mesh admin-API helper

SECRET_NAMES = re.compile(r"(^|/)(\.env[^/]*|.*\.(pem|key|p12|pfx)|id_rsa[^/]*|.*secret.*|.*token.*|.*credentials.*)$", re.I)
MAX_FILE = 6000
MAX_SPLIT_FILES = 6
DEFAULT_MODELS = ["qwen2.5-coder:7b", "qwen2.5-coder:3b"]


class Cancelled(Exception):
    pass


def default_tests(split=False):
    return f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider" + ("" if split else " -x")


def make_opts(**kw):
    o = dict(task="", files=[], ref=[], tests=None, backend="race", full=False, stats=None, claude_cmd="claude", claude_timeout=600, models=list(DEFAULT_MODELS),
             n=6, rounds=3, mesh_timeout=240, split=False, test_timeout=60)
    o.update(kw)
    if not o["tests"]:
        o["tests"] = default_tests(o["split"])
    return SimpleNamespace(**o)


# ---------- the scratch copy ----------
def project_files(root):
    r = subprocess.run(["git", "-C", root, "ls-files", "-co", "--exclude-standard"], capture_output=True, text=True)
    if r.returncode == 0:
        return [f for f in r.stdout.splitlines() if os.path.isfile(os.path.join(root, f))]
    out = []
    for d, ds, fs in os.walk(root):
        ds[:] = [x for x in ds if x not in (".git", "node_modules", "__pycache__", ".venv", ".meshcode")]
        out += [os.path.relpath(os.path.join(d, f), root) for f in fs]
    return out


def scratch_copy(root):
    d = tempfile.mkdtemp(prefix="meshcode-")
    for f in project_files(root):
        if SECRET_NAMES.search(f) or f.startswith(".meshcode/"):
            continue                                        # a scratch copy never carries secrets
        dst = os.path.join(d, f); os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(root, f), dst)
    return d


def run_tests(cmd, cwd, timeout=180):
    """Run the project's test command. A model's code can loop forever, so on a timeout the WHOLE process group is killed, not just the shell."""
    import signal
    p = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode == 0, out
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        p.communicate()
        return False, f"tests took longer than {timeout}s (probably an endless loop in the new code)"


def score(ok, out):
    """How many tests still fail (0 = all pass). Read from pytest's final summary line ("3 failed, 2 passed in 0.1s"), wherever plugin
    warnings put it; any other test runner counts as 1 when it fails."""
    if ok:
        return 0
    for line in reversed(out.strip().splitlines()):
        if re.search(r"\bin [\d.]+s\b", line) and re.search(r"\d+ (?:failed|passed|errors?)", line):
            return sum(int(m.group(1)) for m in re.finditer(r"(\d+) (?:failed|errors?)\b", line)) or 1
    return 1


def fail_excerpt(out, limit=2500):
    """The useful end of a test run for a model to read: cut the plugin-warnings blob that pytest prints after the failures, then keep the last part."""
    cut = re.split(r"\n=+ warnings summary =+", out, maxsplit=1)[0]
    cut = re.sub(r"(?m)^(\s*warnings\.warn\(.*\)|.*PytestDeprecationWarning.*)$", "", cut)
    return cut.strip()[-limit:]


def changed(root, work):
    """{path: new_text} for files that differ between the real folder and a scratch copy."""
    out = {}
    for f in project_files(work):
        a, b = os.path.join(root, f), os.path.join(work, f)
        try:
            new = open(b, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        old = open(a, encoding="utf-8").read() if os.path.exists(a) else None
        if old != new:
            out[f] = new
    return out


def diff_text(root, files):
    parts = []
    for f, new in files.items():
        a = os.path.join(root, f)
        old = open(a, encoding="utf-8").read().splitlines(True) if os.path.exists(a) else []
        parts += difflib.unified_diff(old, new.splitlines(True), "a/" + f, "b/" + f)
    return "".join(parts)


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


# ---------- backend 1: Claude Code ----------
def try_claude(task, files, root, tests, a, cancel=None):
    """-> (ok, files_dict, note, workdir). ok False means 'use the mesh': Claude failed, hit a limit, or ran out of time."""
    work = scratch_copy(root)
    prompt = (f"{task}\n\nWork only inside this folder. Edit the files needed" + (f" (mainly: {', '.join(files)})" if files else "") +
              f". When done, run `{tests}` and make sure it passes. Reply with one short line saying what you changed.")
    cmd = [a.claude_cmd, "-p", prompt, "--permission-mode", "acceptEdits", "--allowedTools", "Read", "Edit", "Write", "Bash", "--no-session-persistence"]
    t0 = time.time()
    try:
        proc = subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except FileNotFoundError:
        return False, {}, f"'{a.claude_cmd}' is not installed or not on PATH", work
    box = {}
    th = threading.Thread(target=lambda: box.update(out=proc.communicate()[0]), daemon=True)
    th.start()
    while th.is_alive():
        th.join(0.5)
        if cancel is not None and cancel.is_set():
            proc.kill(); raise Cancelled()
        if time.time() - t0 > a.claude_timeout:
            proc.kill()
            return False, {}, f"Claude did not finish in {a.claude_timeout}s", work
    text = (box.get("out") or "").strip()
    if proc.returncode != 0 or re.search(r"usage limit|rate.?limit|overloaded|credit balance|not logged in|please run /login|ECONNREFUSED|ENOTFOUND", text, re.I):
        return False, {}, "Claude is not available: " + (text.splitlines()[-1][:160] if text else f"exit code {proc.returncode}"), work
    ok, out = run_tests(tests, work)
    return ok, changed(root, work), (f"Claude finished in {time.time() - t0:.0f}s: " + (text.splitlines()[-1][:160] if text else "")) + ("" if ok else "  [tests FAIL]"), work


# ---------- backend 2: the mesh ----------
def mesh_models(want):
    nodes = F.api("GET", "/status")["nodes"]
    have = {}
    for n in nodes:
        if n["online"]:
            for m in ((n.get("inventory", {}).get("ollama") or {}).get("models") or []):
                have[m] = have.get(m, 0) + 1
    use = [m for m in want if m in have]
    return use, have


def read_only_context(root, files, extra, budget=6000):
    """The tests (and any --ref files) are shown to the model but never changed. A model that cannot see what the tests expect is guessing."""
    cand = [f for f in project_files(root) if f not in files and (re.search(r"(^|/)(test_[^/]*|[^/]*_test)\.\w+$|(^|/)tests?/", f))] + list(extra)
    out, used = [], 0
    for f in dict.fromkeys(cand):
        if SECRET_NAMES.search(f):
            continue                                       # never even read it
        try:
            t = open(os.path.join(root, f), encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        if used + len(t) > budget:
            continue
        out.append(f"### READ-ONLY FILE (do not change): {f}\n```\n{t}\n```"); used += len(t)
    return "\n".join(out)


def build_prompt(task, files, texts, root, fail_output=None, prev=None, context=""):
    parts = [f"You are an expert programmer. TASK: {task}\n"]
    if context:
        parts.append(context + "\n")
    parts.append("Files to change:\n" + "\n".join(f"### FILE: {f}\n```\n{(prev or texts)[f]}\n```" for f in files))
    if fail_output:
        parts.append("\nThese tests FAIL with the code above. Fix the code so they pass:\n```\n" + fail_excerpt(fail_output) + "\n```")
    parts.append("\nReply with the COMPLETE new contents of each file, each as:\n### FILE: <path>\n```\n<whole file>\n```\nNo explanations.")
    return "\n".join(parts)


def parse_reply(text, files):
    got = {}
    for m in re.finditer(r"###\s*FILE:\s*(\S+)\s*\n```[a-zA-Z0-9]*\n(.*?)```", text, re.S):
        if m.group(1).strip("`") in files:
            got[m.group(1).strip("`")] = m.group(2)
    if not got and len(files) == 1:
        m = re.search(r"```[a-zA-Z0-9]*\n(.*?)```", text, re.S)
        if m:
            got[files[0]] = m.group(1)
    return got if set(got) == set(files) else {}


def parse_block(text):
    m = re.search(r"```[a-zA-Z0-9]*\n(.*?)```", text, re.S)
    return m.group(1) if m else None


def mesh_batch(prompts, models, nodes_by_id, timeout, cancel=None, stats=None):
    """Send many prompts at once; -> [(tag, model, node, text)] for the ones that came back. prompts = [(tag, prompt)]; models are used in turn."""
    jobs = {}
    for i, (tag, prompt) in enumerate(prompts):
        m = models[i % len(models)]
        jobs[F.api("POST", "/jobs", {"type": "generate", "payload": {"model": m, "prompt": prompt}, "project": "meshcode", "requires": {"model": m}})["id"]] = (tag, m)
    t0, res = time.time(), {}
    while len(res) < len(jobs) and time.time() - t0 < timeout:
        _check(cancel)
        for j in jobs:
            if j not in res:
                r = F.api("GET", f"/jobs/{j}")
                if r["status"] in ("done", "failed"):
                    res[j] = r
        time.sleep(1)
    out = []
    for j, r in res.items():
        if r["status"] != "done":
            continue
        d = json.loads(r["result"]); node = nodes_by_id.get(r["node"], "?")
        out.append((jobs[j][0], jobs[j][1], node, d.get("response", "")))
        if stats is not None:                              # for the speed report
            e = stats["nodes"].setdefault(node, {"n": 0, "tokens": 0, "gen_s": 0.0})
            e["n"] += 1; e["tokens"] += int(d.get("eval_count") or 0); e["gen_s"] += float(d.get("seconds") or 0)
    if stats is not None:
        stats["attempts"] += len(jobs); stats["answered"] += len(out)
    return out


def do_mesh(task, files, root, tests, a, log, cancel=None):
    models, have = mesh_models(a.models)
    if not models:
        return False, {}, "no online computer has any of: " + ", ".join(a.models)
    nodes = {n["id"]: n["name"] for n in F.api("GET", "/status")["nodes"]}
    texts = {f: open(os.path.join(root, f), encoding="utf-8").read() for f in files}
    ok0, base_out = run_tests(tests, root)
    best_fail = base_out if not ok0 else None
    if ok0:
        log("  note: the tests already pass before any change; the task may be about something they do not test.")
    prev, partial = None, {}
    context = read_only_context(root, files, a.ref)
    for rnd in range(1, a.rounds + 1):
        _check(cancel)
        prompt = build_prompt(task, files, texts, root, best_fail if rnd > 1 or not ok0 else None, prev, context)
        if len(prompt) > 19000:
            return False, {}, "the files plus the prompt are over 19,000 characters; name smaller files"
        log(f"  mesh round {rnd}: {a.n} attempts on {', '.join(models)} ...")
        cands = mesh_batch([(i, prompt) for i in range(a.n)], models, nodes, a.mesh_timeout, cancel, a.stats)
        log(f"    {len(cands)} answers back")
        passing, failing = [], []
        for _, model, node, text in cands:
            new = parse_reply(text, files)
            if not new or all(new[f].strip() == texts[f].strip() for f in files):
                continue
            work = scratch_copy(root)
            for f, t in new.items():
                open(os.path.join(work, f), "w", encoding="utf-8").write(t)
            ok, out = run_tests(tests, work, a.test_timeout)
            size = sum(len(l) for l in difflib.unified_diff(texts[files[0]].splitlines(), new[files[0]].splitlines()))
            (passing if ok else failing).append((size, {"node": node, "model": model}, new, out, work))
            log(f"    {node:<12} {model:<18} {'PASS' if ok else 'fail'}")
        if passing:
            passing.sort(key=lambda x: x[0])
            _, c, new, _, _ = passing[0]
            for t in passing + failing:
                shutil.rmtree(t[4], ignore_errors=True)
            return True, new, f"the mesh solved it in round {rnd}: {len(passing)} of {len(cands)} attempts passed; chose the smallest change, from {c['node']} ({c['model']})"
        if failing:
            failing.sort(key=lambda x: len(x[3]))       # shortest error output = probably the closest
            _, c, new, out, _ = failing[0]
            best_fail, prev, partial = out, new, new
        for t in failing:
            shutil.rmtree(t[4], ignore_errors=True)
    return False, partial, f"none of the mesh's attempts passed your tests after {a.rounds} rounds (the closest one is kept as a partial)"


# ---------- spread mode: one big task over several files ----------
def safe_rel(root, p):
    p = str(p).strip().replace("\\", "/")
    if not p or p.startswith("/") or ".." in p.split("/") or SECRET_NAMES.search(p) or p.startswith(".git/"):
        return None
    full = os.path.realpath(os.path.join(root, p))
    return p if full.startswith(os.path.realpath(root) + os.sep) else None


def make_plan(task, files, root, context, a, models, nodes, log, cancel):
    """One job per file plus the contract between them. -> [{path, job, api}]. A planner that fails falls back to 'same task, every file'."""
    listing = "\n".join(project_files(root)[:80])
    given = "\n".join(f"### FILE: {f}\n```\n{open(os.path.join(root, f), encoding='utf-8').read()[:2500]}\n```" for f in files if os.path.exists(os.path.join(root, f)))
    rule = (f"Use EXACTLY these files: {', '.join(files)}." if files else f"Choose at most {MAX_SPLIT_FILES} files (existing or new) to change.")
    prompt = (f"You are a software architect planning work for several programmers who each write ONE file and cannot talk to each other.\nTASK: {task}\n\n"
              f"Project files:\n{listing}\n\n{given}\n\n{context}\n\n{rule}\n"
              'Reply with ONLY JSON like {"files":[{"path":"a.py","job":"what this file must do","api":"exact names + signatures the OTHER files rely on, e.g. def parse(text) -> dict"}]}. '
              "Make the api lines precise: every file will be written from this plan alone.")
    plan = None
    for text in [t for _, _, _, t in mesh_batch([(i, prompt) for i in range(3)], models[:1], nodes, a.mesh_timeout, cancel, a.stats)]:
        try:
            d = json.loads(text[text.index("{"): text.rindex("}") + 1])
            out = []
            for e in d.get("files", []):
                p = safe_rel(root, e.get("path"))
                if p and (not files or p in files) and p not in [x["path"] for x in out]:
                    out.append({"path": p, "job": str(e.get("job", ""))[:400], "api": str(e.get("api", ""))[:600]})
            if out and (not files or {x["path"] for x in out} == set(files)):
                plan = out[:MAX_SPLIT_FILES]; break
        except (ValueError, AttributeError, TypeError):
            continue
    if not plan:
        if not files:
            return None
        log("  planner gave no usable plan: every file gets the whole task")
        plan = [{"path": f, "job": task, "api": ""} for f in files]
    return plan


def do_split(task, files, root, tests, a, log, cancel=None):
    models, have = mesh_models(a.models)
    if not models:
        return False, {}, "no online computer has any of: " + ", ".join(a.models)
    nodes = {n["id"]: n["name"] for n in F.api("GET", "/status")["nodes"]}
    files = [f for f in files]
    if len(files) > MAX_SPLIT_FILES:
        return False, {}, f"spread mode takes at most {MAX_SPLIT_FILES} files"
    context = read_only_context(root, files, a.ref)
    log("  planning the split ...")
    plan = make_plan(task, files, root, context, a, models, nodes, log, cancel)
    if not plan:
        return False, {}, "the planner could not split the task; name the files with --files"
    paths = [p["path"] for p in plan]
    for p in plan:
        log(f"    {p['path']}: {p['job'][:90]}")
    state = {}
    for p in paths:
        fp = os.path.join(root, p)
        state[p] = open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
        if len(state[p]) > MAX_FILE:
            return False, {}, f"{p} is over {MAX_FILE} characters"
    contract = "\n".join(f"- {p['path']}: {p['job']}" + (f"\n  API: {p['api']}" if p["api"] else "") for p in plan)

    def run_state(st):
        work = scratch_copy(root)
        for f, t in st.items():
            os.makedirs(os.path.dirname(os.path.join(work, f)) or work, exist_ok=True)
            open(os.path.join(work, f), "w", encoding="utf-8").write(t)
        ok, out = run_tests(tests, work, a.test_timeout)
        shutil.rmtree(work, ignore_errors=True)
        return ok, out

    ok, out = run_state(state)
    cur = 0 if ok else score(ok, out)
    log(f"  start: {'all tests pass' if ok else str(cur) + ' failing'}")
    orig, start = dict(state), cur
    if ok:
        return True, {}, "the tests already pass before any change"
    k = max(2, a.n // 2)
    for rnd in range(1, a.rounds + 1):
        _check(cancel)
        prompts = []
        for p in plan:
            others = "\n".join(f"### {q['path']} (current)\n```\n{state[q['path']][:1500]}\n```" for q in plan if q["path"] != p["path"])
            body = (f"You are an expert programmer on a team. Several files are written at the same time by different people from one plan. "
                    f"YOUR file is {p['path']}. Follow the plan's API lines EXACTLY so the other files work with yours.\n\nOVERALL TASK: {task}\n\nTHE PLAN:\n{contract}\n\n"
                    f"{context}\n\n{others}\n\nYOUR FILE {p['path']} now:\n```\n{state[p['path']]}\n```\n\n")
            if out:
                body += "The tests currently fail like this:\n```\n" + fail_excerpt(out, 2200) + "\n```\n\n"
            body += f"Reply with the COMPLETE new contents of {p['path']} in ONE code block and nothing else."
            if len(body) > 19000:
                return False, {}, "the plan, tests and files are over 19,000 characters; use fewer or smaller files"
            prompts += [((p["path"], i), body) for i in range(k)]
        log(f"  round {rnd}: {len(prompts)} attempts ({k} per file) on {', '.join(models)} ...")
        got = mesh_batch(prompts, models, nodes, a.mesh_timeout, cancel, a.stats)
        by = {p: [] for p in paths}
        for (path, i), model, node, text in got:
            new = parse_block(text)
            if new and new.strip() and new.strip() != state[path].strip():
                by[path].append({"i": i, "text": new, "node": node, "model": model})
        log(f"    {len(got)} answers back, usable per file: " + ", ".join(f"{p}={len(v)}" for p, v in by.items()))
        # 1. each attempt alone with the other files as they are now: a ranking for each file
        for p, cs in by.items():
            for c in cs:
                _check(cancel)
                ok1, out1 = run_state({**state, p: c["text"]})
                c["score"] = 0 if ok1 else score(ok1, out1)
            cs.sort(key=lambda c: (c["score"], len(c["text"])))
        # 2. attempts together: the i-th best attempt of every file as one set (files that depend on each other only work as a set)
        combos, seen = [], set()
        for i in range(k):
            pick = {p: cs[min(i, len(cs) - 1)]["text"] for p, cs in by.items() if cs}
            key = tuple(sorted((p, hash(t)) for p, t in pick.items()))
            if pick and key not in seen:
                seen.add(key); combos.append(pick)
        best = None
        for pick in combos:
            _check(cancel)
            st = {**state, **pick}
            ok2, out2 = run_state(st)
            sc = 0 if ok2 else score(ok2, out2)
            if best is None or sc < best[0]:
                best = (sc, st, out2)
            log(f"    set {len(seen) and combos.index(pick) + 1}/{len(combos)}: {'ALL PASS' if ok2 else str(sc) + ' failing'}")
            if ok2:
                break
        if best and best[0] < cur:
            cur, state, out = best
            log(f"    kept: {cur} failing" if cur else "    kept: all tests pass")
        else:
            log("    nothing got better this round")
        if cur == 0:
            return True, {p: t for p, t in state.items()}, f"spread over {len(plan)} files; the tests pass after round {rnd}"
    better = {p: t for p, t in state.items() if t != orig[p]} if cur < start else {}
    return False, better, f"after {a.rounds} rounds {cur} test(s) still fail (started with {start}); nothing is applied" + ("; the closest state is kept as a partial" if better else "")


def mesh_job(a, root, log, cancel):
    """The mesh side as one call. -> (ok, files, note)."""
    if not a.split:
        big = [f for f in a.files if os.path.getsize(os.path.join(root, f)) > MAX_FILE]
        if big:
            return False, {}, f"For the mesh, files must be under {MAX_FILE} characters: {', '.join(big)}"
    return (do_split if a.split else do_mesh)(a.task, a.files, root, a.tests, a, log, cancel)


def race(a, root, log, cancel):
    """Claude and the mesh start together; the first one whose result passes the tests wins and the other is stopped (a.full lets both finish,
    so you can compare their real times). -> {tag: {ok, files, note, t, state}}."""
    t0, lock = time.time(), threading.Lock()
    ev = {"claude": threading.Event(), "mesh": threading.Event()}
    out = {}

    def say(tag):
        def f(m):
            with lock:
                log(f"[{tag} +{time.time() - t0:4.0f}s] {str(m).strip()}")
        return f

    def run(tag):
        try:
            if tag == "claude":
                ok, files, note, work = try_claude(a.task, a.files, root, a.tests, a, ev[tag])
                shutil.rmtree(work, ignore_errors=True)
                say(tag)(note)
                ok = bool(ok and files)
            else:
                ok, files, note = mesh_job(a, root, say(tag), ev[tag])
                say(tag)(note)
            out[tag] = {"ok": ok, "files": files, "note": note, "t": time.time() - t0, "state": "passed" if ok else "failed"}
        except Cancelled:
            out[tag] = {"ok": False, "files": {}, "note": "stopped", "t": time.time() - t0, "state": "stopped"}
        except Exception as e:                              # one side crashing must never take the other down
            out[tag] = {"ok": False, "files": {}, "note": f"{type(e).__name__}: {e}", "t": time.time() - t0, "state": "failed"}
    ths = [threading.Thread(target=run, args=(t,), daemon=True) for t in ev]
    for t in ths:
        t.start()
    while any(t.is_alive() for t in ths):
        time.sleep(0.2)
        if cancel is not None and cancel.is_set():
            for e in ev.values():
                e.set()
        if not a.full:
            for tag, r in list(out.items()):
                if r["ok"]:
                    for other, e in ev.items():
                        if other != tag and other not in out:
                            e.set()
    return out


def speed_report(a, rr, wall):
    st = a.stats or {"attempts": 0, "answered": 0, "nodes": {}}
    by = {n: {"n": e["n"], "tok_s": round(e["tokens"] / e["gen_s"], 1) if e["gen_s"] else None} for n, e in st["nodes"].items()}
    sp = {"wall_s": round(wall, 1), "mesh_attempts": st["attempts"], "mesh_answered": st["answered"], "mesh_nodes": by, "claude": None, "mesh": None, "winner": None, "saved_s": None}
    for tag in ("claude", "mesh"):
        if tag in rr:
            sp[tag] = {"state": rr[tag]["state"], "s": round(rr[tag]["t"], 1)}
    winners = sorted((r["t"], tag) for tag, r in rr.items() if r["ok"])
    if winners:
        sp["winner"] = winners[0][1]
        if len(winners) > 1:
            sp["saved_s"] = round(winners[1][0] - winners[0][0], 1)       # only known when both were allowed to finish (--full)
    return sp


def run_task(a, root, log=print, cancel=None):
    """The whole job. -> {"ok","used","note","files","diff","speed"}. Nothing in `root` is ever written here."""
    t0 = time.time()
    res = {"ok": False, "used": None, "note": "", "files": {}, "diff": "", "speed": {}}
    if getattr(a, "stats", None) is None:
        a.stats = {"attempts": 0, "answered": 0, "nodes": {}}
    bad = [f for f in a.files if SECRET_NAMES.search(f)]
    if bad:
        res["note"] = f"Refusing: {', '.join(bad)} looks like a secret. Nothing is sent anywhere."; return res
    for f in a.files:
        if not os.path.isfile(os.path.join(root, f)) and not a.split:
            res["note"] = f"No such file: {f}"; return res
    backend = a.backend
    if backend == "race" and not (a.files or a.split):
        backend = "auto"                                  # the mesh needs to know which files; without them it can only be Claude-then-nothing
    if backend == "race":
        rr = race(a, root, log, cancel)
        win = sorted((r["t"], tag) for tag, r in rr.items() if r["ok"])
        res["speed"] = speed_report(a, rr, time.time() - t0)
        if win:
            tag = win[0][1]
            res.update(ok=True, used=tag if tag == "claude" else ("mesh-split" if a.split else "mesh"), note=rr[tag]["note"], files=rr[tag]["files"], diff=diff_text(root, rr[tag]["files"]))
        else:
            res["note"] = "Neither Claude nor the mesh produced a change that passes the tests. " + " | ".join(f"{t}: {r['note']}" for t, r in rr.items())
        return res
    tag = None
    if backend in ("auto", "claude"):
        log("Claude Code first...")
        t1 = time.time()
        try:
            ok, files, note, work = try_claude(a.task, a.files, root, a.tests, a, cancel)
        except Cancelled:
            res["note"] = "Cancelled."; return res
        shutil.rmtree(work, ignore_errors=True)
        log("  " + note)
        res["speed"]["claude"] = {"state": "passed" if ok and files else "failed", "s": round(time.time() - t1, 1)}
        if ok and files:
            res.update(ok=True, used="claude", note=note, files=files, diff=diff_text(root, files), speed={**res["speed"], "winner": "claude", "wall_s": round(time.time() - t0, 1)}); return res
        if backend == "claude":
            res["note"] = "Claude did not produce a passing change, and --backend claude means no fallback."; return res
        log("Falling back to the Echo Mesh.")
    if not a.files and not a.split:
        res["note"] = "The mesh needs --files (the files to change)."; return res
    t1 = time.time()
    try:
        ok, files, note = mesh_job(a, root, log, cancel)
    except Cancelled:
        res["note"] = "Cancelled."; return res
    sp = speed_report(a, {"mesh": {"ok": ok, "t": time.time() - t1, "state": "passed" if ok else "failed"}}, time.time() - t0)
    sp["claude"] = res["speed"].get("claude")
    res.update(ok=ok, used="mesh-split" if a.split else "mesh", note=note, files=files if ok else {}, diff=diff_text(root, files) if ok else "", speed=sp)
    if not ok and files:
        res["partial"] = {"files": files, "diff": diff_text(root, files)}           # a head start, never an applied result
    return res


def print_speed(sp):
    if not sp:
        return
    print("\n=== Speed ===")
    for tag, label in (("claude", "Claude Code"), ("mesh", "Echo Mesh  ")):
        r = sp.get(tag)
        if r:
            print(f"  {label}: {r['s']:>6.1f}s  ({r['state']})")
    if sp.get("mesh_attempts"):
        nodes = ", ".join(f"{n} x{v['n']}" + (f" @{v['tok_s']} tok/s" if v["tok_s"] else "") for n, v in sp["mesh_nodes"].items())
        print(f"  Mesh work : {sp['mesh_answered']}/{sp['mesh_attempts']} attempts answered by {len(sp['mesh_nodes'])} computer(s): {nodes}")
    if sp.get("winner"):
        extra = f", {sp['saved_s']}s faster than the other" if sp.get("saved_s") else ""
        print(f"  Winner    : {sp['winner']}{extra}.  Total wall time {sp['wall_s']}s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task")
    ap.add_argument("--files", nargs="*", default=[], help="files the task is about (needed for the mesh; Claude can find its own)")
    ap.add_argument("--ref", nargs="*", default=[], help="extra files the mesh may READ for context (never changed)")
    ap.add_argument("--tests", default=None, help="command that runs your tests (exit 0 = pass)")
    ap.add_argument("--backend", choices=["race", "auto", "claude", "mesh"], default="race",
                    help="race (default): Claude and the mesh start together, first passing result wins; auto: Claude first, mesh only if Claude fails; or force one")
    ap.add_argument("--full", action="store_true", help="in a race, let both finish so you can compare their real times (no early stop)")
    ap.add_argument("--split", action="store_true", help="spread one big task across several files (see above)")
    ap.add_argument("--claude-cmd", default="claude")
    ap.add_argument("--claude-timeout", type=int, default=600)
    ap.add_argument("--models", nargs="*", default=DEFAULT_MODELS, help="mesh models to use, tried in turn")
    ap.add_argument("-n", type=int, default=6, help="attempts per mesh round (default 6)")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--mesh-timeout", type=int, default=240)
    ap.add_argument("--apply", action="store_true", help="apply a passing result without asking")
    a = ap.parse_args()
    apply_now = a.apply
    a = make_opts(**{k: v for k, v in vars(a).items() if k != "apply"})
    root = os.getcwd()
    res = run_task(a, root)
    if not res["ok"]:
        print_speed(res["speed"])
        sys.exit(res["note"] or 1)
    print("  " + res["note"])
    print(f"\n=== Result from {res['used']}: {len(res['files'])} file(s) changed, tests PASS ===\n{res['diff']}")
    print_speed(res["speed"])
    os.makedirs(".meshcode", exist_ok=True)
    open(".meshcode/last.patch", "w").write(res["diff"])
    if apply_now or (sys.stdin.isatty() and input("Apply this to your folder? [y/N] ").strip().lower() == "y"):
        for f, t in res["files"].items():
            os.makedirs(os.path.dirname(os.path.join(root, f)) or ".", exist_ok=True)
            open(os.path.join(root, f), "w", encoding="utf-8").write(t)
        print("Applied.")
    else:
        print("Not applied. The change is saved in .meshcode/last.patch (git apply .meshcode/last.patch).")


if __name__ == "__main__":
    main()
