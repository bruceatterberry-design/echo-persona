"""Phase 6: a synthetic Echo memory database at ~1M memories (made-up text, no personal data), then time Echo's own
code on it. Run with Echo's environment:  PYTHONPATH=. uv run python scripts/bench_scale.py build|bench /tmp/big.sqlite
(~1.1 GB; delete it afterwards)"""
import json, random, sqlite3, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memory.store import MemoryStore  # noqa: E402

N_TURNS, TURNS_PER_NOTE, N_FACT_MEMS, N_ENT, FACTS_PER_MEM = 500_000, 16, 970_000, 50_000, 3
WORDS = [f"w{i}" for i in range(5000)] + "lake fishing dinner school garden truck church music cats weather".split()
PREDS = ["likes", "lives_in", "friend_of", "talked_about", "interested_in", "visited", "is_a", "values"]


def build(path: Path):
    path.unlink(missing_ok=True)
    s = MemoryStore(path)
    db, r, t0 = s.db, random.Random(1), time.time()
    start = time.time() - 10 * 365 * 86400   # ten years of conversation
    with db:
        db.executemany("INSERT INTO turns(role,content,ts) VALUES (?,?,?)",
                       ((("user", "assistant")[i % 2], " ".join(r.choices(WORDS, k=14)), start + i * 630) for i in range(N_TURNS)))
    print(f"turns {time.time() - t0:.0f} s", flush=True)
    with db:   # episodes: 25 turns each
        db.executemany("INSERT INTO episodes(kind,start,end,first_turn,last_turn,turns,created) VALUES ('conversation',?,?,?,?,25,?)",
                       ((start + a * 630, start + (a + 24) * 630, a + 1, a + 25, time.time()) for a in range(0, N_TURNS, 25)))
        db.execute("INSERT INTO meta(key,value) VALUES ('episodes_upto', ?)", (str(N_TURNS),))
    now = time.time()
    notes = ((("summary", f"On day {k} we talked about " + " ".join(r.choices(WORDS, k=18)) + ".", "inferred", 0.6,
               "auto:conversation-summary", f"turns:{k * TURNS_PER_NOTE + 1}-{(k + 1) * TURNS_PER_NOTE}", now, now))
             for k in range(N_TURNS // TURNS_PER_NOTE))
    facts = ((("fact", " ".join(r.choices(WORDS, k=12)) + ".", r.choice(["confirmed", "inferred"]), 0.8, "owner:chat", None, now, now))
             for _ in range(N_FACT_MEMS))
    with db:
        db.executemany("INSERT INTO memories(kind,content,status,confidence,source,span,created,updated) VALUES (?,?,?,?,?,?,?,?)", notes)
        db.executemany("INSERT INTO memories(kind,content,status,confidence,source,span,created,updated) VALUES (?,?,?,?,?,?,?,?)", facts)
    n_mem = db.execute("SELECT MAX(id) FROM memories").fetchone()[0]
    print(f"memories {n_mem} {time.time() - t0:.0f} s", flush=True)
    with db:
        db.executemany("INSERT INTO entities(type,name,mentions,created,by) VALUES (?,?,?,?, 'synthetic')",
                       ((r.choice(["person", "place", "thing", "pet"]), f"Name{e}", 0, now) for e in range(N_ENT)))
        db.executemany("INSERT INTO aliases(entity_id, alias) VALUES (?,?)", ((e + 1, f"name{e}") for e in range(N_ENT)))
        # a few very frequent people (like the owner and the loved one), the rest rare (Zipf-like)
        pick = lambda: min(N_ENT, int(r.paretovariate(1.2)))
        db.executemany("INSERT INTO facts(subject_id,predicate,object_id,value,status,confidence,source,span,memory_id,created) "
                       "VALUES (?,?,?,?,'inferred',0.8,?,?,?,?)",
                       ((pick(), r.choice(PREDS), pick() if r.random() < .5 else None, r.choice(WORDS), f"memory:{m}", "quote", m, now)
                        for m in range(1, n_mem + 1) for _ in range(FACTS_PER_MEM if r.random() < .5 else 1)))
        db.execute("INSERT OR IGNORE INTO relations(a_type,a_id,rel,b_type,b_id,weight,status,created) "
                   "SELECT 'memory', memory_id, 'mentions', 'entity', subject_id, 1.0, 'inferred', created FROM facts")
        db.execute("UPDATE entities SET mentions=(SELECT COUNT(*) FROM relations WHERE b_type='entity' AND b_id=entities.id)")
        db.executemany("INSERT INTO extracted(memory_id,updated,at,model,facts) VALUES (?,?,?,?,1)",
                       ((m, now, now, "synthetic") for m in range(1, n_mem + 1)))
    print(f"facts/relations {time.time() - t0:.0f} s", flush=True)
    db.execute("ANALYZE")
    s.close()
    print(f"built in {time.time() - t0:.0f} s, {path.stat().st_size / 1e9:.2f} GB", flush=True)


class TooSlow(Exception):
    pass


def timed(label, fn, n=3, limit=120):
    import signal
    def stop(*_):
        raise TooSlow
    signal.signal(signal.SIGALRM, stop)
    signal.alarm(limit)
    try:
        t = time.perf_counter(); out = fn(); first = time.perf_counter() - t
        if first * n < 20:
            t = time.perf_counter()
            for _ in range(n):
                out = fn()
            first = (time.perf_counter() - t) / n
        print(f"  {label:58} {first * 1000:10.1f} ms", flush=True)
        return out
    except TooSlow:
        print(f"  {label:58}  > {limit} s (TOO SLOW)", flush=True)
    finally:
        signal.alarm(0)


def bench(path: Path):
    import resource
    from memory import archive, consolidate
    from memory.context import build as pack_build
    from memory.entities import Entities
    from memory.retrieve import Retriever
    s = MemoryStore(path)
    print("counts:", json.dumps(consolidate.counts(s)))
    e = Entities(s, {"self": "the owner", "echo": "Echo", "entities": []})
    r = Retriever(s, e, None)   # vectors: measured separately (scratchpad/ann.py)
    now = time.time()
    frequent = s.db.execute("SELECT name FROM entities ORDER BY mentions DESC LIMIT 1").fetchone()[0]
    rare = s.db.execute("SELECT name FROM entities WHERE mentions BETWEEN 1 AND 3 LIMIT 1").fetchone()[0]
    timed("keyword search (FTS5)", lambda: s.search("lake fishing dinner w42"))
    timed(f"retrieve: frequent name ({frequent}) + topic", lambda: r.retrieve(f"Does {frequent} like fishing at the lake?", now=now), 3)
    timed(f"retrieve: rare name ({rare})", lambda: r.retrieve(f"Who is {rare}?", now=now), 3)
    timed("retrieve: 'what did we talk about yesterday'", lambda: r.retrieve("What did we talk about yesterday?", now=now), 3)
    timed("retrieve: 'on October 29' (a past date)", lambda: r.retrieve("What did we talk about on October 29?", now=now), 3)
    timed(f"facts about the most frequent person", lambda: r.facts_for(e.find(frequent), question="what does he like"), 3)
    timed("about(frequent person): everything", lambda: e.about(frequent), 1)
    timed("turn → episode", lambda: archive.episode_for_turn(s, 250_000))
    from memory.context import _version
    timed("context pack: 'has anything changed?' (every message)", lambda: _version(s))
    timed("context pack build (only when memories change)", lambda: pack_build(s, e, r), 1)
    timed("consolidation guard (counts + quick_check)", lambda: consolidate.guard(s), 1)
    timed("priorities for all memories", lambda: consolidate.priorities(s, e, now), 1)
    print(f"  peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6:.0f} MB")


if __name__ == "__main__":
    (build if sys.argv[1] == "build" else bench)(Path(sys.argv[2]))
