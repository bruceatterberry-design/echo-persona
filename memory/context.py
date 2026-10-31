"""Level 3, the context pack (Phase 5 of docs/MEMORY_ARCHITECTURE.md): what Echo always has in mind.

Replaces "every fact, or the latest 40" in the system prompt with a small, stable block chosen by priority
(memory/consolidate.py), so it stays the same size however much Echo remembers:

  * People and pets: the most-mentioned people/things, each as one line of facts
    ("Sam: married to the owner; a nurse; middle name Lee; likes apple pie").
  * Things I remember: the highest-priority memories in use (not conversation notes), up to a character budget,
    marked "(inferred, not confirmed)" or "(may disagree with another memory)" as before.

Everything else is found when needed by the retrieval engine (memory/retrieve.py), which leaves out what is already
in the pack. The pack only changes when memories change, so the language model can reuse its work on the prompt.
Identity (who Echo is, how he talks) stays in the personality prompt and the owner's approved traits, which already
have their own versions (personality/versions.py).
"""
from __future__ import annotations


from memory.entities import Entities
from memory.retrieve import Retriever
from memory.store import Memory, MemoryStore, life_details

BUDGET_CHARS = 3000   # ≈ 750 tokens for "Things I remember"
PEOPLE = 8            # people/things with their own line
FACTS_EACH = 6


def _version(store: MemoryStore) -> str:
    """Changes whenever a memory, fact or priority changes."""
    # the database counts every change to memories, facts and contradiction flags (triggers in memory/store.py); the
    # nightly priority run is marked separately. One cheap read per message, however big the memory gets.
    return store.get_meta("changes", "0") + "/" + store.get_meta("priorities_at", "0")


def people(store: MemoryStore, ents: Entities, recall: Retriever) -> list[str]:
    rows = store.db.execute("SELECT id, name, type FROM entities WHERE type IN ('person','pet','self') "
                            "ORDER BY mentions DESC, id LIMIT ?", (PEOPLE,)).fetchall()
    out = []
    for eid, name, _type in rows:
        if (f := recall.facts_for(eid, FACTS_EACH)):
            out.append(f"- {name}: " + "; ".join(f))
    return out


def chosen(store: MemoryStore, budget: int = BUDGET_CHARS) -> list[Memory]:
    """Highest priority first until the budget is used; then shown in the order they were saved (stable)."""
    n = max(50, budget // 20)
    # the database picks the top ones by importance (its index), plus the newest not scored yet: never all of them
    top = [r[0] for r in store.db.execute("SELECT item_id FROM priority WHERE item_type='memory' ORDER BY score DESC LIMIT ?",
                                          (n * 3,))]
    new = [r[0] for r in store.db.execute("SELECT id FROM memories m WHERE kind NOT IN ('summary','knowledge') AND hidden IS NULL AND NOT EXISTS "
                                          "(SELECT 1 FROM priority p WHERE p.item_type='memory' AND p.item_id=m.id) "
                                          "ORDER BY id DESC LIMIT ?", (n,))]
    got = store.get_many(top + new)
    mems = [got[i] for i in dict.fromkeys(top + new) if i in got and got[i].kind not in ("summary", "knowledge")]
    picked, used = [], 0
    for m in mems:
        cost = len(m.content) + 40
        if used + cost > budget:
            continue
        picked.append(m)
        used += cost
    return sorted(picked, key=lambda m: m.id)


def build(store: MemoryStore, ents: Entities, recall: Retriever, budget: int = BUDGET_CHARS) -> tuple[str, set[int]]:
    """(text for the system prompt, ids of the memories in it)."""
    conflicted = store.conflicted_ids()
    lines = []
    if (ppl := people(store, ents, recall)):
        lines += ["People and pets I know (read from my notes; some of it inferred):", *ppl, ""]
    mems = chosen(store, budget)
    if mems:
        lines.append("Things I remember (from the owner):")
        lines += [f"- {m.content}{life_details(m)}" + ("" if m.status == "confirmed" else " (inferred, not confirmed)")
                  + (" (may disagree with another memory; not sure which is right)" if m.id in conflicted else "")
                  for m in mems]
    else:
        lines.append("Things I remember: (nothing saved yet)")
    return "\n".join(lines), {m.id for m in mems}


class Pack:
    """Cached pack: rebuilt only when the memories change."""

    def __init__(self, store: MemoryStore, ents: Entities, recall: Retriever, budget: int = BUDGET_CHARS):
        self.store, self.ents, self.recall, self.budget = store, ents, recall, budget
        self.ver, self.text, self.ids = None, "", set()

    def stale(self) -> bool:
        return self.ver != _version(self.store)

    def get(self, refresh: bool = True) -> tuple[str, set[int]]:
        """With refresh=False the pack stays as it was (unless there is none yet): during a conversation a changed
        pack would make a checkpoint-only model (qwen3.5) re-read the whole prompt (~20 s). New memories still reach
        the model through retrieval meanwhile; the quiet-time loop refreshes the pack and re-reads it."""
        if self.ver is None or (refresh and self.stale()):
            self.ver = _version(self.store)
            self.text, self.ids = build(self.store, self.ents, self.recall, self.budget)
        return self.text, self.ids
