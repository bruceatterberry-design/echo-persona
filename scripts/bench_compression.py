"""Vector compression check on Echo's real data: how much can meaning vectors shrink before search changes?
    PYTHONPATH=. uv run python scripts/bench_compression.py   (read-only; needs Ollama + nomic-embed-text)"""
import json, sqlite3, sys, time
import numpy as np, httpx
H = "http://127.0.0.1:11434"
db = sqlite3.connect("file:data/memory.sqlite?mode=ro", uri=True)
docs = [r[0] for r in db.execute("SELECT content FROM memories WHERE hidden IS NULL")]
docs += [r[0] for r in db.execute("SELECT content FROM turns WHERE length(content) > 20")]
mem_ids = [r[0] for r in db.execute("SELECT id FROM memories WHERE hidden IS NULL")]
qs = [json.loads(x) for x in open("data/eval/memory_questions.jsonl")]
def emb(texts, pre):
    out = []
    for i in range(0, len(texts), 64):
        r = httpx.post(f"{H}/api/embed", json={"model": "nomic-embed-text", "input": [pre + t[:2000] for t in texts[i:i+64]]}, timeout=300)
        out += r.json()["embeddings"]
    return np.asarray(out, dtype=np.float32)
t = time.time(); D = emb(docs, "search_document: "); Q = emb([q["q"] for q in qs], "search_query: ")
print(f"embedded {len(docs)} docs + {len(qs)} questions in {time.time()-t:.1f} s")
def prep(X, dims):
    X = X - 0  # Matryoshka (nomic v1.5): layer norm, cut, renormalise
    X = (X - X.mean(1, keepdims=True)) / (X.std(1, keepdims=True) + 1e-6)
    X = X[:, :dims]
    return X / np.linalg.norm(X, axis=1, keepdims=True)
def quant(X, kind):
    if kind == "float32": return X, 4
    if kind == "float16": return X.astype(np.float16).astype(np.float32), 2
    if kind == "int8":
        s = 127 / np.abs(X).max(); return np.round(X * s).astype(np.int8).astype(np.float32) / s, 1
    if kind == "binary": return np.where(X > 0, 1.0, -1.0).astype(np.float32), 1 / 8
full = prep(D, 768); fullq = prep(Q, 768)
truth = np.argsort(-(fullq @ full.T), axis=1)[:, :10]
nmem = len(mem_ids)
def hit(Dv, Qv):   # the 98-question set, memories only, hit@5 by memory id
    s = Qv @ Dv[:nmem].T; got = np.argsort(-s, axis=1)[:, :5]; n = h = 0
    for i, q in enumerate(qs):
        if q["want"]:
            n += 1; h += bool(set(mem_ids[j] for j in got[i]) & set(q["want"]))
    return h / n
print(f"{'dims':>5} {'type':>8} {'bytes/vector':>12} {'same top-10 as full':>20} {'hit@5 (98 questions)':>21}")
for dims in (768, 512, 256, 128, 64):
    for kind in ("float32", "float16", "int8", "binary"):
        Dv, b = quant(prep(D, dims), kind); Qv = prep(Q, dims)
        top = np.argsort(-(Qv @ Dv.T), axis=1)[:, :10]
        rec = np.mean([len(set(a) & set(t)) / 10 for a, t in zip(top, truth)])
        print(f"{dims:>5} {kind:>8} {dims * b:>12.0f} {rec:>20.0%} {hit(Dv, Qv):>21.0%}")
