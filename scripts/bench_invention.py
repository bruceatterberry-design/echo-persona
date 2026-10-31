"""Does Echo invent details? (Phase 5, docs/MEMORY_ARCHITECTURE.md)

Sends questions whose answers are thin or missing in Echo's memory through the real reply pipeline (create_app +
the configured LLM), on a *copy* of the memory database, with a fresh conversation for each question, and writes the
answers to a JSON file for checking by hand ("invented" = a claim about a person, event or thing that isn't in his
memories or the question).

    PYTHONPATH=. uv run python scripts/bench_invention.py out.json [--model llama3.1:8b] [--temperature 0.7]
        [--think on|off] [--options '{"top_p": 0.95, "top_k": 20}']
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient     # noqa: E402

from core.config import load_config            # noqa: E402
from core.llm import OllamaBackend             # noqa: E402
from core.server import create_app             # noqa: E402
from memory.store import MemoryStore           # noqa: E402

QUESTIONS = [
    "Who is Jordan Price?", "Tell me about Biscuit.", "Tell me about Pepper.", "Who is Casey Ward?",
    "What happened with the acetone and the carpet?", "Tell me about the owner's first job.",
    "What do you know about the loved one's mom?", "What's the owner's rotary engine project?", "Tell me about our trip to the lake.",
    "Who are the owner's parents?", "What did the loved one and the owner do last weekend?", "What's the loved one's middle name?",
    "Tell me about the owner's brother.", "What does the owner do for work?", "Where did the owner grow up?",
    "What does the loved one teach?",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--model")
    ap.add_argument("--temperature", type=float)
    ap.add_argument("--think", choices=["on", "off"])
    ap.add_argument("--options", default="{}")
    ap.add_argument("--db", default="data/memory.sqlite")
    ap.add_argument("--conversation", action="store_true", help="one ongoing conversation (speed as in real use)")
    ap.add_argument("--questions", help="file with one question per line instead of the built-in ones")
    a = ap.parse_args()
    cfg = load_config()
    if a.model:
        cfg.llm.model = a.model
    if a.temperature is not None:
        cfg.llm.temperature = a.temperature
    if a.think:
        cfg.llm.think = a.think == "on"
    cfg.llm.options = json.loads(a.options)
    work = Path(a.out).with_suffix(".sqlite")
    work.unlink(missing_ok=True)
    src, dst = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True), sqlite3.connect(work)
    src.backup(dst)
    dst.close()
    store = MemoryStore(work)
    backend = OllamaBackend(cfg.llm)
    app = create_app(cfg, backend, store)
    c = TestClient(app)   # not as a context manager: no background jobs, no speech models loaded
    results = []
    qs = Path(a.questions).read_text().splitlines() if a.questions else QUESTIONS
    store.clear_turns()
    for q in qs:
        if not a.conversation:
            store.clear_turns()   # a fresh conversation each time (an earlier invented answer would be repeated)
        t = time.perf_counter()
        with c.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"text": q})
            reply, first, expr = "", None, None
            while True:
                m = ws.receive_json()
                if m["type"] == "delta" and first is None:
                    first = time.perf_counter() - t
                if m["type"] == "reply":
                    reply, expr = m["text"], m.get("expression")
                if m["type"] == "state" and m["state"] == "idle":
                    break
        st = backend.last_stats
        results.append({"q": q, "a": reply, "s": round(time.perf_counter() - t, 1), "first_s": round(first or 0, 1),
                        "thinking_chars": st.get("thinking_chars", 0), "reply_tokens": st.get("reply_tokens"),
                        "expression": expr})
        print(f"Q: {q}\nA: {reply}\n   [first word {first or 0:.1f} s, total {time.perf_counter() - t:.1f} s, "
              f"thinking {st.get('thinking_chars', 0)} chars, face {expr}]\n", flush=True)
    Path(a.out).write_text(json.dumps({"model": cfg.llm.model, "temperature": cfg.llm.temperature, "think": cfg.llm.think,
                                       "options": cfg.llm.options, "results": results},
                                      indent=1))
    work.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
