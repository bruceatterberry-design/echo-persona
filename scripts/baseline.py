"""Stage 0 baseline suite (cognitive-architecture directive): the CONTROL GROUP every later stage is compared against.

Runs a fixed question set through the REAL reply pipeline (create_app + the configured local LLM) on a *copy* of the
memory database, a fresh conversation per question, and records per question: time to first word, total time, reply
tokens, tokens/s, prompt tokens (context size), number of local-LLM and embedding calls, external (non-localhost)
requests, plus Mac-wide resource samples (RSS + CPU of Ollama / Python / voice services, swap). Scoring is objective
where possible (reasoning: expected answer regex; identity: names itself as the owner / digital self); the "unknowns" set
(invention check) is saved for review by hand: it is NOT auto-scored.

    .venv/bin/python scripts/baseline.py <label> [--repeat 1] [--only simple,reasoning,...]
        → docs/bench/baseline-<label>.json   (answers about the owner's life stay out of git: see --private)

Never edits live data: memory DB is copied (SQLite backup API) and the copy deleted at the end.
Not measurable here: GPU utilisation / VRAM (Apple unified memory; powermetrics needs sudo), per-token streaming
timestamps inside Ollama (first-word time is measured at the WebSocket, as the loved one would experience it).
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx                                    # noqa: E402
from fastapi.testclient import TestClient     # noqa: E402

from core.config import load_config            # noqa: E402
from core.llm import OllamaBackend             # noqa: E402
from core.server import create_app             # noqa: E402
from memory.store import MemoryStore           # noqa: E402
from scripts.bench_invention import QUESTIONS as UNKNOWNS   # noqa: E402

SUITE_VERSION = "baseline-v1"

# Fixed sets. Change only by making a new SUITE_VERSION (old results stay comparable to their own version).
SIMPLE = ["Good morning!", "How are you today?", "What's your favorite season?", "Tell me something nice.",
          "Thanks, that's all for now.", "Did you sleep well?"]
REASONING = [   # (question, regex an answer must match: objective, not judged by Claude)
    ("If I have 3 boxes with 12 eggs each and I break 5, how many eggs are left?", r"\b31\b|thirty[- ]one"),
    ("A train leaves at 2:45 PM and the trip takes 1 hour 50 minutes. What time does it arrive?", r"4:35|four thirty[- ]five"),
    ("What's 15 percent of 80?", r"\b12\b|twelve"),
    ("All the birds in the cage are blue. Tweety is in the cage. What color is Tweety?", r"\bblue\b"),
    ("Which is heavier, a pound of feathers or a pound of bricks?", r"same|equal|identical|neither|both weigh"),
    ("If today is Wednesday, what day will it be in 10 days?", r"saturday"),
    ("I'm older than Sam, and Sam is older than Pat. Who is the youngest?", r"\bpat\b"),
    ("How many minutes are in three and a half hours?", r"\b210\b|two hundred (and )?ten"),
    ("A shirt costs 40 dollars and is 25 percent off. What's the sale price?", r"\$?\b30\b|thirty"),
    ("What's the next number: 2, 4, 8, 16?", r"\b32\b|thirty[- ]two"),
    ("If 5 machines take 5 minutes to make 5 widgets, how long would 100 machines take to make 100 widgets?",
     r"\b5 minutes|five minutes"),
    ("Sally has 3 brothers. Each brother has 2 sisters. How many sisters does Sally have?",
     r"\bone\b|\b1\b|only sister|just one"),
]
IDENTITY = [("Who are you?", r"owner|digital"), ("What's your name?", r"owner|echo|echo"),
            ("Are you a real person?", r"digital|not (a )?real|ai|computer|program")]


class Counter:
    """Counts HTTP requests by kind via httpx's send (sync + async): local LLM chat, embeddings, other local, external."""
    def __init__(self):
        self.n = {"llm_chat": 0, "llm_generate": 0, "embed": 0, "local_other": 0, "external": 0}
        self.lock = threading.Lock()

    def note(self, url: httpx.URL):
        host, path = url.host, url.path
        local = host in ("127.0.0.1", "localhost", "::1", "testserver")
        k = ("llm_chat" if path.endswith("/api/chat") else "llm_generate" if path.endswith("/api/generate")
             else "embed" if "/api/embed" in path else "local_other" if local else "external")
        if host == "testserver":
            return
        with self.lock:
            self.n[k] += 1

    def snap(self) -> dict:
        with self.lock:
            return dict(self.n)

    def install(self):
        c = self
        o_sync, o_async = httpx.Client.send, httpx.AsyncClient.send

        def s(self_, *a, **k):
            c.note((a[0] if a else k["request"]).url)
            return o_sync(self_, *a, **k)

        async def s2(self_, *a, **k):
            c.note((a[0] if a else k["request"]).url)
            return await o_async(self_, *a, **k)
        httpx.Client.send, httpx.AsyncClient.send = s, s2


class Sampler(threading.Thread):
    """Every second: RSS (MB) and CPU% summed per group, from `ps` (psutil isn't installed; nothing new added)."""
    GROUPS = {"ollama": re.compile(r"ollama|llama-server", re.I), "voice_tts_stt": re.compile(r"tts/|stt_service|service\.py"),
              "python_core": re.compile(r"uvicorn|core\.server|baseline\.py")}

    def __init__(self):
        super().__init__(daemon=True)
        self.rows, self.stop = [], threading.Event()

    def run(self):
        while not self.stop.is_set():
            out = subprocess.run(["ps", "-axo", "pid=,rss=,%cpu=,command="], capture_output=True, text=True).stdout
            g = {k: [0.0, 0.0] for k in self.GROUPS}
            pid_groups: dict[str, list[str]] = {k: [] for k in self.GROUPS}
            for line in out.splitlines():
                p = line.split(None, 3)
                if len(p) < 4:
                    continue
                for k, rx in self.GROUPS.items():
                    if rx.search(p[3]):
                        g[k][0] += int(p[1]) / 1024
                        g[k][1] += float(p[2])
                        pid_groups[k].append(p[0])
                        break
            if len(self.rows) % 5 == 0:   # real private memory (RSS also counts the model file cached in RAM)
                for k, pids in pid_groups.items():
                    g[k].append(footprint_mb(pids))
            sw = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
            m = re.search(r"used = ([\d.]+)M", sw)
            self.rows.append({k: {"rss_mb": round(v[0]), "cpu": round(v[1], 1),
                                  **({"footprint_mb": v[2]} if len(v) > 2 and v[2] is not None else {})}
                              for k, v in g.items()}
                             | {"swap_used_mb": float(m.group(1)) if m else None})
            self.stop.wait(1.0)

    def summary(self) -> dict:
        s = {}
        for k in self.GROUPS:
            rss = [r[k]["rss_mb"] for r in self.rows]
            cpu = [r[k]["cpu"] for r in self.rows]
            fp = [r[k]["footprint_mb"] for r in self.rows if "footprint_mb" in r[k]]
            if rss:
                s[k] = {"rss_mb_median": round(statistics.median(rss)), "rss_mb_max": max(rss),
                        "cpu_pct_median": round(statistics.median(cpu), 1), "cpu_pct_max": max(cpu),
                        **({"footprint_mb_median": round(statistics.median(fp)), "footprint_mb_max": max(fp)} if fp else {})}
        sw = [r["swap_used_mb"] for r in self.rows if r["swap_used_mb"] is not None]
        if sw:
            s["swap_used_mb"] = {"start": sw[0], "max": max(sw), "end": sw[-1]}
        s["samples"] = len(self.rows)
        return s


def footprint_mb(pids: list[str]) -> float | None:
    """macOS phys_footprint (private memory really in use) summed over pids; RSS overstates a model whose weights are a
    memory-mapped file (2026-11-01: Ollama RSS 7.5 GB vs footprint 3.06 GB)."""
    total, seen = 0.0, False
    for pid in pids:
        out = subprocess.run(["footprint", "-p", pid], capture_output=True, text=True).stdout
        m = re.search(r"phys_footprint:\s*([\d.]+)\s*([KMG])B", out)
        if m:
            total += float(m.group(1)) * {"K": 1 / 1024, "M": 1, "G": 1024}[m.group(2)]
            seen = True
    return round(total) if seen else None


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, max(0, int(round(p * (len(xs) - 1)))))], 2)


def stats(xs: list[float]) -> dict:
    return {"n": len(xs), "mean": round(statistics.mean(xs), 2) if xs else None,
            "median": round(statistics.median(xs), 2) if xs else None, "p95": pct(xs, .95)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("label")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--only", default="simple,reasoning,identity,unknowns,memory")
    ap.add_argument("--db", default="data/memory.sqlite")
    ap.add_argument("--memory-db", default="data/backups/memory-20261101-120140-stage0.sqlite",
                    help="frozen memory for the retrieval test (same data every stage; live memory keeps changing)")
    ap.add_argument("--private", default="data/eval", help="where answers mentioning the owner's life are written")
    a = ap.parse_args()
    only = set(a.only.split(","))
    cfg = load_config()
    counter = Counter()
    counter.install()

    work = ROOT / "data" / "eval" / f"baseline-{a.label}.sqlite"
    work.parent.mkdir(parents=True, exist_ok=True)
    work.unlink(missing_ok=True)
    src, dst = sqlite3.connect(f"file:{ROOT / a.db}?mode=ro", uri=True), sqlite3.connect(work)
    src.backup(dst)
    dst.close()
    src.close()
    store = MemoryStore(work)
    backend = OllamaBackend(cfg.llm)
    app = create_app(cfg, backend, store)
    c = TestClient(app)   # not as a context manager: no background jobs, no speech models loaded

    sets = []
    if "simple" in only:
        sets += [("simple", q, None) for q in SIMPLE]
    if "reasoning" in only:
        sets += [("reasoning", q, rx) for q, rx in REASONING]
    if "identity" in only:
        sets += [("identity", q, rx) for q, rx in IDENTITY]
    if "unknowns" in only:
        sets += [("unknowns", q, None) for q in UNKNOWNS]

    sampler = Sampler()
    sampler.start()
    results = []
    t_all = time.perf_counter()
    for rep in range(a.repeat):
        for kind, q, rx in sets:
            store.clear_turns()
            before = counter.snap()
            t = time.perf_counter()
            first, reply, err = None, "", None
            try:
                with c.websocket_connect("/ws") as ws:
                    ws.receive_json()
                    ws.send_json({"text": q})
                    while True:
                        m = ws.receive_json()
                        if m["type"] == "delta" and first is None:
                            first = time.perf_counter() - t
                        if m["type"] == "reply":
                            reply = m["text"]
                        if m["type"] == "error":
                            err = f"server: {m.get('message')}"
                        if m["type"] == "state" and m["state"] == "idle":
                            break
            except Exception as e:      # recorded, never hidden
                err = f"{type(e).__name__}: {e}"
            total = time.perf_counter() - t
            after = counter.snap()
            st = backend.last_stats or {}
            tps = (st.get("reply_tokens", 0) / (st["reply_ms"] / 1000)) if st.get("reply_ms") else None
            r = {"set": kind, "rep": rep, "q": q, "a": reply, "error": err,
                 "first_s": round(first, 2) if first else None, "total_s": round(total, 2),
                 "prompt_tokens": st.get("prompt_tokens"), "reply_tokens": st.get("reply_tokens"),
                 "tokens_per_s": round(tps, 1) if tps else None,
                 "calls": {k: after[k] - before[k] for k in after}}
            if rx:
                r["correct"] = bool(re.search(rx, reply, re.I))
            results.append(r)
            mark = "" if rx is None else (" OK" if r["correct"] else " MISS")
            print(f"[{kind}{mark}] {q}\n   → {reply[:160]!r}\n   first {r['first_s']} s, total {r['total_s']} s, "
                  f"{r['tokens_per_s']} tok/s, prompt {r['prompt_tokens']} tok, calls {r['calls']}", flush=True)
    sampler.stop.set()
    sampler.join()

    memory_out = None
    if "memory" in only:
        frozen = ROOT / "data" / "eval" / f"memory-frozen-{a.label}.sqlite"
        s_, d_ = sqlite3.connect(f"file:{ROOT / a.memory_db}?mode=ro", uri=True), sqlite3.connect(frozen)
        s_.backup(d_)
        d_.close()
        s_.close()
        p = subprocess.run([sys.executable, "scripts/bench_memory.py", "--db", str(frozen)], cwd=ROOT,
                           capture_output=True, text=True,
                           env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})
        memory_out = {"db": a.memory_db, "returncode": p.returncode, "stdout_tail": p.stdout.splitlines()[-25:],
                      "stderr_tail": p.stderr.splitlines()[-5:]}
        for ext in ("", "-wal", "-shm"):
            Path(str(frozen) + ext).unlink(missing_ok=True)

    def agg(kind=None):
        rs = [r for r in results if (kind is None or r["set"] == kind) and not r["error"]]
        out = {"first_s": stats([r["first_s"] for r in rs if r["first_s"]]),
               "total_s": stats([r["total_s"] for r in rs]),
               "tokens_per_s": stats([r["tokens_per_s"] for r in rs if r["tokens_per_s"]]),
               "prompt_tokens": stats([r["prompt_tokens"] for r in rs if r["prompt_tokens"]]),
               "llm_calls_per_q": stats([r["calls"]["llm_chat"] + r["calls"]["llm_generate"] for r in rs]),
               "embed_calls_per_q": stats([r["calls"]["embed"] for r in rs]),
               "external_requests": sum(r["calls"]["external"] for r in rs),
               "errors": sum(1 for r in results if (kind is None or r["set"] == kind) and r["error"])}
        scored = [r for r in rs if "correct" in r]
        if scored:
            out["accuracy"] = f"{sum(r['correct'] for r in scored)}/{len(scored)}"
        return out

    summary = {"all": agg()} | {k: agg(k) for k in ("simple", "reasoning", "identity", "unknowns")
                                if any(r["set"] == k for r in results)}
    git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    meta = {"suite": SUITE_VERSION, "label": a.label, "date": time.strftime("%Y-%m-%dT%H:%M:%S"), "git": git,
            "model": cfg.llm.model, "temperature": cfg.llm.temperature, "think": getattr(cfg.llm, "think", None),
            "machine": f"{platform.machine()} macOS {platform.mac_ver()[0]}", "repeat": a.repeat,
            "wall_s": round(time.perf_counter() - t_all, 1),
            "not_measured": ["GPU utilisation / VRAM (unified memory; needs sudo powermetrics)",
                             "unknowns set is not auto-scored: hallucination rate needs review by hand"]}
    pub = {"meta": meta, "summary": summary, "resources": sampler.summary(), "memory_retrieval": memory_out,
           "per_question": [{k: v for k, v in r.items() if k != "a"} | ({"a": r["a"]} if r["set"] in ("reasoning",)
                                                                         else {}) for r in results]}
    (ROOT / "docs" / "bench" / f"baseline-{a.label}.json").write_text(json.dumps(pub, indent=1))
    (ROOT / a.private / f"baseline-{a.label}-answers.json").write_text(json.dumps({"meta": meta, "results": results},
                                                                                   indent=1))
    work.unlink(missing_ok=True)
    for ext in ("-wal", "-shm"):
        Path(str(work) + ext).unlink(missing_ok=True)
    print(json.dumps(summary, indent=1))
    print("resources:", json.dumps(sampler.summary()))


if __name__ == "__main__":
    main()
