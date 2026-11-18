"""Optional "Speed up my AI" step of the installers (owner says yes; nothing happens otherwise, nothing is downloaded).

What it can really do, and what it cannot:
  * SPECULATIVE DECODING: a small "draft" model guesses the next 5-16 words, the main model checks them all at once. The text is identical to what the
    main model alone would write; only the speed changes. It needs llama.cpp's server (llama-server): Ollama has no draft-model setting that this
    step can rely on. Both models must fit in memory together and come from the same family (same tokenizer).
  * TUNING: tries a few batch / thread / GPU-layer settings with a warm-up run first (a cold start reports misleading numbers) and keeps the fastest.
  * MULTI-TOKEN PREDICTION (the main model predicts several words itself) needs a model trained for it (DeepSeek V3/R1, Qwen3-Next, GLM-4.5) and a
    server that supports it (vLLM today; llama.cpp not yet). It is only ever REPORTED here, never switched on.
Pure functions plus one small file (speedup.json in the node's folder). Standard library only, no shell strings (commands are argument lists).
"""
import json, os, re, shutil, subprocess, time

FILE = "speedup.json"
DRAFT_MAX, DRAFT_MIN = 16, 5
MTP_FAMILIES = ("deepseek-v3", "deepseek-r1", "qwen3-next", "glm-4.5")
OVERHEAD_BYTES = int(1.5 * 1024 ** 3)           # context cache, buffers, the system itself
GIB = 1024 ** 3

# the knobs tried (kept small so the step finishes in minutes, not hours)
SPACE = {"batch": (512, 1024, 2048, 4096), "ubatch": (256, 512, 1024), "threads": None, "gpu_layers": None, "flash": (0, 1)}


def family(name):
    """'llama3.2:1b' -> 'llama3.2'; 'Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf' -> 'qwen2.5-coder'. Used only to match a draft to its main model."""
    n = os.path.basename(str(name)).lower()
    n = re.sub(r"\.gguf$", "", n)
    n = n.split(":")[0]
    n = re.sub(r"[-_ ](\d+(\.\d+)?)b\b.*$", "", n)          # drop size and everything after it
    n = re.sub(r"[-_ ](instruct|chat|q\d.*)$", "", n)
    return n.strip("-_ ")


def tokenizer_group(name):
    """Models can draft for each other only when they share a tokenizer. Minor versions of some families do (Llama 3.x, Qwen 2.x); anything not listed
    must match exactly, which is the safe side: a missed pairing costs speed, a wrong one breaks the output."""
    f = family(name)
    return re.sub(r"^(llama\d)\.\d+", r"\1", re.sub(r"^(qwen2)\.\d+", r"\1", f))


def size_b(name):
    """Parameter count in billions from a name like 'llama3.2:1b' / '...-70B-...'; None when not stated."""
    m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)b\b", str(name).lower())
    return float(m.group(1)) if m else None


def mtp_note(name):
    """A plain sentence when the model could do multi-token prediction, else None. Informational only."""
    n = str(name).lower().replace("_", "-")
    if any(f in n for f in MTP_FAMILIES):
        return (f"{name} was trained for multi-token prediction, which is faster still, but it needs a server that supports it (vLLM today, "
                "llama.cpp not yet). Echo Mesh does not switch it on; it is noted here for later.")
    return None


def plan(main, candidates, mem_bytes, bytes_of):
    """Decide what is possible. main: model name; candidates: names of other installed models; mem_bytes: GPU memory if a GPU does the work,
    else the RAM we may use; bytes_of(name) -> file size. Returns {"mode": "speculative"|"tuning-only", "draft": name|None, "reason": plain text}."""
    fam, msize = tokenizer_group(main), size_b(main)
    better = []
    for c in candidates:
        if c == main or tokenizer_group(c) != fam:
            continue
        cs = size_b(c)
        if msize is not None and cs is not None and cs >= msize:
            continue                                         # a draft must be smaller than the main model
        better.append(c)
    better.sort(key=lambda c: (size_b(c) is None, size_b(c) or 0))
    if not better:
        return {"mode": "tuning-only", "draft": None,
                "reason": f"No smaller model of the same family as {main} is installed, so there is nothing to use as a draft. Tuning can still help."}
    for c in better:
        need = bytes_of(main) + bytes_of(c) + OVERHEAD_BYTES
        if need <= mem_bytes:
            return {"mode": "speculative", "draft": c, "reason": f"{c} can draft for {main}: about {need / GIB:.1f} GB needed, {mem_bytes / GIB:.1f} GB available."}
    c = better[0]
    need = bytes_of(main) + bytes_of(c) + OVERHEAD_BYTES
    return {"mode": "tuning-only", "draft": None,
            "reason": f"{main} and the draft {c} together need about {need / GIB:.1f} GB but only {mem_bytes / GIB:.1f} GB is available, so the draft is skipped."}


def ollama_blob(model, root=None):
    """The GGUF file behind an Ollama model name ('llama3.2:1b'), read from Ollama's own manifest. None if not found. Never downloads."""
    root = root or os.environ.get("OLLAMA_MODELS") or os.path.join(os.path.expanduser("~"), ".ollama", "models")
    name, _, tag = model.partition(":")
    parts = name.split("/")
    if len(parts) == 1:
        host, ns, nm = "registry.ollama.ai", "library", parts[0]
    elif len(parts) == 2:
        host, ns, nm = "registry.ollama.ai", parts[0], parts[1]
    else:
        host, ns, nm = parts[0], parts[1], "/".join(parts[2:])
    try:
        with open(os.path.join(root, "manifests", host, ns, nm, tag or "latest")) as f:
            layers = json.load(f).get("layers", [])
        for L in layers:
            if L.get("mediaType") == "application/vnd.ollama.image.model":
                p = os.path.join(root, "blobs", L["digest"].replace(":", "-"))
                return p if os.path.isfile(p) else None
    except (OSError, ValueError, KeyError):
        return None
    return None


def find_llama(names=("llama-server", "llama-server.exe")):
    """Where llama-server is installed, or None. The installer does not install it."""
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def server_command(llama_server, main_gguf, draft_gguf=None, port=9999, settings=None, ctx=8192):
    """The argument list to start llama-server (never joined into a shell string)."""
    s = settings or {}
    cmd = [llama_server, "-m", main_gguf, "--port", str(port), "-c", str(ctx)]
    if s.get("threads"):
        cmd += ["-t", str(int(s["threads"]))]
    if s.get("batch"):
        cmd += ["--batch-size", str(int(s["batch"]))]
    if s.get("ubatch"):
        cmd += ["--ubatch-size", str(int(s["ubatch"]))]
    if s.get("gpu_layers") is not None:
        cmd += ["-ngl", str(int(s["gpu_layers"]))]
    if s.get("flash"):
        cmd += ["--flash-attn"]
    if draft_gguf:
        cmd += ["-md", draft_gguf, "-ngld", "99", "--draft-max", str(DRAFT_MAX), "--draft-min", str(DRAFT_MIN)]
    return cmd


def bench_command(llama_bench, gguf, settings, reps=1):
    """llama-bench for text generation speed (the number that matters for chat). JSON output so it can be read safely."""
    s = settings or {}
    cmd = [llama_bench, "-m", gguf, "-p", "0", "-n", "64", "-r", str(reps), "-o", "json"]
    for flag, key in (("-t", "threads"), ("-b", "batch"), ("-ub", "ubatch"), ("-ngl", "gpu_layers")):
        if s.get(key) is not None:
            cmd += [flag, str(int(s[key]))]
    if s.get("flash") is not None:
        cmd += ["-fa", str(int(bool(s["flash"])))]
    return cmd


def parse_bench(text):
    """tokens/second from llama-bench -o json; None if it cannot be read."""
    try:
        rows = json.loads(text)
        v = [float(r["avg_ts"]) for r in rows if int(r.get("n_gen", 0)) > 0]
        return max(v) if v else None
    except (ValueError, KeyError, TypeError):
        return None


def real_trial(llama_bench, gguf, timeout=300):
    """A trial function for tune(): runs llama-bench once with the given settings."""
    def trial(settings):
        try:
            r = subprocess.run(bench_command(llama_bench, gguf, settings), capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return parse_bench(r.stdout) if r.returncode == 0 else None
    return trial


def default_space(cpu_count=None, gpu=False, layers=None):
    cpu = cpu_count or os.cpu_count() or 4
    sp = dict(SPACE)
    sp["threads"] = tuple(sorted({max(1, cpu // 4), max(1, cpu // 2), max(1, cpu - 1)}))
    sp["gpu_layers"] = tuple(sorted({0, (layers or 99) // 2, layers or 99})) if gpu else (0,)
    return sp


def tune(trial, space, max_trials=24, warmup=True, clock=time.time, budget_s=900):
    """Coordinate search: start from the first value of every knob, then try each knob's other values one at a time, keeping any improvement.
    trial(settings) -> tokens/sec or None (a failed setting is skipped). The warm-up run is thrown away. Stops at max_trials or budget_s.
    -> {"settings", "tps", "baseline", "trials"}; settings is None when nothing ran."""
    t0 = clock()
    cur = {k: v[0] for k, v in space.items() if v}
    if warmup:
        trial(dict(cur))
    base = trial(dict(cur))
    n, best = 1, base
    if base is None:
        return {"settings": None, "tps": None, "baseline": None, "trials": n}
    for k, vals in space.items():
        for v in (vals or ())[1:]:
            if n >= max_trials or clock() - t0 > budget_s:
                return {"settings": cur, "tps": best, "baseline": base, "trials": n}
            n += 1
            t = trial({**cur, k: v})
            if t is not None and t > best:
                best, cur = t, {**cur, k: v}
    return {"settings": cur, "tps": best, "baseline": base, "trials": n}


# ---- the owner's saved result (one small file in the node's folder) ----
def load(d):
    try:
        with open(os.path.join(d, FILE)) as f:
            x = json.load(f)
        return x if isinstance(x, dict) else {}
    except (OSError, ValueError):
        return {}


def save(d, data):
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, FILE)
    with open(p + ".tmp", "w") as f:
        json.dump(data, f, indent=1)
    os.replace(p + ".tmp", p)


def summary(plan_, tuned=None):
    """Plain words for the owner."""
    lines = [plan_["reason"]]
    if tuned and tuned.get("settings") is not None and tuned.get("baseline"):
        gain = (tuned["tps"] / tuned["baseline"] - 1) * 100
        lines.append(f"Tuning: {tuned['baseline']:.1f} -> {tuned['tps']:.1f} tokens per second ({gain:+.0f}%), {tuned['trials']} trials.")
    elif tuned is not None:
        lines.append("Tuning could not run here, so nothing was changed.")
    return "\n".join(lines)


# ---- the installer step (shared by the Linux installer and the Windows wizard: written once) ----
def usable_memory():
    """(bytes, 'GPU'|'RAM'): the first NVIDIA GPU's memory if there is one, otherwise 70% of RAM. Apple and AMD GPUs are not guessed, so they count as RAM."""
    from . import inventory
    for g in inventory.gpus():
        m = re.match(r"\s*(\d+)\s*MiB", str(g.get("vram") or ""))
        if m:
            return int(m.group(1)) * 1024 * 1024, "GPU"
    r = inventory.ram_bytes()
    return (int(r * 0.7) if r else 0), "RAM"


def offer(d, ask=input, out=None, models=None, find=find_llama, trial_for=None, mem=None):
    """Optional step. Nothing runs unless the owner answers yes. It never downloads or installs anything, and never starts a server by itself.
    models: names of the models in Ollama (found by the installer). trial_for(llama_bench, gguf) -> trial fn (tests pass a fake).
    Returns the saved dict, or None when skipped."""
    import sys
    out = out or sys.stdout
    w = out.write
    w("\nOptional: speed up your AI (about 2-5x faster replies on some computers, same words as before).\n")
    if (ask("  Try it? Nothing is downloaded and nothing changes unless it works. [y/N]: ") or "").strip().lower() not in ("y", "yes"):
        w("  Skipped. You can run this step again later.\n")
        return None
    srv = find()
    bench = (os.path.join(os.path.dirname(srv), "llama-bench" + (".exe" if srv.lower().endswith(".exe") else "")) if srv else None)
    if not srv:
        w("  This needs llama.cpp (its llama-server program), which is not installed here, and Echo Mesh does not install it for you.\n"
          "  When you have it (github.com/ggml-org/llama.cpp, Releases), run this step again.\n")
        return None
    models = models or []
    ggufs = {m: ollama_blob(m) for m in models}
    ggufs = {m: p for m, p in ggufs.items() if p}
    if not ggufs:
        w("  I could not find any Ollama model files to use. Nothing changed.\n")
        return None
    main = max(ggufs, key=lambda m: (size_b(m) or 0, m))
    memory, kind = mem or usable_memory()
    pl = plan(main, list(ggufs), memory, lambda n: os.path.getsize(ggufs[n]))
    w(f"  Main model: {main}\n  {pl['reason']}\n")
    note = mtp_note(main)
    if note:
        w("  " + note + "\n")
    tuned = None
    if bench and os.path.exists(bench) and (ask("  Test a few speed settings? It takes a few minutes. [y/N]: ") or "").strip().lower() in ("y", "yes"):
        trial = (trial_for or real_trial)(bench, ggufs[main])
        tuned = tune(trial, default_space(gpu=(kind == "GPU")))
    elif not (bench and os.path.exists(bench)):
        w("  llama-bench was not found next to llama-server, so the speed settings are not tested.\n")
    w("  " + summary(pl, tuned).replace("\n", "\n  ") + "\n")
    settings = (tuned or {}).get("settings") or {}
    data = {"enabled": True, "main": main, "draft": pl["draft"], "mode": pl["mode"], "settings": settings,
            "baseline_tps": (tuned or {}).get("baseline"), "tuned_tps": (tuned or {}).get("tps"), "at": int(time.time()),
            "command": server_command(srv, ggufs[main], ggufs.get(pl["draft"]) if pl["draft"] else None, settings=settings)}
    save(d, data)
    w("  Saved. The start command for llama-server is in " + os.path.join(d, FILE) + ". Echo Mesh does not start it for you.\n")
    return data
