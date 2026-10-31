"""Memory retrieval benchmark (Phase 3, docs/MEMORY_ARCHITECTURE.md): today's way vs the v2 engine on a question set.

    PYTHONPATH=. uv run python scripts/bench_memory.py [questions.jsonl] [--db data/memory.sqlite] [--now 2026-10-31T01:00]

Questions (data/eval/memory_questions.jsonl, private): {"q", "speaker"?, "want": [memory ids], "type"}.
"want" = memories that answer it (any one is a hit); [] = nothing should be found ("I don't know" questions).
Read-only: the database is opened on a copy in memory.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import load_config                    # noqa: E402
from memory.embed import Embedder                       # noqa: E402
from memory.entities import Entities                    # noqa: E402
from memory.retrieve import MEANING_MIN, Retriever      # noqa: E402
from memory.store import MemoryStore                    # noqa: E402

STOP_OLD = None


def old_way(store: MemoryStore, text: str, qvec, model: str, k: int) -> list[int]:
    """Today's build_messages extras (core/server.py), without the 'already in the prompt' exclusion, up to k."""
    from core.server import content_words
    said = content_words(text)
    extra = [m for m in store.search(text, limit=8) if len(said & content_words(m.content)) >= 2]
    if qvec is not None:
        near = [m for m, _ in store.by_meaning(qvec, model, limit=6, min_sim=MEANING_MIN)]
        extra = list({m.id: m for m in near[:k] + extra}.values())
    return [m.id for m in extra][:k]


def score(rows: list[tuple[list[int], list[int]]], k: int) -> dict:
    ans = [(got, want) for got, want in rows if want]
    none = [(got, want) for got, want in rows if not want]
    hit = sum(bool(set(got[:k]) & set(want)) for got, want in ans)
    rr = [next((1 / (i + 1) for i, g in enumerate(got[:k]) if g in want), 0.0) for got, want in ans]
    prec = [len(set(got[:k]) & set(want)) / len(got[:k]) for got, want in ans if got[:k]]
    return {"answerable": len(ans), f"hit@{k}": f"{hit}/{len(ans)} ({hit / len(ans):.0%})", "MRR": round(statistics.mean(rr), 3),
            "precision": f"{statistics.mean(prec):.0%}" if prec else "-",
            "none_quiet": f"{sum(not got[:k] for got, _ in none)}/{len(none)}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("questions", nargs="?", default="data/eval/memory_questions.jsonl")
    ap.add_argument("--db", default="data/memory.sqlite")
    ap.add_argument("--now", default="2026-10-31T01:00")
    ap.add_argument("--show", action="store_true", help="print each question's results")
    a = ap.parse_args()
    cfg = load_config()
    src = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    mem = sqlite3.connect(":memory:", check_same_thread=False)
    src.backup(mem)
    store = MemoryStore.__new__(MemoryStore)
    store.path, store.db = Path(":memory:"), mem
    mem.row_factory = sqlite3.Row
    ents = Entities(store)
    emb = Embedder(cfg.llm.host)
    r = Retriever(store, ents, emb.model)
    now = datetime.fromisoformat(a.now).timestamp()
    qs = [json.loads(x) for x in Path(a.questions).read_text().splitlines() if x.strip()]
    old2, old5, new2, new5, lat, emb_ms = [], [], [], [], [], []
    by_type: dict[str, list] = {}
    for q in qs:
        t = time.perf_counter()
        v = emb.query(q["q"])
        emb_ms.append((time.perf_counter() - t) * 1000)
        o = old_way(store, q["q"], v, emb.model, 5)
        res = r.retrieve(q["q"], v, q.get("speaker"), now, k=5)
        n = [h.memory.id for h in res.hits]
        lat.append(res.ms)
        old2.append((o[:2], q["want"])); old5.append((o, q["want"])); new2.append((n[:2], q["want"])); new5.append((n, q["want"]))
        by_type.setdefault(q["type"], []).append((o, n, q["want"]))
        if a.show:
            mark = lambda got: "✓" if set(got) & set(q["want"]) or (not q["want"] and not got) else "✗"
            print(f"{mark(o)}{mark(n)} {q['q']}\n    old {o}\n    new {[(h.memory.id, ' '.join(h.why)) for h in res.hits]}  "
                  f"want {q['want']}" + (f"  facts {res.facts}" if res.facts else ""))
    print(f"\n{len(qs)} questions, now = {a.now}")
    for half, sel in (("odd-numbered", slice(0, None, 2)), ("even-numbered", slice(1, None, 2))):
        print(f"  {half:14} old 2 {score(old2[sel], 2)['hit@2']}, new 2 {score(new2[sel], 2)['hit@2']}, "
              f"new quiet on none {score(new5[sel], 5)['none_quiet']}")
    print("old, 2 extras (as used today):", score(old2, 2))
    print("old, up to 5:                 ", score(old5, 5))
    print("new, top 2:                   ", score(new2, 2))
    print("new, up to 5:                 ", score(new5, 5))
    print("by type (hit@5 old → new):")
    for t, rows in sorted(by_type.items()):
        if t == "none":
            print(f"  {t:10} quiet {sum(not o for o, _, _ in rows)}/{len(rows)} → {sum(not n for _, n, _ in rows)}/{len(rows)}")
        else:
            print(f"  {t:10} {sum(bool(set(o) & set(w)) for o, _, w in rows)}/{len(rows)} → {sum(bool(set(n) & set(w)) for _, n, w in rows)}/{len(rows)}")
    q = sorted(lat)
    print(f"engine latency: p50 {statistics.median(q):.1f} ms, p95 {q[int(len(q) * .95) - 1]:.1f} ms, max {q[-1]:.1f} ms "
          f"(+ query vector p50 {statistics.median(emb_ms):.0f} ms)")


if __name__ == "__main__":
    main()
