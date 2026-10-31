"""Phase 6: vector search at 1M / 2M items on this Mac (synthetic memories shaped like Echo's real ones).

Each synthetic memory = one of Echo's real meaning vectors + noise (cosine to it ~0.75), so the data clusters like
real memories instead of being uniform noise. Data is regenerated chunk by chunk from fixed seeds, so every process
sees identical vectors without keeping float32 copies in RAM.

    python ann.py truth N            exact top-10 for 200 queries (saved)
    python ann.py <method> N         build + measure: brute_i8 | bin_rerank | usearch_i8 | faiss_ivf_sq8
"""
import json, os, resource, sys, time
import numpy as np

D, CHUNK, NQ, K = 768, 50_000, 200, 10
HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "bench")   # private, gitignored
os.makedirs(HERE, exist_ok=True)
# Needs: numpy, faiss-cpu, usearch, httpx in a separate env (not Echo's), e.g.
#   uv venv /tmp/annenv && uv pip install --python /tmp/annenv/bin/python numpy faiss-cpu usearch httpx
#   /tmp/annenv/bin/python scripts/bench_vectors.py truth 1000000 && ... faiss_ivf_pq 1000000


def seeds():
    p = os.path.join(HERE, "seeds.npy")
    if not os.path.exists(p):
        import httpx, sqlite3
        db = sqlite3.connect(f"file:{os.path.join(HERE, '..', 'memory.sqlite')}?mode=ro", uri=True)
        docs = [r[0] for r in db.execute("SELECT content FROM memories WHERE hidden IS NULL")]
        docs += [r[0] for r in db.execute("SELECT content FROM turns WHERE length(content) > 20")]
        out = []
        for i in range(0, len(docs), 64):
            out += httpx.post("http://127.0.0.1:11434/api/embed", timeout=300, json={"model": "nomic-embed-text",
                              "input": ["search_document: " + t[:2000] for t in docs[i:i + 64]]}).json()["embeddings"]
        S = np.asarray(out, dtype=np.float32)
        np.save(p, S / np.linalg.norm(S, axis=1, keepdims=True))
    return np.load(p)


SEEDS = seeds()


def chunk(i):
    """Synthetic vectors for chunk i (float32, unit length), always the same."""
    rng = np.random.default_rng(1000 + i)
    X = rng.standard_normal((CHUNK, D), dtype=np.float32)
    X *= 0.032
    X += SEEDS[rng.integers(0, len(SEEDS), CHUNK)]
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    return X


def queries(N):
    rng = np.random.default_rng(7)
    idx = rng.integers(0, N, NQ)
    Q = np.empty((NQ, D), dtype=np.float32)
    for c in sorted({int(j) // CHUNK for j in idx}):   # each chunk made once
        X = chunk(c)
        for n, j in enumerate(idx):
            if int(j) // CHUNK == c:
                Q[n] = X[int(j) % CHUNK]
    Q = Q + rng.standard_normal(Q.shape, dtype=np.float32) * 0.02
    return Q / np.linalg.norm(Q, axis=1, keepdims=True)


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6


def truth(N):
    Q = queries(N)
    best_s = np.full((NQ, K), -2.0, dtype=np.float32); best_i = np.zeros((NQ, K), dtype=np.int64)
    for c in range(N // CHUNK):
        s = Q @ chunk(c).T
        top = np.argpartition(-s, K, axis=1)[:, :K]
        cs = np.concatenate([best_s, np.take_along_axis(s, top, 1)], 1)
        ci = np.concatenate([best_i, top + c * CHUNK], 1)
        o = np.argsort(-cs, axis=1)[:, :K]
        best_s, best_i = np.take_along_axis(cs, o, 1), np.take_along_axis(ci, o, 1)
    np.save(os.path.join(HERE, f"truth_{N}.npy"), best_i)
    np.save(os.path.join(HERE, f"queries_{N}.npy"), Q)


def recall(got, T):
    return float(np.mean([len(set(g[:K]) & set(t)) / K for g, t in zip(got, T)]))


def timed_queries(fn, Q):
    fn(Q[0])
    lat, got = [], []
    for q in Q:
        t = time.perf_counter(); got.append(fn(q)); lat.append((time.perf_counter() - t) * 1000)
    lat.sort()
    return got, lat[len(lat) // 2], lat[int(len(lat) * .95)]


def run(method, N):
    Q = np.load(os.path.join(HERE, f"queries_{N}.npy")); T = np.load(os.path.join(HERE, f"truth_{N}.npy"))
    t0 = time.perf_counter(); extra = {}
    if method in ("brute_i8", "bin_rerank"):
        X8 = np.empty((N, D), dtype=np.int8); scale = np.empty(N, dtype=np.float32)   # per-vector int8 scale
        for c in range(N // CHUNK):
            X = chunk(c); m = np.abs(X).max(1, keepdims=True)
            X8[c * CHUNK:(c + 1) * CHUNK] = np.round(X * 127 / m); scale[c * CHUNK:(c + 1) * CHUNK] = (m / 127).ravel()
        if method == "brute_i8":
            def fn(q, B=250_000):   # dequantise block by block (bounded extra memory)
                best = []
                for a in range(0, N, B):
                    s = (X8[a:a + B].astype(np.float32) @ q) * scale[a:a + B]
                    top = np.argpartition(-s, K)[:K]; best += [(s[t], a + t) for t in top]
                return [i for _, i in sorted(best, reverse=True)[:K]]
        else:
            bits = np.packbits(X8 > 0, axis=1)                      # 96 bytes per vector
            lut = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
            extra["binary_mb"] = bits.nbytes / 1e6
            def fn(q, cand=2000):
                qb = np.packbits(q > 0)
                h = lut[np.bitwise_xor(bits, qb)].sum(1, dtype=np.uint16)
                c = np.argpartition(h, cand)[:cand]
                s = (X8[c].astype(np.float32) @ q) * scale[c]
                return c[np.argsort(-s)[:K]]
        build = time.perf_counter() - t0
        got, p50, p95 = timed_queries(fn, Q)
        size_mb = X8.nbytes / 1e6
    elif method == "usearch_i8":
        from usearch.index import Index
        idx = Index(ndim=D, metric="cos", dtype="i8", connectivity=16, expansion_add=128, expansion_search=64)
        for c in range(N // CHUNK):
            idx.add(np.arange(c * CHUNK, (c + 1) * CHUNK), chunk(c), threads=10)
        build = time.perf_counter() - t0
        p = os.path.join(HERE, f"usearch_{N}.idx"); idx.save(p); size_mb = os.path.getsize(p) / 1e6
        out = {}
        for ef in (32, 64, 128):
            idx.expansion_search = ef
            got, p50, p95 = timed_queries(lambda q: idx.search(q, K, threads=1).keys, Q)
            out[ef] = (recall(got, T), p50, p95)
        os.remove(p)
        extra["by_expansion_search"] = {k: {"recall@10": round(v[0], 3), "p50_ms": round(v[1], 2), "p95_ms": round(v[2], 2)} for k, v in out.items()}
        got, p50, p95 = None, out[64][1], out[64][2]
        rec = out[64][0]
    elif method == "faiss_ivf_sq8":
        import faiss
        faiss.omp_set_num_threads(10)
        q8 = faiss.IndexFlatIP(D)
        idx = faiss.IndexIVFScalarQuantizer(q8, D, 4096, faiss.ScalarQuantizer.QT_8bit, faiss.METRIC_INNER_PRODUCT)
        train = np.concatenate([chunk(c)[:25_000] for c in range(min(8, N // CHUNK))])
        idx.train(train)
        for c in range(N // CHUNK):
            idx.add(chunk(c))
        build = time.perf_counter() - t0
        p = os.path.join(HERE, f"faiss_{N}.idx"); faiss.write_index(idx, p); size_mb = os.path.getsize(p) / 1e6; os.remove(p)
        faiss.omp_set_num_threads(1)
        out = {}
        for nprobe in (16, 32, 64):
            idx.nprobe = nprobe
            got, p50, p95 = timed_queries(lambda q: idx.search(q[None, :], K)[1][0], Q)
            out[nprobe] = (recall(got, T), p50, p95)
        extra["by_nprobe"] = {k: {"recall@10": round(v[0], 3), "p50_ms": round(v[1], 2), "p95_ms": round(v[2], 2)} for k, v in out.items()}
        got, p50, p95 = None, out[32][1], out[32][2]
        rec = out[32][0]
    elif method == "faiss_ivf_pq":   # small in RAM (96 B/vector); exact int8 vectors on disk, read only for re-checking
        import faiss
        faiss.omp_set_num_threads(10)
        idx = faiss.IndexIVFPQ(faiss.IndexFlatIP(D), D, 4096, 96, 8, faiss.METRIC_INNER_PRODUCT)
        idx.train(np.concatenate([chunk(c)[:25_000] for c in range(min(8, N // CHUNK))]))
        disk = os.path.join(HERE, f"x8_{N}.bin"); scale = np.empty(N, dtype=np.float32)
        with open(disk, "wb") as f:
            for c in range(N // CHUNK):
                X = chunk(c); idx.add(X); m = np.abs(X).max(1, keepdims=True)
                f.write(np.round(X * 127 / m).astype(np.int8).tobytes()); scale[c * CHUNK:(c + 1) * CHUNK] = (m / 127).ravel()
        build = time.perf_counter() - t0
        p = os.path.join(HERE, f"faisspq_{N}.idx"); faiss.write_index(idx, p); size_mb = os.path.getsize(p) / 1e6; os.remove(p)
        X8 = np.memmap(disk, dtype=np.int8, mode="r", shape=(N, D))
        faiss.omp_set_num_threads(1)
        idx.nprobe = 32
        def fn(q, cand=100):
            c = idx.search(q[None, :], cand)[1][0]
            c = np.sort(c[c >= 0])
            s = (np.asarray(X8[c], dtype=np.float32) @ q) * scale[c]
            return c[np.argsort(-s)[:K]]
        out = {}
        for nprobe, cand in ((32, 300), (64, 1000), (128, 3000)):
            idx.nprobe = nprobe
            g, a50, a95 = timed_queries(lambda q: fn(q, cand), Q)
            out[f"nprobe{nprobe}_cand{cand}"] = {"recall@10": round(recall(g, T), 3), "p50_ms": round(a50, 2), "p95_ms": round(a95, 2)}
        extra["by_setting"] = out
        idx.nprobe = 64
        got, p50, p95 = timed_queries(lambda q: fn(q, 1000), Q)
        extra["vectors_on_disk_mb"] = round(os.path.getsize(disk) / 1e6)
        extra["pq_only_recall"] = round(recall([idx.search(q[None, :], K)[1][0] for q in Q], T), 3)
        os.remove(disk)
    if got is not None:
        rec = recall(got, T)
    r = {"method": method, "N": N, "build_s": round(build, 1), "index_mb": round(size_mb), "peak_rss_mb": round(rss_mb()),
         "recall@10": round(rec, 3), "p50_ms": round(p50, 2), "p95_ms": round(p95, 2)} | extra
    print(json.dumps(r))
    with open(os.path.join(HERE, "ann_results.jsonl"), "a") as f:
        f.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    what, N = sys.argv[1], int(sys.argv[2])
    truth(N) if what == "truth" else run(what, N)
