"""Daydream training, step 6: the overnight run on this Mac (the owner, 2026-11-04). Started by launchd every night
(com.echopersona.daydreamtrain, 02:30); does nothing unless every condition below holds.

  1. config/echo.yaml daydream.training.enabled, inside the night window (01:00–05:00), nobody talked to Echo in the
     last 30 minutes, and at least training.min_examples kept examples (core/daydream_train.py: never Claude's words).
  2. Train a small LoRA on Echo's own model with MLX (.venv-train; base mlx-community/Qwen3.5-9B-4bit, MLP blocks only,
     answers only: the prompt isn't learned).
  3. Bake it into a copy of Echo's Ollama model (core/lora_gguf.py merge; Ollama 0.40 refuses separate adapters) and
     import it as echo-trained-<date>.
  4. Safety gate: measure the current model and the candidate the same way (scripts/baseline.py reasoning, identity,
     unknowns; scripts/daydream_set.py --echo-only: test-set accuracy + personality). Switch only if the candidate is
     better on reasoning or the test set and not worse on identity, invention or personality. Otherwise delete it.
  5. Switched → config/echo.yaml llm.model changes and core restarts; "undo training" goes back.
If anyone talks to Echo while it runs, it stops and throws the candidate away. Everything goes to
docs/bench/daydream/training-log.jsonl.

    .venv/bin/python scripts/daydream_train.py [--now] [--dry-run] [--iters N]
      --now      ignore the night window (still refuses while someone is talking to Echo)
      --dry-run  stop after exporting the examples
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import daydream, daydream_train as T  # noqa: E402

TRAIN_PY = ROOT / ".venv-train" / "bin" / "python"
ECHO_PY = ROOT / ".venv" / "bin" / "python"
BASE_MLX = "mlx-community/Qwen3.5-9B-4bit"
OLLAMA = "http://127.0.0.1:11434"
NIGHT = (1, 5)            # hours: start at or after 01:00, before 05:00
QUIET_MIN = 30            # minutes since anyone last talked to Echo
MAX_SEQ = 768             # tokens per training example (16 GB Mac; longer answers are cut)
LORA_YAML = """lora_parameters:
  keys: ["mlp.gate_proj", "mlp.up_proj", "mlp.down_proj"]
  rank: 8
  scale: 20.0
  dropout: 0.0
"""


def last_talk_minutes() -> float | None:
    import sqlite3
    try:
        db = sqlite3.connect(f"file:{ROOT / 'data' / 'memory.sqlite'}?mode=ro", uri=True)
        ts = db.execute("SELECT max(ts) FROM turns").fetchone()[0]
        return (time.time() - ts) / 60 if ts else None
    except sqlite3.Error:
        return None


class Interrupted(Exception):
    pass


def quiet_or_stop(since: float) -> None:
    m = last_talk_minutes()
    if m is not None and m * 60 < time.time() - since:
        raise Interrupted(f"someone talked to Echo {m:.0f} min ago")


def run(argv: list, log_to: Path, env: dict | None = None, timeout: int = 4 * 3600) -> None:
    with open(log_to, "a") as f:
        f.write(f"\n$ {' '.join(map(str, argv))}\n")
        f.flush()
        r = subprocess.run([str(a) for a in argv], cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
                           env={**os.environ, **(env or {})}, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"{Path(str(argv[1])).name if len(argv) > 1 else argv[0]} failed ({r.returncode}); see {log_to}")


def ollama(path: str, body: dict, timeout: int = 600) -> dict:
    req = urllib.request.Request(OLLAMA + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def modelfile_for(current: str, gguf: Path) -> str:
    """Echo's own Modelfile (template, renderer/parser, vision part, parameters) with the weights swapped."""
    text = subprocess.run(["ollama", "show", "--modelfile", current], capture_output=True, text=True, check=True).stdout
    out, swapped = [], False
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith("FROM ") and not swapped:
            out.append(f"FROM {gguf}")
            swapped = True
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def base_gguf(current: str) -> Path:
    text = subprocess.run(["ollama", "show", "--modelfile", current], capture_output=True, text=True, check=True).stdout
    return Path(next(line.split(" ", 1)[1] for line in text.splitlines() if line.startswith("FROM /")))


def measure(model: str, work: Path, who: str, log_to: Path) -> dict:
    """The same numbers for any model: baseline (reasoning, identity, unknowns) + test set (Echo alone)."""
    env = {"ECHO_LLM_MODEL": model}
    label = f"train-{work.name}-{who}"
    run([ECHO_PY, "scripts/baseline.py", label, "--only", "reasoning,identity,unknowns", "--private", str(work)],
        log_to, env)
    pub_p = ROOT / "docs" / "bench" / f"baseline-{label}.json"
    pub = json.loads(pub_p.read_text())
    shutil.move(pub_p, work / pub_p.name)                    # keep nightly runs out of docs/bench
    answers = json.loads((work / f"baseline-{label}-answers.json").read_text())
    nums = T.baseline_numbers(pub, answers)
    out = work / f"set-{who}"
    run([ECHO_PY, "scripts/daydream_set.py", "--echo-only", "--repeat", "1", "--force", "--out", str(out)], log_to, env)
    s = json.loads(sorted(out.glob("set-*.json"))[-1].read_text())["modes"]["echo"]
    nums |= {"daydream_set": s.get("accuracy"), "personality": s.get("personality")}
    return nums


def prune(keep: int, versions: T.Versions) -> list[str]:
    """Delete old echo-trained-* models, keeping the current one and the last `keep` it could go back to."""
    st = versions.state()
    protect = {versions.current(), *st.get("previous", [])[:keep]}
    with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=30) as r:
        names = [m["name"] for m in json.loads(r.read())["models"] if m["name"].startswith("echo-trained-")]
    keep_base = {p.split(":")[0] for p in protect}
    gone = [n for n in names if n.split(":")[0] not in keep_base]
    for n in gone:
        subprocess.run(["ollama", "rm", n], capture_output=True)
    return gone


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true", help="ignore the night window")
    ap.add_argument("--dry-run", action="store_true", help="stop after exporting the examples")
    ap.add_argument("--iters", type=int, default=0, help="training steps (default: two passes over the examples)")
    a = ap.parse_args()
    dcfg = daydream.load_config(daydream.CONFIG)
    tr = dcfg.get("training", {})

    def skip(why: str) -> int:
        T.log({"event": "skipped", "why": why})
        print("skipped:", why)
        return 0
    if not tr.get("enabled"):
        return skip("training is off (config/echo.yaml daydream.training.enabled)")
    if not a.now and not (NIGHT[0] <= datetime.now().hour < NIGHT[1]):
        return skip(f"outside the night window {NIGHT[0]:02d}:00–{NIGHT[1]:02d}:00")
    m = last_talk_minutes()
    if m is not None and m < QUIET_MIN:
        return skip(f"someone talked to Echo {m:.0f} min ago")
    ex = T.Examples()
    n = ex.counts()["included"]
    if n < int(tr.get("min_examples", 50)):
        return skip(f"{n} kept examples, need {tr.get('min_examples', 50)}")
    lock = T.DATA / "training.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < 8 * 3600:
        return skip("another training run is going")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))

    started = time.time()
    work = T.DATA / "training" / datetime.now().strftime("%Y%m%d-%H%M")
    work.mkdir(parents=True, exist_ok=True)
    log_to = work / "run.log"
    versions = T.Versions()
    current = versions.current()
    candidate = f"echo-trained-{work.name}"
    created = False
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from daydream_set import instructions                        # exactly what the live Echo uses
        from core.config import load_config
        # Only the identity lines of Echo's instructions: all 1,935 tokens didn't fit the training window (every
        # example was cut before the answer) and ran the GPU out of memory (11-04). The answers carry the style.
        short = "\n".join(instructions(load_config(ROOT / "config" / "echo.yaml")).splitlines()[:3])
        info = ex.export(work / "data", short)
        T.log({"event": "started", "examples": info, "current": current})
        if a.dry_run:
            return skip(f"dry run: exported {info}")
        iters = a.iters or max(50, min(600, 2 * info["train"]))
        (work / "lora.yaml").write_text(LORA_YAML)
        ollama("/api/generate", {"model": current, "keep_alive": 0})     # free memory for training
        run([TRAIN_PY, "-m", "mlx_lm", "lora", "-c", work / "lora.yaml", "--model", BASE_MLX, "--train",
             "--data", work / "data", "--iters", iters, "--batch-size", 1, "--num-layers", 8, "--max-seq-length", MAX_SEQ,
             "--mask-prompt", "--grad-checkpoint", "--adapter-path", work / "adapters"], log_to)
        quiet_or_stop(started)
        merged = work / "merged.gguf"
        run([TRAIN_PY, ROOT / "core" / "lora_gguf.py", "merge", base_gguf(current), work / "adapters", merged], log_to)
        (work / "Modelfile").write_text(modelfile_for(current, merged))
        run(["ollama", "create", candidate, "-f", work / "Modelfile"], log_to)
        created = True
        merged.unlink(missing_ok=True)
        quiet_or_stop(started)
        cur = measure(current, work, "current", log_to)
        quiet_or_stop(started)
        cand = measure(candidate, work, "candidate", log_to)
        quiet_or_stop(started)
        ok, why = T.gate(cur, cand)
        result = {"event": "gate", "ok": ok, "why": why, "current": cur, "candidate": cand, "model": candidate,
                  "iters": iters, "minutes": round((time.time() - started) / 60, 1)}
        T.log(result)
        (work / "result.json").write_text(json.dumps(result, indent=1))
        if ok:
            versions.switch(candidate, why)
            T.log({"event": "pruned", "models": prune(int(tr.get("keep_versions", 2)), versions)})
            T.restart_core(delay_s=1)
        else:
            subprocess.run(["ollama", "rm", candidate], capture_output=True)
        print(json.dumps(result, indent=1))
        return 0
    except Interrupted as e:
        T.log({"event": "stopped", "why": str(e)})
        if created:
            subprocess.run(["ollama", "rm", candidate], capture_output=True)
        return 0
    except Exception as e:  # noqa: BLE001  (logged, never leaves a half-made model behind)
        T.log({"event": "failed", "why": f"{type(e).__name__}: {e}"})
        if created:
            subprocess.run(["ollama", "rm", candidate], capture_output=True)
        raise
    finally:
        (work / "merged.gguf").unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
        try:
            ollama("/api/generate", {"model": versions.current(), "keep_alive": "24h"})   # Echo warm again
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
