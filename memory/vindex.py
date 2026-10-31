"""Search by meaning at any size (Phase 6 of docs/MEMORY_ARCHITECTURE.md; the owner approved FAISS 2026-10-31).

Up to EXACT_MAX memories: exact search over the vectors cached in RAM (the same results as reading them from SQLite,
without re-reading them for every message). Beyond that: a FAISS IVF-PQ index (96 bytes per memory in RAM) finds
1,000 candidates, which are re-checked exactly against int8 copies of the vectors on disk (measured at 2M: 97 % of
the true top 10, 1.7 ms, 221 MB RAM; docs/BENCHMARKS.md). The index is built in quiet time (memory/consolidate.py);
memories added since the last build are searched exactly and merged in, so nothing new is ever missed.

Everything here is a cache: it can be deleted and rebuilt from the `vectors` table at any time.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import numpy as np

from memory.store import MemoryStore

EXACT_MAX = 50_000        # up to this many memories: exact search (no approximation at all)
CANDIDATES, NPROBE = 1000, 64
SAMPLE = 2048             # vectors kept to estimate how unusual a match is (the z-score in memory/retrieve.py)


class VectorIndex:
    def __init__(self, store: MemoryStore, model: str, folder: Path | None = None):
        self.store, self.model = store, model
        self.folder = folder or store.path.parent / "vindex"
        self.ver = None
        self.ids = np.zeros(0, dtype=np.int64)
        self.mat = np.zeros((0, 0), dtype=np.float32)
        self.ann = None   # loaded FAISS parts, when the memory is big

    # ---- what's in the database ----
    def _version(self) -> str:
        return self.store.get_meta("changes", "0") + "/" + self.store.get_meta("vec_changes", "0")

    def count(self) -> int:
        return self.store.db.execute("SELECT COUNT(*) FROM vectors WHERE model=?", (self.model,)).fetchone()[0]

    def _rows(self, where: str = "", params: tuple = (), batch: int = 20_000):
        cur = self.store.db.execute(
            "SELECT v.memory_id, v.vec FROM vectors v JOIN memories m ON m.id=v.memory_id "
            f"WHERE v.model=? AND m.hidden IS NULL {where} ORDER BY v.memory_id", (self.model, *params))
        while (part := cur.fetchmany(batch)):
            ids = np.array([r[0] for r in part], dtype=np.int64)
            yield ids, np.frombuffer(b"".join(r[1] for r in part), dtype=np.float32).reshape(len(part), -1)

    def _refresh(self) -> None:
        v = self._version()
        if v == self.ver:
            return
        self.ver = v
        if self.count() <= EXACT_MAX or not self._load_ann():
            self.ann = None
            parts = list(self._rows())
            self.ids = np.concatenate([p[0] for p in parts]) if parts else np.zeros(0, dtype=np.int64)
            self.mat = np.concatenate([p[1] for p in parts]) if parts else np.zeros((0, 0), dtype=np.float32)
        else:   # big: only the memories added since the index was built are kept here, searched exactly
            built = self.ann["built_upto"]
            parts = list(self._rows("AND v.memory_id > ?", (built,)))
            self.ids = np.concatenate([p[0] for p in parts]) if parts else np.zeros(0, dtype=np.int64)
            self.mat = np.concatenate([p[1] for p in parts]) if parts else np.zeros((0, 0), dtype=np.float32)

    # ---- search ----
    def search(self, qvec, k: int = 30) -> tuple[np.ndarray, np.ndarray, float, float]:
        """(memory ids, cosine similarities) of the k closest memories in use, best first, plus the mean and spread of
        similarities over all memories (exact when small; from a fixed sample when big)."""
        self._refresh()
        q = np.asarray(qvec, dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-9)
        if self.ann is None:
            if not len(self.ids):
                return self.ids, np.zeros(0, dtype=np.float32), 0.0, 1.0
            sims = self.mat @ q
            top = np.argsort(-sims)[:k]
            return self.ids[top], sims[top], float(sims.mean()), float(sims.std()) or 1.0
        a = self.ann
        a["index"].nprobe = NPROBE
        cand = a["index"].search(q[None, :], CANDIDATES)[1][0]
        cand = np.sort(cand[cand >= 0])
        sims = (np.asarray(a["x8"][cand], dtype=np.float32) @ q) * a["scale"][cand]
        ids = a["ids"][cand]
        if len(self.ids):   # added since the build: exact
            ids, sims = np.concatenate([ids, self.ids]), np.concatenate([sims, self.mat @ q])
        live = {r[0] for r in self.store.db.execute(   # forgotten since the build: left out
            f"SELECT id FROM memories WHERE hidden IS NULL AND id IN ({','.join('?' * len(ids))})", [int(i) for i in ids])}
        keep = np.array([int(i) in live for i in ids], dtype=bool)
        ids, sims = ids[keep], sims[keep]
        top = np.argsort(-sims)[:k]
        ref = a["sample"] @ q
        return ids[top], sims[top], float(ref.mean()), float(ref.std()) or 1.0

    # ---- the big-memory index (built in quiet time) ----
    def needs_build(self) -> bool:
        n = self.count()
        if n <= EXACT_MAX:
            return False
        info = self._info()
        return not info or info.get("model") != self.model or n - info.get("n", 0) > 0.05 * info.get("n", 1)

    def _info(self) -> dict | None:
        p = self.folder / "info.json"
        return json.loads(p.read_text()) if p.exists() else None

    def _load_ann(self) -> bool:
        info = self._info()
        if not info or info.get("model") != self.model:
            return False
        if self.ann and self.ann.get("built_at") == info["built_at"]:
            return True
        import faiss
        f = self.folder
        self.ann = {"index": faiss.read_index(str(f / "ivfpq.faiss")), "ids": np.load(f / "ids.npy"),
                    "scale": np.load(f / "scale.npy"), "sample": np.load(f / "sample.npy"),
                    "x8": np.memmap(f / "x8.bin", dtype=np.int8, mode="r", shape=(info["n"], info["dim"])),
                    "built_upto": info["built_upto"], "built_at": info["built_at"]}
        return True

    def build(self) -> dict:
        """Build the FAISS index from all vectors in use (minutes at millions; run in quiet time). Written to a new
        folder and swapped in at the end, so a half-built index is never used."""
        import faiss
        t0 = time.time()
        n = self.count()
        tmp = self.folder.with_name(self.folder.name + ".new")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        dim = None
        rng = np.random.default_rng(0)
        train, sample, ids, scales = [], [], [], []
        with open(tmp / "x8.bin", "wb") as f:
            for part_ids, X in self._rows():
                dim = X.shape[1]
                m = np.abs(X).max(1, keepdims=True) + 1e-9
                f.write(np.round(X * 127 / m).astype(np.int8).tobytes())
                ids.append(part_ids)
                scales.append((m / 127).ravel().astype(np.float32))
                take = rng.random(len(X)) < min(1.0, 200_000 / max(n, 1))
                train.append(X[take])
                sample.append(X[rng.random(len(X)) < SAMPLE / max(n, 1)])
        ids, scales = np.concatenate(ids), np.concatenate(scales)
        nlist = int(min(4096, max(64, 4 * np.sqrt(len(ids)))))
        index = faiss.IndexIVFPQ(faiss.IndexFlatIP(dim), dim, nlist, 96 if dim % 96 == 0 else 64, 8, faiss.METRIC_INNER_PRODUCT)
        index.train(np.concatenate(train))
        x8 = np.memmap(tmp / "x8.bin", dtype=np.int8, mode="r", shape=(len(ids), dim))
        for a in range(0, len(ids), 50_000):
            index.add((np.asarray(x8[a:a + 50_000], dtype=np.float32) * scales[a:a + 50_000, None]))
        faiss.write_index(index, str(tmp / "ivfpq.faiss"))
        np.save(tmp / "ids.npy", ids)
        np.save(tmp / "scale.npy", scales)
        np.save(tmp / "sample.npy", np.concatenate(sample)[:SAMPLE * 2])
        info = {"model": self.model, "n": int(len(ids)), "dim": int(dim), "built_upto": int(ids.max()),
                "built_at": time.time(), "seconds": round(time.time() - t0, 1), "nlist": nlist}
        (tmp / "info.json").write_text(json.dumps(info))
        old = self.folder.with_name(self.folder.name + ".old")
        shutil.rmtree(old, ignore_errors=True)
        if self.folder.exists():
            os.replace(self.folder, old)
        os.replace(tmp, self.folder)
        shutil.rmtree(old, ignore_errors=True)
        self.ver = None   # reload on next search
        return info
