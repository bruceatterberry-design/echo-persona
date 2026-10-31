# Echo memory architecture (proposal, 2026-10-30)

Status: **design for the owner to approve. Nothing here is built yet** except what "Today" describes.
The brief: the "Advanced Symbolic Memory & Retrieval Architecture" prompt (the owner, 2026-10-30).
Every number below was measured on this Mac on 2026-10-30, or is labelled as an estimate with its assumptions.

---

## 0. The short version

- Echo already has a sound base: one SQLite database with facts, conversation notes, keyword search (FTS5), search
  by meaning (nomic-embed-text vectors), duplicate merging, contradiction flags and a review queue. **Keep it and grow
  it.** Don't replace it with a stack of separate servers.
- **The originals are what take up space, not the memory.** Echo's whole memory today (68 memories + 624 conversation
  turns) is 67 KB of text. The 1.4 GB on the SSD is recordings and photos. At 10 TB, almost all of it will be video,
  audio and photos, which are already compressed. "Symbolic compression" will not shrink those. What it does is make
  terabytes **searchable from a small index (about 0.1–0.5 % of the archive's size)** that fits on the Mac's internal
  drive and in RAM. That's the realistic goal, and it meets the brief: fast retrieval without loading the archive.
- Four memory systems (episodic, semantic, identity, relational) map onto **three levels**: L1 originals (never
  changed, checksummed), L2 symbols (entities, facts, relations, time, vectors; each pointing back to L1),
  L3 a small context pack loaded into every conversation.
- Built in **8 phases**, each small enough for one session, tested, and approved by the owner before the next.
  **Phase 0 fixes 3 places where Echo can lose memories today** (section 2).

---

## 1. What exists today (inspected 2026-10-30)

| Part | Where | What it does |
|---|---|---|
| Durable memories | `memory/store.py`, table `memories` | fact / preference / person / project / correction / behavior / summary / memory; confirmed vs inferred; confidence; source; life details (period, when, year, people, place, told_by) |
| Keyword search | FTS5 `memories_fts` | OR of words ≥ 3 letters, ranked |
| Search by meaning | `memory/embed.py`, table `vectors` | nomic-embed-text through Ollama (274 MB), 768-dim float32, brute-force cosine |
| Duplicates | `store.add` / `find_duplicate` | word overlap ≥ 0.85 → merge; an inferred memory can be upgraded to confirmed, never downgraded |
| Contradictions | `memory/conflicts.py`, table `conflicts` | related pairs judged by the local LLM, flagged for the owner; never auto-resolved |
| Conversation log | table `turns` | every message; the last 20+ go to the LLM |
| Conversation notes | `memory/summarize.py` | turns that slide out of view → 1–3 sentence "summary" memories (inferred, 0.6) while nobody is talking |
| Life-memory suggestions | `memory/candidates.py`, table `candidates` | from the owner's recordings, with his exact quote as evidence; wait for approval |
| Curiosity | `memory/curiosity.py` | Echo asks the owner about gaps in his life story |
| Identity | `config/personality.example.yaml` + `data/personality/` (versions, approved traits, evals) | base prompt + the owner-approved traits with quotes; versioned |
| Originals | SSD `EchoPersona-Data/raw/` (read-only, sha256) | 834 MB phone, 176 MB voice, 11 MB face, 0.8 MB speakers |
| Backups | `docs/BACKUP_AND_RECOVERY.md` | SSD + internal copy + Time Machine + encrypted off-site sparsebundle; DB snapshots |

**Live database:** 31 confirmed facts and 37 inferred conversation notes (10.7 KB of text), 68 vectors, 624 turns
(27–30 Oct, 56 KB), **68 life-memory suggestions waiting for the owner's review**, 0 memories with people filled in.

**Prompt assembly** (`core/server.py` `build_messages`): personality + traits + all facts (the latest 40 once there
are more) + the 3 latest notes; then up to 2 extra memories matching the message (by meaning first, then keywords).

### Limits found

1. **"Clear conversation" deletes the original turns** (`DELETE FROM turns`). The originals are lost.
2. **Edits overwrite.** `update`/`correct` replace the text in place, and `forget` / "keep A" delete rows. There's no
   history of what a memory used to say.
3. **No integrity check on the database**, unlike the recordings (no checksums, no `PRAGMA integrity_check` in backups).
4. Facts in the prompt are capped at "latest 40": older facts only come back through 2 search slots.
5. No people, places or events as things in their own right, so nothing can be linked ("everything about Tom").
6. No time search ("what did we talk about last Christmas?"); turns aren't grouped into conversations.
7. Brute-force vectors in Python are fine to about 100k items (4.6 ms, measured below). Past that, the vectors need RAM.
8. Notes summarize conversation but don't link back to the turns they came from.

---

## 2. Proposed architecture

```
                         ┌──────────────── conversation (core/server.py) ───────────────────┐
  message ─► subject     │  L3 context pack (≤ 1,500 tokens, always loaded)                   │
             extraction ─┤  + retrieved L2 symbols (top 5–8, with sources)                    │──► LLM (llama3.1:8b)
             (entities,  │  + L1 quotes when exact words matter                               │
              time, kind)└───────────────────────────────▲───────────────────────────────────┘
                                                         │ Retrieval engine (section 4)
     ┌───────────────────────────────────────────────────┴─────────────────────────────────────┐
     │ L2 SYMBOLIC (internal SSD, SQLite memory.sqlite + vector index file)                    │
     │  entities · aliases · facts (subject–predicate–object) · relations · episodes ·         │
     │  memories (today's table, kept) · vectors · priorities · versions · provenance          │
     └───────────────────────▲──────────────────────────────────────────┬─────────────────────┘
          consolidation (idle, │ nightly): extract · link · merge ·       │ every symbol points to
          contradictions · priorities · L3 rebuild                        ▼ its source (sha256 + span)
     ┌─────────────────────────────────────────────────────────────────────────────────────────┐
     │ L1 ORIGINALS (external SSD → later more drives; append-only, checksummed, read-only)      │
     │  conversations (turn archive, zstd JSONL by month) · recordings · photos · documents ·     │
     │  transcripts · research sources                                                         │
     └─────────────────────────────────────────────────────────────────────────────────────────┘
```

The four memory systems on top of the three levels:

| System | L1 | L2 | L3 |
|---|---|---|---|
| **Episodic** (what happened) | turn archive, recordings | `episodes` (when, who, where, summary, source span) | latest few episodes |
| **Semantic** (what is true) | where it was said | `facts` + `memories` + `entities` | top facts by priority |
| **Identity** (who Echo is) | the owner's own words | approved traits, values, reflections; `identity_versions` | the identity core |
| **Relational** (what connects) | — | `relations` (entity↔entity, episode↔entity, fact↔fact) | the people the loved one and the owner talk about most |

**Separation of working and long-term memory:** at runtime the core keeps only the L3 pack, the vector index (or
its compressed form) and SQLite's page cache in RAM. L1 is opened only to quote an original.
**Model weights never change**: everything Echo learns lives in these tables. This is true today and stays true.

---

## 3. Data structures and schemas (L2, SQLite)

Today's tables stay as they are. New tables are added beside them through the existing migration path, which
backs up the database before any schema change.

```sql
-- L1 catalogue: every original, by content hash (the files themselves stay where they are)
CREATE TABLE sources (
  sha256 TEXT PRIMARY KEY, kind TEXT NOT NULL,          -- conversation_month | recording | photo | document | web
  path TEXT NOT NULL, bytes INTEGER, created REAL, added REAL NOT NULL,
  consent TEXT, verified REAL                            -- last time the checksum was re-checked
);

-- Episodes: one conversation, recording session, event or day
CREATE TABLE episodes (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL,            -- conversation | recording | event | document
  start REAL NOT NULL, end REAL, place_id INTEGER,
  summary TEXT NOT NULL,                                 -- 1–3 sentences, inferred
  source TEXT NOT NULL, span TEXT,                       -- sha256 + "turns 812–840" / "00:03:10–00:05:42"
  priority REAL NOT NULL DEFAULT 0.5, created REAL NOT NULL
);
CREATE INDEX episodes_time ON episodes(start);

-- Entities: people, places, things, organisations, pets, events, concepts
CREATE TABLE entities (
  id INTEGER PRIMARY KEY, type TEXT NOT NULL,            -- person | place | thing | org | pet | event | concept
  name TEXT NOT NULL, note TEXT,                         -- "the owner's brother", "the lake house"
  priority REAL NOT NULL DEFAULT 0.5, first_seen REAL, last_seen REAL, mentions INTEGER DEFAULT 0
);
CREATE TABLE aliases (entity_id INTEGER NOT NULL, alias TEXT NOT NULL COLLATE NOCASE, PRIMARY KEY(alias, entity_id));

-- Facts: compact symbolic statements (subject – predicate – object), each with provenance
CREATE TABLE facts (
  id INTEGER PRIMARY KEY,
  subject_id INTEGER NOT NULL, predicate TEXT NOT NULL,  -- predicate from a small controlled list (below)
  object_id INTEGER, value TEXT,                         -- another entity, or a literal ("1974", "blue")
  valid_from TEXT, valid_to TEXT,                        -- when it was true, as said ("1974", "until 2001")
  status TEXT NOT NULL,                                  -- original | confirmed | inferred | learned
  confidence REAL NOT NULL, source TEXT NOT NULL, span TEXT,
  memory_id INTEGER,                                     -- the human-readable memory it came from, if any
  created REAL NOT NULL, superseded_by INTEGER           -- never overwritten: a correction adds a new row
);
CREATE INDEX facts_s ON facts(subject_id, predicate); CREATE INDEX facts_o ON facts(object_id);

-- Relations: any link between two things (entity, episode, memory, fact)
CREATE TABLE relations (
  a_type TEXT, a_id INTEGER, rel TEXT, b_type TEXT, b_id INTEGER,
  weight REAL NOT NULL DEFAULT 1.0, status TEXT NOT NULL, source TEXT, created REAL NOT NULL,
  PRIMARY KEY(a_type, a_id, rel, b_type, b_id)
);
CREATE INDEX relations_b ON relations(b_type, b_id);

-- History: every change to a memory or fact, so nothing is ever silently overwritten or lost
CREATE TABLE versions (
  id INTEGER PRIMARY KEY, item_type TEXT NOT NULL, item_id INTEGER NOT NULL,
  action TEXT NOT NULL,                                  -- create | edit | correct | forget | merge | resolve
  before TEXT, after TEXT,                               -- JSON
  by TEXT NOT NULL, at REAL NOT NULL, reason TEXT
);

-- Priority and use
CREATE TABLE usage (item_type TEXT, item_id INTEGER, retrieved INTEGER DEFAULT 0, used REAL, PRIMARY KEY(item_type, item_id));

-- Identity: versioned core, written only by the owner (or with his approval)
CREATE TABLE identity_versions (id INTEGER PRIMARY KEY, at REAL NOT NULL, by TEXT NOT NULL, core TEXT NOT NULL, note TEXT);
```

**Forgetting stays possible.** the owner or the loved one asking to forget something is a deliberate choice, not corruption.
"Forget" removes the item from L2 and L3 and records a `versions` row ("forgotten by the loved one, date"). It keeps the
content only if the owner chooses "hide" rather than "delete". This must be settled before Phase 0, because privacy
(a true delete) and continuity (keep everything) pull in opposite directions.
**Decided (the owner, 2026-10-31): forget = hide.** A forgotten item leaves L2/L3 and the prompt, but its content is kept
and can be restored.

**Status values** answer section 7 of the brief, "original vs inferred vs learned":
`original` = said in a source; `confirmed` = the owner or the loved one approved it; `inferred` = Echo concluded it
(consolidation, summaries); `learned` = from authorized research. The prompt shows them differently, as it already
does for confirmed vs inferred.

---

## 4. Symbolic encoding strategy

1. **Entities with IDs instead of repeated names.** "My brother Tom", "Tom", "Thomas" → one `entities` row with
   aliases. Everything about Tom is one index lookup.
2. **Facts as triples** with a small controlled list of predicates (about 40, grown on review): `is_a`, `named`,
   `born_in`, `lived_in`, `worked_at`, `married_to`, `parent_of`, `sibling_of`, `friend_of`, `likes`, `dislikes`,
   `owns`, `happened_at`, `happened_on`, `took_part`, `said`, `feels_about` … Anything that doesn't fit stays as a
   natural-language `memory` (today's table). **Nothing is forced into symbols.**
3. **Time as data:** absolute where known (`1974-07`), relative text kept verbatim ("when I was eight"), resolved
   later when the birth year is known. That makes time search an indexed range query.
4. **Hierarchy:** turn → episode → day/month summaries → life period. Each level points down, so Echo can zoom in
   from "the summer of 1974" to the words that were said.
5. **Vectors** on memories, episode summaries and facts (rendered as a sentence). Option to shrink to 256 dims and
   int8 (nomic-embed-text v1.5 is trained to be cut short; still to check that Ollama's build is v1.5), about 12×
   smaller. The index is a cache: it can always be rebuilt from SQLite.
6. **Deduplication:** what Echo does today (word overlap ≥ 0.85), plus meaning (cosine ≥ 0.92 *and* the same
   entities), plus identical originals by sha256. Merges are logged in `versions` and can be undone.
7. **Extraction** (L1 → L2) by the local LLM, in the pattern `candidates.py` already uses: every extracted fact must
   quote its source span. A fact whose quote isn't really there is dropped. Extracted facts start `inferred`.
   Facts about the owner's life go to the review queue; low-stakes facts (a topic mentioned) are kept as inferred.

**What compression can and can't do here** (to be measured in Phase 3, not promised): text → L2 symbols saves space
on long transcripts. 1 hour of talk ≈ 9,000 words ≈ 50 KB, which becomes perhaps 2–5 KB of symbols. Media can't be
shrunk without losing quality, and **originals are never replaced by symbols.** So "storage reduction" in the brief
means the **active** footprint (what must be on the fast drive and in RAM), not the archive.

---

## 5. Retrieval engine

```
message
  1 subject:   aliases lookup (exact, ~0.1 ms) + time words ("last summer", "1974") + kind (who/when/where)
  2 symbols:   facts/relations of matched entities (indexed)          ┐
               vector search (top 30)                                 │ all in parallel,
               FTS5 keyword (top 30)                                  │ budget ≤ 60 ms total
               time range on episodes (indexed)                       ┘
  3 expand:    1 hop in relations from the best hits (Tom → Tom's wife, the lake trip)
  4 score:     fused rank (RRF) × status weight (confirmed > original > inferred)
               × priority × recency (episodes only) × "who's speaking" (the loved one's own memories rank higher for the loved one)
  5 select:    top 5–8 non-duplicates under a token budget (≈ 600 tokens); keep sources
  6 original:  when exact words matter ("what exactly did I say…"), fetch the L1 span
  7 answer:    LLM sees each item with its status + date; prompt rule: say "I'm not sure" when only inferred
```

Accuracy comes first. A hit is kept only if it clears a fused-score floor, tuned on a question set (section 9).
Three good memories beat eight loose ones, and that's already how today's `MEANING_MIN` works.

**Measured on this Mac today (synthetic 768-dim vectors, Python/numpy, one query):**

| Items | float32 brute force | RAM | int8 brute force | RAM | 1-bit + rerank | RAM |
|---|---|---|---|---|---|---|
| 10,000 | 0.4 ms | 31 MB | 2.2 ms | 8 MB | 0.8 ms | 1 MB |
| 100,000 | 4.6 ms | 307 MB | 22.6 ms | 77 MB | 9.0 ms | 10 MB |
| 300,000 | 15.7 ms | 922 MB | 69.0 ms | 230 MB | 25.7 ms | 29 MB |

| Items | SQLite FTS5 build | query | file |
|---|---|---|---|
| 100,000 | 1.3 s | 0.1 ms | 48 MB |
| 1,000,000 | 14.8 s | 1.3 ms | 465 MB |

Read-outs: float32 brute force is fast but RAM-hungry. numpy int8 is *slower* (no fast int8 path), so int8 needs a
real index library. The 1-bit test on random data found only 30–70 % of the true top 10; real embeddings cluster
and usually do far better, which needs measuring on real data. **Up to ~100k items, today's code is fine.** Past
that, use an approximate index (HNSW), chosen by benchmark in Phase 6. FTS5 stays fast to millions.

---

## 6. Consolidation (idle time and nightly, never during a conversation)

Runs when nobody has spoken for 10 minutes, like `summarize.py` now, and as a nightly pass at 3 am:

1. **Group** new turns into episodes (gap > 20 min = new episode); summary with its source span.
2. **Extract** entities and facts with quotes (section 4.7); resolve aliases (exact → known alias → LLM check against
   the 3 nearest entities, else a new entity marked "maybe the same as …" for review).
3. **Link**: shared entities, same day or place, meaning ≥ threshold → `relations` (inferred, weighted).
4. **Patterns**: an entity or topic coming back in ≥ 3 episodes → a pattern note ("the loved one often mentions her class's
   reading progress"), inferred, for the owner or the loved one to confirm.
5. **Contradictions**: today's `conflicts.py`, extended to facts (same subject + predicate, different object).
6. **Duplicates**: merge with history (section 4.6).
7. **Priority** update (section 7), then **rebuild L3**.
8. **Integrity**: checksums of new originals, `PRAGMA quick_check`, row counts; a short report on the Status page.

Each pass is resumable (a "done up to" marker, like `summarized_upto`) and capped per run, so a big import never
locks up the Mac. **Nothing is deleted. Nothing is confirmed without a person.**

---

## 7. Priority system

`priority = 0.35·significance + 0.25·identity + 0.15·frequency + 0.15·relationship + 0.10·recency`, each 0–1:

| Class | How it's recognised | Floor |
|---|---|---|
| Critical identity | in the identity core, or approved trait / value | 0.95 (never decays) |
| Important life experiences | kind=memory, confirmed, told by the owner | 0.8 |
| Long-term relationships | entity with ≥ 5 episodes over ≥ 30 days | 0.7 |
| Frequently used | retrieved and used often (`usage`) | rises with use |
| Learned concepts | status=learned, confidence ≥ 0.7 | 0.4 |
| Temporary context | "tomorrow", "this week" (valid_to in the past) | decays after its date |
| Low-priority archive | everything else | 0.1 |

Priority only decides **ranking and what goes into L3**. It never decides deletion. the owner can pin or lower anything
on the "My memories" page. The weights live in `config/memory.yaml` and are adjustable as Echo develops.

---

## 8. Integrity, provenance and security

- **Provenance on every symbol:** `source` (sha256 or `owner:chat`) + `span` + `status` + `confidence` + `created`.
- **History:** `versions` table (section 3). Corrections add a new fact and point the old one to it via
  `superseded_by`. Echo can say "I used to think X; the owner corrected me in 2027."
- **Originals:** append-only and read-only (as `raw/` is today). The conversation archive is written monthly as
  zstd JSONL, checksummed and listed in `sources`. "Clear conversation" clears only Echo's *view*, not the archive
  (Phase 0; the owner to confirm).
- **Database safety:** WAL mode (already), SQLite online backup API for snapshots (already), plus `integrity_check`
  before each backup and after each restore, a schema version in `meta`, and migrations that back up first (already).
- **Encryption:** backups off the Mac are encrypted (today: AES-256 sparsebundle). On the Mac, FileVault covers the
  internal drive. The external SSD should also be encrypted (to check: APFS encrypted?).
- **No fabrication:** symbols without a valid quote are dropped (already the rule for candidates). The prompt marks
  inferred items, and the LLM is told to say when it isn't sure.

---

## 9. Continuous learning (authorized sources only)

acquire → analyse → extract → symbolise → associate → consolidate → evaluate → update, as a **job queue** in SQLite
(`jobs`: source, step, status, error) so every step is resumable and visible on the Status page.

Sources, in order of trust: the owner's own words (recordings, chat) → the loved one's → documents the owner adds → web research that
the owner or the loved one asked for (status `learned`, source URL + fetched copy kept in L1 + date). Research results never
become facts about the owner's life. Everything new about the owner's life goes through the review queue (as now).

---

## 10. Storage, RAM and processing estimates

**Assumptions** (estimates, to be corrected with real data as it grows). The archive is mostly media. On average
1 GB of archive yields about **200 retrievable items** (video ≈ 15 chunks/GB of transcript, audio ≈ 45/GB,
photos ≈ 300/GB, documents/text ≈ 1,000+/GB), plus about 3 facts and 2 relations per item. Vectors are 256-dim int8
(after the Phase 6 check; float32 768-dim would be 12× bigger).

| Archive (L1) | Items | Facts | L2 SQLite (text, facts, FTS) | Vector index | L2 total (internal SSD) | RAM, working set |
|---|---|---|---|---|---|---|
| 100 GB | 20 k | 60 k | ~ 60 MB | ~ 25 MB | **~ 0.1 GB** | ~ 150 MB |
| 1 TB | 200 k | 600 k | ~ 0.6 GB | ~ 0.25 GB | **~ 0.9 GB** | ~ 400 MB |
| 5 TB | 1 M | 3 M | ~ 3 GB | ~ 1.2 GB | **~ 4.5 GB** | ~ 1.0 GB |
| 10 TB | 2 M | 6 M | ~ 6 GB | ~ 2.5 GB | **~ 9 GB** | ~ 1.5 GB |

(SQLite size from the FTS5 measurement: ~465 MB per million 40-word items; facts are smaller. Vector index =
items + facts × 256 bytes × 1.5 for HNSW links. RAM = vector index or its graph + SQLite page cache 64–256 MB +
L3 pack.) So **the symbolic layer is about 0.1 % of the archive**, and even 10 TB fits comfortably on the internal
drive and in a 1–1.5 GB RAM budget next to the LLM, voice and speech models.

**Processing:** a query is ≤ 60 ms (targets: alias + FTS + time < 5 ms; vector ≤ 30 ms at 2M with HNSW, to be
measured). Ingestion is the expensive part: LLM extraction at ~1–3 s per chunk on llama3.1:8b. At 10 TB
(2M items) that is weeks of idle-time processing, which is fine for a background job, but plan for it.
Embedding: nomic ~15 ms per query, batches much faster per item.

**Hardware limits, plainly:**
- Internal SSD: 228 GB, **34 GB free** today. Fine for L2 up to 10 TB, but the free space needs watching.
- External SSD: 1.6 TB, 922 GB free. L1 beyond ~0.9 TB needs more drives (5–10 TB = 1–2 large external drives +
  backup copies of them). Time Machine doesn't back up the SSD (known; see BACKUP_AND_RECOVERY.md).
- RAM 16 GB: today's services use ~7 GB (2 × Python ~2.6 GB for speech/voice, llama-server ~1.5 GB + the model).
  The memory system's budget: **≤ 1.5 GB**.

---

### Measured at scale (Phase 6, 2026-10-31; replaces the estimates above where they differ)
- 1M memories + 2M facts + 500k turns: SQLite 1.1 GB (text, facts, FTS, indexes), Echo's lookups 0.1–75 ms, 81 MB RAM.
- Vectors: 768-dim int8 = 0.77 GB per million on disk; FAISS IVF-PQ index 221 MB in RAM for **2M** (97 % recall, 1.7 ms).
- So 2M items (≈ the 10 TB row) needs ≈ 2.5 GB SQLite + 1.5 GB vectors on disk and ≈ 0.3 GB RAM for search.
- Vectors are **not** cut to 256 dims (that kept only 76 % of the same results on real data); int8 instead.

## 11. Recommended software (compared)

| Need | Options compared | Recommendation | Why |
|---|---|---|---|
| System of record | SQLite · Postgres · DuckDB | **SQLite (keep)** | already used, one file, WAL, FTS5, online backup, zero servers; billions of rows OK |
| Graph | SQLite tables + recursive CTE · Kùzu (embedded) · Neo4j (server, Java) | **SQLite tables** | Echo needs 1–2 hops, not graph analytics; Kùzu's project was archived (Oct 2025, to re-check); Neo4j = a JVM server using RAM |
| Vectors ≤ 100k | numpy brute force (now) · sqlite-vec | **keep numpy** | measured 4.6 ms at 100k |
| Vectors > 100k | usearch · FAISS (measured) · hnswlib · LanceDB · sqlite-vec | **FAISS IVF-PQ + int8 re-check** (measured 2M: 97 %, 1.7 ms, 221 MB RAM; usearch HNSW only 59–70 % on clustered data) | a rebuildable cache, not the record; add when Echo passes ~100k memories (the owner to approve faiss-cpu) |
| Embeddings | nomic-embed-text (now) · bge-small · mxbai | **keep nomic** | already installed; Matryoshka 256-dim if v1.5 |
| Extraction | llama3.1:8b (now) · optional Claude helper (exists, capped) | **keep, local first** | privacy; helper only for non-sensitive jobs, as now |
| Archive compression | zstd · gzip | **zstd** (text archives only) | fast, good ratio; media stored as-is |
| Backups | current scripts + Time Machine · restic | **current + add restic later?** (the owner to decide) | restic = encrypted, deduplicated, incremental; one more tool to look after |

New dependencies, all to be approved before installing: `zstandard` (Phase 1), and an ANN library (Phase 6), maybe
`restic` (Phase 7). Nothing in Phases 0–5 needs new downloads.

---

## 12. Implementation roadmap (one phase per session; test, measure, the owner approves, then the next)

| Phase | What | Done when |
|---|---|---|
| **0. Never lose a memory** ✅ 2026-10-31 | `versions` table; edit/correct/forget/merge write history; "Clear conversation" archives instead of deleting (after the owner confirms); `integrity_check` in backup/restore; schema version | tests; a clear + restore round-trip loses nothing |
| **1. L1 catalogue + episodes** ✅ 2026-10-31 (xz instead of zstd: no install) | `sources`; monthly zstd turn archive; turns → episodes with spans; link today's summaries to their turns | every turn findable from an episode and back |
| **2. Entities + facts** ✅ 2026-10-31 (92.6 % hand-checked) | `entities`, `aliases`, `facts`, `relations`; extraction with quotes over existing memories and approved candidates | precision on a hand-checked sample ≥ 90 %; "everything about the loved one" works |
| **3. Retrieval engine v2 + evaluation** ✅ 2026-10-31 (hit 85→95 %, quiet 2→7/10, 1.6 ms) | section 5; 100-question test set made with the owner (who / when / where / exact words / connections) | beats today's retrieval on the set; latency ≤ 60 ms; numbers in BENCHMARKS.md |
| **4. Consolidation** ✅ 2026-10-31 (week unattended: pending) | section 6 as an idle + nightly job; patterns, fact contradictions | runs unattended for a week; report on the Status page |
| **5. Identity + L3 pack** | `identity_versions` (reuses personality versions); priorities; L3 rebuilt nightly; replaces "latest 40 facts" | conversation quality check with the owner and the loved one |
| **6. Scale** ✅ 2026-10-31 (measured; FAISS not yet in Echo) | synthetic 1M / 2M-item benchmark; ANN library chosen by measurement; vector compression check on real data | latency/RAM table at 1M and 2M |
| **7. Learning + backups at scale** ✅ 2026-10-31 | job queue; documents and authorized research; backup/restore drill at scale; restic decision | measured backup and restore times |
| Then | port to the public Echo Persona repo, scrubbed as before | |

---

## 13. Benchmarking strategy

Script `scripts/bench_memory.py` (to write in Phase 3), results appended to `docs/BENCHMARKS.md` with date and commit:

| Measure | How |
|---|---|
| Storage reduction | bytes of L1 text vs L2 rows for the same sources; L2 size vs archive size |
| Retrieval latency | p50 / p95 per stage over the question set; warm and cold (after restart) |
| Search accuracy | question set with expected memories: hit@5, MRR; "should say I don't know" questions count wrong answers |
| Reconstruction | for 50 facts, follow the source span back to L1: does the original support it? (hand-checked) |
| RAM / CPU | peak RSS of the core and the consolidation job; CPU seconds per 1,000 items ingested |
| Growth | DB and index size per 1,000 items, tracked weekly on the Status page |
| Backup / recovery | snapshot time, restore time, `integrity_check` result, all on a copy |
| Scale | synthetic corpora of 100k / 1M / 2M items (made-up text, never personal data) |

---

## 14. Backup and recovery design

Keeps today's design (BACKUP_AND_RECOVERY.md) and adds:

1. **Memory DB:** an online-backup snapshot nightly after consolidation (keep 14 daily, 12 monthly, all yearly),
   each checked with `integrity_check` and given a checksum. The vector index is not backed up (rebuildable).
2. **L1 archive:** append-only, so incremental copies are cheap; every file is checksummed in `sources`; the
   monthly turn archive joins the dataset backup and the off-site encrypted copy.
3. **Recovery order:** restore the DB snapshot → `integrity_check` → replay any turns newer than the snapshot from
   the turn archive → rebuild vectors and L3 → compare counts with the last report.
4. **Drills:** a restore onto a copy every quarter (scripted), with timings recorded.
5. **Corruption guard:** consolidation refuses to run if `quick_check` fails or counts drop unexpectedly, and
   tells the owner on the Status page.
