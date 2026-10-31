"""Search by meaning at any size (memory/vindex.py): exact up to EXACT_MAX, FAISS beyond (the owner approved 2026-10-31)."""
import numpy as np
import pytest

import memory.vindex as vx
from memory.store import Memory, MemoryStore


def fill(store, n, dim=96, seed=0):
    rng = np.random.default_rng(seed)
    centers = rng.standard_normal((40, dim)).astype(np.float32)
    ids = []
    for i in range(n):
        m = store.save(Memory(content=f"synthetic memory number {i} " + "x" * (i % 13), status="inferred", source="auto:t"))
        v = centers[i % 40] + rng.standard_normal(dim).astype(np.float32) * 0.5
        store.set_vector(m, "t", v)
        ids.append(m.id)
    return ids, rng


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "m.sqlite")


def test_exact_mode_matches_brute_force_and_follows_changes(store):
    ids, rng = fill(store, 300)
    vi = vx.VectorIndex(store, "t")
    q = rng.standard_normal(96).astype(np.float32)
    got, sims, mu, sd = vi.search(q, 10)
    all_ids, all_sims = store.similarities(q, "t")
    order = np.argsort(-all_sims)[:10]
    assert list(got) == list(all_ids[order]) and np.allclose(sims, all_sims[order])
    assert abs(mu - all_sims.mean()) < 1e-5 and abs(sd - all_sims.std()) < 1e-5
    store.forget(int(got[0]))                                          # forgotten: gone at once
    assert int(got[0]) not in vi.search(q, 10)[0]


def test_faiss_mode_finds_nearly_all_true_neighbours_and_new_memories(store, monkeypatch, tmp_path):
    monkeypatch.setattr(vx, "EXACT_MAX", 1000)
    monkeypatch.setattr(vx, "CANDIDATES", 300)
    ids, rng = fill(store, 3000)
    vi = vx.VectorIndex(store, "t", tmp_path / "vindex")
    assert vi.needs_build()
    info = vi.build()
    assert info["n"] == 3000 and not vi.needs_build()
    hit = 0
    for _ in range(30):
        q = rng.standard_normal(96).astype(np.float32)
        all_ids, all_sims = store.similarities(q, "t")
        truth = set(all_ids[np.argsort(-all_sims)[:10]])
        hit += len(truth & set(vi.search(q, 10)[0])) / 10
    assert hit / 30 >= 0.9
    # added after the build: found (searched exactly until the next build)
    m = store.save(Memory(content="a brand new memory about kites", status="inferred", source="auto:t"))
    v = rng.standard_normal(96).astype(np.float32)
    store.set_vector(m, "t", v)
    assert vi.search(v, 1)[0][0] == m.id
    store.forget(m.id)                                                  # and forgotten again: left out
    assert m.id not in vi.search(v, 5)[0]


def test_links_are_the_same_in_blocks(store):
    from memory import consolidate
    ids, rng = fill(store, 1500)
    links, reps = consolidate.link(store, "t")
    assert links > 0 and consolidate.link(store, "t") == (links, reps)
