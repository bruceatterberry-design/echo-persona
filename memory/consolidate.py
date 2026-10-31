"""Consolidation (Phase 4 of docs/MEMORY_ARCHITECTURE.md): Echo's memory upkeep while nobody is talking.

One run, in order (each step is safe to repeat; nothing is deleted, nothing is confirmed without a person):
  0. guard:      SQLite quick_check, and no count may have dropped since the last run (memories incl. forgotten ones,
                 conversation turns, facts, originals). If anything is wrong, stop and report it.
  1. episodes:   group new conversation (memory/archive.py).
  2. links:      memories close in meaning (cosine ≥ LINK_MIN) are linked, up to 3 each ("related").
  3. repeats:    memories that say nearly the same thing (cosine ≥ REPEAT_MIN, same kind, not conversation notes:
                 notes from different conversations are often worded alike) are marked "repeats" for review.
                 They are *not* hidden: the owner decides.
  4. support:    the same fact found in several memories (Sam is_a nurse ×3) is counted, not stored again.
  5. conflicts:  facts that can only have one answer (married_to, lives_in, from, first_job, created_by) with
                 different answers for the same person → the local model judges the two memories → possible
                 contradictions go to the existing review list ("Keep 1 / Keep 2 / Both are true").
  6. patterns:   a topic that comes up in ≥ PATTERN_EPISODES separate conversations becomes one inferred memory
                 ("We often talk about the weather."), shown in "What I remember" to confirm (edit) or forget.
  7. priority:   0.35 significance + 0.25 identity + 0.15 use + 0.15 relationships + 0.10 recency (section 7);
                 only ranks (Phase 5 builds the context pack from it), never decides deletion.
  8. report:     counts and timings in meta "consolidation_last", shown on the Status page.
"""
from __future__ import annotations

import json
import math
import time

from memory import archive
from memory.entities import Entities, norm
from memory.store import Memory, MemoryStore

LINK_MIN = 0.75
REPEAT_MIN = 0.92
PATTERN_EPISODES = 3
ONE_ANSWER = ("married_to", "lives_in", "from", "first_job", "created_by")
WEIGHTS = {"significance": 0.35, "identity": 0.25, "use": 0.15, "relationships": 0.15, "recency": 0.10}


def counts(store: MemoryStore) -> dict:
    q = lambda sql: store.db.execute(sql).fetchone()[0]
    return {"memories": q("SELECT COUNT(*) FROM memories"), "turns": q("SELECT COUNT(*) FROM turns"),
            "facts": q("SELECT COUNT(*) FROM facts"), "originals": q("SELECT COUNT(*) FROM sources")}


def guard(store: MemoryStore) -> list[str]:
    """Problems that must stop consolidation (and be shown to the owner)."""
    problems = []
    if (ok := store.db.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
        problems.append(f"database check failed: {ok}")
    last = json.loads(store.get_meta("consolidation_last", "{}")).get("counts", {})
    now = counts(store)
    for k, v in last.items():
        if now.get(k, 0) < v:
            problems.append(f"{k} dropped from {v} to {now.get(k, 0)} since the last run")
    return problems


def link(store: MemoryStore, model: str | None, vindex=None) -> tuple[int, int]:
    """Steps 2 + 3. Returns (links, repeats). Up to vindex.EXACT_MAX memories: every memory against every other, in
    blocks (exact, recomputed each run). Beyond: only memories added since the last run, through the FAISS index."""
    import numpy as np
    from memory.vindex import EXACT_MAX, VectorIndex
    if not model:
        return 0, 0
    vi = vindex or VectorIndex(store, model)
    now, links, repeats = time.time(), 0, 0
    big = vi.count() > EXACT_MAX
    if big:   # incremental: new memories only (old links stay)
        upto = int(store.get_meta("linked_upto", "0"))
        new = [(int(r[0]), np.frombuffer(r[1], dtype=np.float32)) for r in store.db.execute(
            "SELECT v.memory_id, v.vec FROM vectors v JOIN memories m ON m.id=v.memory_id WHERE v.model=? "
            "AND m.hidden IS NULL AND v.memory_id > ? ORDER BY v.memory_id", (model, upto))]
        pairs = []
        for mid, vec in new:
            ids, sims, _, _ = vi.search(vec, 4)
            pairs += [(mid, int(j), float(s)) for j, s in zip(ids, sims) if int(j) != mid][:3]
        if new:
            store.set_meta("linked_upto", str(new[-1][0]))
    else:
        vi._refresh()
        ids, mat = vi.ids, vi.mat
        if len(ids) < 2:
            return 0, 0
        pairs = []
        for a0 in range(0, len(ids), 1024):   # blocks: never an n × n matrix in memory
            sim = mat[a0:a0 + 1024] @ mat.T
            for r in range(len(sim)):
                sim[r, a0 + r] = -1
                pairs += [(int(ids[a0 + r]), int(ids[j]), float(sim[r, j])) for j in np.argsort(-sim[r])[:3]]
        with store.db:
            store.db.execute("DELETE FROM relations WHERE a_type='memory' AND b_type='memory' AND rel IN ('related','repeats') "
                             "AND source='auto:consolidate'")   # recomputed each run (derived, not originals)
    linked = sorted({x for p in pairs for x in p[:2]})
    kinds = {r[0]: r[1] for a0 in range(0, len(linked), 900) for r in store.db.execute(
        f"SELECT id, kind FROM memories WHERE id IN ({','.join('?' * len(linked[a0:a0 + 900]))})", linked[a0:a0 + 900])}
    with store.db:
        for i_, j_, s in pairs:
            if s < LINK_MIN:
                continue
            a, b = sorted((i_, j_))
            rel = "repeats" if s >= REPEAT_MIN and kinds.get(i_) == kinds.get(j_) != "summary" else "related"
            cur = store.db.execute("INSERT OR IGNORE INTO relations(a_type,a_id,rel,b_type,b_id,weight,status,source,created) "
                                   "VALUES ('memory',?,?,'memory',?,?,'inferred','auto:consolidate',?)",
                                   (a, rel, b, round(s, 3), now))
            if cur.rowcount:
                links += rel == "related"
                repeats += rel == "repeats"
    return links, repeats


def live_facts(store: MemoryStore) -> list:
    return store.db.execute(
        "SELECT f.id, f.subject_id, f.predicate, f.object_id, COALESCE(o.name, f.value) AS obj, f.memory_id FROM facts f "
        "LEFT JOIN entities o ON o.id=f.object_id JOIN memories m ON m.id=f.memory_id "
        "WHERE f.retired IS NULL AND m.hidden IS NULL").fetchall()


def support(store: MemoryStore) -> int:
    """Step 4: how many facts are said by more than one memory."""
    groups: dict[tuple, set] = {}
    for f in live_facts(store):
        groups.setdefault((f["subject_id"], f["predicate"], norm(f["obj"] or "")), set()).add(f["memory_id"])
    return sum(len(v) > 1 for v in groups.values())


def fact_conflicts(store: MemoryStore, judge, max_judge: int = 10) -> tuple[int, int]:
    """Step 5. Returns (pairs judged, flagged)."""
    by: dict[tuple, list] = {}
    for f in live_facts(store):
        if f["predicate"] in ONE_ANSWER:
            by.setdefault((f["subject_id"], f["predicate"]), []).append(f)
    checked = set(json.loads(store.get_meta("consolidate_checked", "[]")))
    judged = flagged = 0
    for facts in by.values():
        for i, f in enumerate(facts):
            for g in facts[i + 1:]:
                x, y = norm(f["obj"] or ""), norm(g["obj"] or "")
                if f["memory_id"] == g["memory_id"] or not x or not y or x in y or y in x:
                    continue   # same answer, or one is just more detailed ("Springfield" / "12 Oak Street in Springfield")
                a, b = sorted((f["memory_id"], g["memory_id"]))
                key = f"{a}-{b}"
                if key in checked or judged >= max_judge:
                    continue
                ma, mb = store.get(a), store.get(b)
                verdict = judge(ma.content, mb.content) if judge else None
                if verdict is None:
                    continue   # couldn't judge now: try again next run
                judged += 1
                checked.add(key)
                if verdict[0]:
                    store.flag_conflict(a, b, 1.0, verdict[1] or "different answers for the same thing")
                    flagged += 1
    store.set_meta("consolidate_checked", json.dumps(sorted(checked)))
    return judged, flagged


def patterns(store: MemoryStore) -> list[str]:
    """Step 6. Returns the new pattern memories' texts."""
    seen = set(json.loads(store.get_meta("patterns_saved", "[]")))
    topics: dict[str, set[int]] = {}
    said: dict[str, str] = {}   # the topic as it was worded ("the weather"), for the note
    for f in live_facts(store):
        if f["predicate"] != "talked_about" or not (m := store.get(f["memory_id"])) or not (sp := archive.span_of(m)):
            continue
        topic = norm(f["obj"] or "")
        topic = topic[4:] if topic.startswith("the ") else topic
        if topic and len(topic.split()) <= 4 and (ep := archive.episode_for_turn(store, sp[0])):
            topics.setdefault(topic, set()).add(ep["id"])
            said.setdefault(topic, (f["obj"] or topic).strip().lower())
    new = []
    for topic, eps in sorted(topics.items()):
        if len(eps) >= PATTERN_EPISODES and topic not in seen:
            text = f"We often talk about {said[topic]}."
            store.save(Memory(content=text, kind="behavior", status="inferred", confidence=0.5, source="auto:pattern"))
            seen.add(topic)   # once only: if it's forgotten, it isn't made again
            new.append(text)
    store.set_meta("patterns_saved", json.dumps(sorted(seen)))
    return new


def priorities(store: MemoryStore, ents: Entities, now: float) -> int:
    """Step 7. Writes one score per memory in use. Returns how many."""
    seed_ids = {ents.find(e["name"]) for e in ents.seeds.get("entities", [])} - {None}
    seeds = ",".join(str(int(i)) for i in seed_ids) or "-1"
    # one pass in SQL: per memory its strongest relationship (most-mentioned entity), whether it names a seed person,
    # and how often it was used; read as a stream (a million rows never sit in memory as objects)
    cur = store.db.execute(
        f"SELECT m.id, m.kind, m.status, m.source, m.updated, COALESCE(u.retrieved, 0), "
        f"(SELECT MAX(e.mentions) FROM relations r JOIN entities e ON e.id=r.b_id WHERE r.a_type='memory' AND r.a_id=m.id "
        f" AND r.rel='mentions' AND r.b_type='entity'), "
        f"EXISTS(SELECT 1 FROM relations r WHERE r.a_type='memory' AND r.a_id=m.id AND r.rel='mentions' "
        f" AND r.b_type='entity' AND r.b_id IN ({seeds})) "
        f"FROM memories m LEFT JOIN usage u ON u.item_type='memory' AND u.item_id=m.id WHERE m.hidden IS NULL")
    n = 0
    while (batch := cur.fetchmany(20_000)):
        rows = []
        for mid, kind, status, source, updated, used, most, seeded in batch:
            sig = 1.0 if kind == "memory" and status == "confirmed" else 0.8 if status == "confirmed" else \
                0.3 if kind == "summary" else 0.4
            ident = 1.0 if status == "confirmed" and (source.startswith("owner:personality") or kind in ("behavior", "preference")) \
                else 0.5 if seeded else 0.0
            parts = {"significance": sig, "identity": ident, "use": min(1.0, used / 5),
                     "relationships": min(1.0, (most or 0) / 10), "recency": math.exp(-(now - (updated or now)) / 86400 / 90)}
            rows.append(("memory", mid, round(sum(WEIGHTS[k] * v for k, v in parts.items()), 3),
                         json.dumps({k: round(v, 2) for k, v in parts.items()}), now))
        with store.db:
            store.db.executemany("INSERT INTO priority(item_type,item_id,score,parts,updated) VALUES (?,?,?,?,?) "
                                 "ON CONFLICT(item_type,item_id) DO UPDATE SET score=excluded.score, parts=excluded.parts, "
                                 "updated=excluded.updated", rows)
        n += len(rows)
    store.set_meta("priorities_at", str(now))   # the context pack is rebuilt with the new ranking
    return n


def run(store: MemoryStore, ents: Entities, embed_model: str | None, judge=None, now: float | None = None,
        max_judge: int = 10, vindex=None) -> dict:
    now = now or time.time()
    t0 = time.perf_counter()
    report: dict = {"at": now, "ok": True}
    if (problems := guard(store)):
        report |= {"ok": False, "problems": problems}
        store.set_meta("consolidation_problem", json.dumps(report))
        return report
    report["episodes"] = archive.build_episodes(store, now)
    if embed_model:
        from memory.vindex import VectorIndex
        vindex = vindex or VectorIndex(store, embed_model)
        if vindex.needs_build():   # big memory: (re)build the FAISS index in quiet time
            report["vector_index"] = vindex.build()
    report["links"], report["repeats"] = link(store, embed_model, vindex)
    report["facts_said_more_than_once"] = support(store)
    report["pairs_judged"], report["conflicts_flagged"] = fact_conflicts(store, judge, max_judge)
    report["patterns"] = patterns(store)
    report["priorities"] = priorities(store, ents, now)
    report["seconds"] = round(time.perf_counter() - t0, 2)
    report["counts"] = counts(store)
    report["check"] = store.check() if report["counts"]["memories"] < 200_000 else "skipped (large)"
    store.set_meta("consolidation_last", json.dumps(report))
    store.set_meta("consolidation_problem", "")
    return report


def repeats(store: MemoryStore) -> list[dict]:
    """Pairs marked as saying nearly the same thing, for review."""
    out = []
    for a, b, w in store.db.execute("SELECT a_id, b_id, weight FROM relations WHERE a_type='memory' AND rel='repeats' "
                                    "AND b_type='memory' ORDER BY weight DESC"):
        ma, mb = store.get(a), store.get(b)
        if ma and mb:
            out.append({"a": ma.model_dump(), "b": mb.model_dump(), "similarity": w})
    return out


def related(store: MemoryStore, mid: int) -> list[dict]:
    rows = store.db.execute("SELECT CASE WHEN a_id=? THEN b_id ELSE a_id END, rel, weight FROM relations "
                            "WHERE a_type='memory' AND b_type='memory' AND (a_id=? OR b_id=?) ORDER BY weight DESC",
                            (mid, mid, mid)).fetchall()
    return [{"memory": m.model_dump(), "rel": rel, "similarity": w} for i, rel, w in rows if (m := store.get(i))]


if __name__ == "__main__":   # PYTHONPATH=. uv run python -m memory.consolidate
    from core.config import load_config
    from memory import conflicts
    cfg = load_config()
    s = MemoryStore(cfg.resolve(cfg.memory_db))
    print(json.dumps(run(s, Entities(s), "nomic-embed-text", conflicts.ollama_judge(cfg.llm.host, cfg.llm.model)), indent=1))
