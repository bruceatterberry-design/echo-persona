"""Retrieval engine v2 (Phase 3 of docs/MEMORY_ARCHITECTURE.md): which memories go with a message.

Four ways of finding a memory, fused by rank (reciprocal rank fusion), then filtered so that only memories the message
really points to are kept (three good memories beat eight loose ones):

  * meaning:  nomic-embed-text cosine (as before), kept from MEANING_MIN, or a little lower for a named person/thing;
  * words:    FTS5 on the message's meaningful words; kept with 2 shared words, or 1 for a named person/thing;
  * who/what: people, places and things named in the message (aliases: "Sammy" → the loved one), "my/I" → whoever is speaking
              (recognised by voice), "you/your" → Echo. A frequent name (the owner, the loved one, Echo) only counts together with
              something else in common; a rare one ("Jordan Price", "Biscuit") or "tell me about X" is enough on its own;
  * when:     "yesterday", "on Thursday", "October 29", "this morning", "3 days ago", "last Christmas" → the
              conversations in that time → the notes made from them (Phase 1 spans).

Also returns compact facts about the named people/things ("Sam: married to the owner; a nurse; middle name Lee"),
for the prompt. Everything is local; no network except the query vector the caller already made.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from memory import archive
from memory.entities import Entities, norm
from memory.store import Memory, MemoryStore

MEANING_MIN = 0.62        # same as before (core/server.py): below this, matches were mostly unrelated
MEANING_WITH_NAME = 0.55  # a named person/thing in common lowers the bar a little
STANDOUT_Z = 2.0          # meaning alone counts only if the memory stands out from all others (z-score); measured
                          # 2026-10-31: answers z 1.7–5.9, "I don't know" questions about the owner/the loved one z 1.6–1.9
KEEP_FRACTION = 0.3       # drop hits scoring under this share of the best one (padding)
RRF_K = 60
RARE_MENTIONS = 3         # an entity mentioned in at most this many memories counts on its own
STOP = set("""a about after again all also am an and any are as at be because been before being but by can could
did do does doing down for from get got had has have having he her here hers him his how i if in into is it its just
know like me more my no not now of off on once only or our out over own really remember said say she so some still
such talk talked tell than that the their them then there these they thing things think this those through to today
too up us very was we well were what when where which while who why will with would yes yesterday you your day days
time good great nice okay discuss discussed ever""".split())
ABOUT = re.compile(r"\b(?:who is|who's|tell me about|what do you know about|what about|do you remember)\s+(.+?)[.?!]*$")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
FACT_WORDS = {"married_to": "married to", "is_a": "a", "has_name": "name", "likes": "likes", "dislikes": "dislikes",
              "favorite": "favourite", "lives_in": "lives in", "from": "hometown", "works_as": "works as",
              "first_job": "first job", "owns": "has", "pet_of": "pet of", "created": "made", "created_by": "made by",
              "interested_in": "interested in", "values": "values", "believes": "believes", "cannot": "can't",
              "can": "can", "wants": "wants", "teaches": "teaches", "parent_of": "parent of", "child_of": "child of",
              "sibling_of": "sibling of", "friend_of": "friend of", "relative_of": "family of", "plans": "plans",
              "works_at": "works at", "visited": "visited"}
# a fact read from its object's side: "the owner created Echo" → Echo: "made by the owner"
INVERSE = {"created": "created_by", "created_by": "created", "married_to": "married_to", "parent_of": "child_of",
           "child_of": "parent_of", "sibling_of": "sibling_of", "friend_of": "friend_of", "relative_of": "relative_of",
           "owns": "pet_of", "pet_of": "owns"}
# question words → the kinds of fact that answer them (to put the right facts first)
ASKS = {"married_to": r"married|wife|husband|spouse", "has_name": r"name|called", "lives_in": r"live|address|home",
        "from": r"\bfrom\b|hometown|gr[eo]w(ing)? up|raised|childhood|born", "works_as": r"job|work|do for a living|career", "first_job": r"first job",
        "is_a": r"what is|who is|what are|who are|what does .* do|teach|job|work", "likes": r"like|love|enjoy|favou?rite|prefer",
        "favorite": r"favou?rite", "owns": r"pet|cat|dog|own|have", "pet_of": r"pet|cat|dog",
        "created": r"made|created|built|designed", "created_by": r"made|created|built|designed|who made",
        "interested_in": r"interest|into|hobby|work on", "values": r"value|care|important", "believes": r"believe|think"}


def words(text: str) -> set[str]:
    ws = (re.sub(r"'s$", "", w) for w in re.findall(r"[a-z']+", text.lower().replace("’", "'")))
    return {w for w in ws if len(w) > 2 and w not in STOP}


def stem(w: str) -> str:
    return re.sub(r"(ing|ed|es|s)$", "", w) if len(w) > 4 else w


@dataclass
class Hit:
    memory: Memory
    score: float
    why: list[str] = field(default_factory=list)


@dataclass
class Result:
    hits: list[Hit]
    entities: list[dict]                 # named in the message: {"id", "name", "how"}
    when: tuple[float, float] | None
    facts: dict[str, list[str]]          # entity name → short fact lines
    ms: float = 0.0


def time_range(text: str, now: float) -> tuple[float, float] | None:
    """A time span the message refers to, in local time, or None."""
    t, d = text.lower(), datetime.fromtimestamp(now)
    day0 = d.replace(hour=0, minute=0, second=0, microsecond=0)
    span = lambda a, b=None: (a.timestamp(), (b or a + timedelta(days=1)).timestamp())
    if "day before yesterday" in t:
        return span(day0 - timedelta(days=2))
    if re.search(r"\byesterday\b", t):
        return span(day0 - timedelta(days=1))
    if re.search(r"\blast night\b", t):
        return span(day0 - timedelta(hours=6), day0 + timedelta(hours=4))
    if re.search(r"\b(today|this morning|this afternoon|this evening|tonight)\b", t):
        return span(day0)
    if (m := re.search(r"\b(\d+|two|three|four|five|six) days ago\b", t)):
        n = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}.get(m.group(1)) or int(m.group(1))
        return span(day0 - timedelta(days=n))
    if re.search(r"\blast week\b", t):
        return span(day0 - timedelta(days=7 + d.weekday()), day0 - timedelta(days=d.weekday()))
    if re.search(r"\bthis week\b", t):
        return span(day0 - timedelta(days=d.weekday()), day0 + timedelta(days=1))
    if "christmas" in t:
        year = d.year - 1 if "last christmas" in t or (d.month, d.day) < (12, 25) else d.year
        return span(datetime(year, 12, 25))
    if (m := re.search(rf"\b({'|'.join(MONTHS)}|{'|'.join(x[:3] for x in MONTHS)})\.? (\d{{1,2}})(st|nd|rd|th)?\b", t)):
        mon = next(i for i, x in enumerate(MONTHS) if x.startswith(m.group(1)[:3])) + 1
        day = datetime(d.year, mon, int(m.group(2)))
        return span(day if day <= d else day.replace(year=d.year - 1))
    if (m := re.search(rf"\b(on |last )?({'|'.join(WEEKDAYS)})\b", t)):
        back = (d.weekday() - WEEKDAYS.index(m.group(2))) % 7 or (7 if m.group(1) == "last " else 0)
        return span(day0 - timedelta(days=back))
    return None


class Retriever:
    def __init__(self, store: MemoryStore, ents: Entities, embed_model: str | None = None):
        from memory.vindex import VectorIndex
        self.store, self.ents, self.db, self.embed_model = store, ents, store.db, embed_model
        self.index = VectorIndex(store, embed_model) if embed_model else None   # exact up to 50k, FAISS beyond

    # ---- who/what is the message about ----
    def named(self, text: str, speaker: str | None) -> list[dict]:
        toks = [re.sub(r"'s$", "", w) for w in norm(text).split()]   # "the owner's" → "owner"
        found: dict[int, dict] = {}
        i = 0
        while i < len(toks):
            for n in (4, 3, 2, 1):   # longest name first: "sam lee smith" before "loved_one"
                if i + n <= len(toks) and (eid := self.ents.find(" ".join(toks[i:i + n]))):
                    found.setdefault(eid, {"id": eid, "how": "named"})
                    i += n - 1
                    break
            i += 1
        if speaker and speaker != "unknown" and re.search(r"\b(i|me|my|mine|i'm|i've)\b", text.lower()):
            if (eid := self.ents.find(speaker)):
                found.setdefault(eid, {"id": eid, "how": "speaker"})
        if re.search(r"\b(you|your|yourself|you're)\b", text.lower()):
            if (eid := self.ents.find(self.ents.seeds.get("echo", "Echo"))):
                found.setdefault(eid, {"id": eid, "how": "you"})
        for e in found.values():
            row = self.db.execute("SELECT name, mentions FROM entities WHERE id=?", (e["id"],)).fetchone()
            e["name"], e["mentions"] = row[0], row[1]
        return list(found.values())

    def entity_memories(self, eid: int, topic: str = "", limit: int = 300, newest: int = 300) -> list[int]:
        """Memories that mention this person/thing: those that are also about the topic first (keyword index), then
        the newest. Capped, so a name in hundreds of thousands of memories (the loved one, years from now) stays fast."""
        out = []
        if topic:
            out = [r[0] for r in self.db.execute(
                "SELECT f.rowid FROM memories_fts f JOIN relations r ON r.a_type='memory' AND r.a_id=f.rowid "
                "AND r.rel='mentions' AND r.b_type='entity' AND r.b_id=? JOIN memories m ON m.id=f.rowid "
                "WHERE memories_fts MATCH ? AND m.hidden IS NULL ORDER BY rank LIMIT ?", (eid, topic, limit))]
        out += [r[0] for r in self.db.execute(
            "SELECT r.a_id FROM relations r JOIN memories m ON m.id=r.a_id WHERE r.b_type='entity' AND r.b_id=? "
            "AND r.rel='mentions' AND r.a_type='memory' AND m.hidden IS NULL ORDER BY r.a_id DESC LIMIT ?", (eid, newest))]
        return list(dict.fromkeys(out))

    def facts_for(self, eid: int, limit: int = 6, question: str = "") -> list[str]:
        """Short lines like "married to the owner", no repeats; the kinds of fact the question asks about first."""
        q = question.lower()
        want = [p for p, rx in ASKS.items() if q and re.search(rx, q)]
        live = "f.retired IS NULL AND (f.memory_id IS NULL OR (SELECT hm.hidden FROM memories hm WHERE hm.id=f.memory_id) IS NULL)"
        own_sql = (f"SELECT f.predicate, COALESCE(o.name, f.value), f.confidence FROM facts f LEFT JOIN entities o "
                   f"ON o.id=f.object_id WHERE f.subject_id=? AND {live}")
        # the kinds asked about (by index), then the most recent: never every fact of a very frequent person
        own = self.db.execute(own_sql + f" AND f.predicate IN ({','.join('?' * len(want))}) ORDER BY f.id DESC LIMIT 500",
                              (eid, *want)).fetchall() if want else []
        own += self.db.execute(own_sql + " AND f.predicate NOT IN ('talked_about','said') ORDER BY f.id DESC LIMIT 3000",
                               (eid,)).fetchall()
        other = [(INVERSE[p], o, c) for p, o, c in self.db.execute(
            f"SELECT f.predicate, s.name, f.confidence FROM facts f JOIN entities s ON s.id=f.subject_id "
            f"WHERE f.object_id=? AND {live} AND f.predicate IN ({','.join('?' * len(INVERSE))}) ORDER BY f.id DESC LIMIT 1000",
            (eid, *INVERSE))]
        found: dict[tuple[str, str], list] = {}   # (predicate, object) → [predicate, object, best confidence, times]
        for p, o, c in own + other:
            v = found.setdefault((p, (o or "").lower()), [p, o, 0.0, 0])
            v[2], v[3] = max(v[2], c), v[3] + 1
        rows = [tuple(v) for v in found.values()]
        asked = lambda p: p in want
        rows = sorted(rows, key=lambda r: (not asked(r[0]), -r[2], -r[3]))[:limit]
        return [f"{FACT_WORDS.get(p, p.replace('_', ' '))} {o}" for p, o, _c, _n in rows]

    # ---- the engine ----
    def retrieve(self, text: str, qvec=None, speaker: str | None = None, now: float | None = None, k: int = 5,
                 exclude: set[int] = frozenset()) -> Result:
        t0 = time.perf_counter()
        now = now or time.time()
        q_words = words(text)
        names = self.named(text, speaker)
        name_words = {w for e in names for w in words(e["name"])} | {w for e in names for w in norm(e["name"]).split()}
        topic = {stem(w) for w in q_words - name_words}
        m_about = ABOUT.search(text.lower().strip())   # "who is Jordan Price?" — the name is the whole rest
        about = bool(m_about) and any(self.ents.find(re.sub(r"'s$", "", m_about.group(1))) == e["id"] for e in names)
        lists: dict[str, list[int]] = {}
        why: dict[int, list[str]] = {}
        sims: dict[int, float] = {}

        zs: dict[int, float] = {}
        if qvec is not None and self.embed_model:
            ids, sim, mu, sd = self.index.search(qvec, 30)
            if len(sim) > 2:
                for i, s_ in zip(ids, sim):
                    if s_ >= MEANING_WITH_NAME:
                        sims[int(i)], zs[int(i)] = float(s_), (float(s_) - mu) / sd
            lists["meaning"] = [i for i, s in sorted(sims.items(), key=lambda x: -x[1])]
        lists["words"] = [m.id for m in self.store.search(" ".join(q_words), limit=30)] if q_words else []
        ent_ids: dict[int, set[str]] = {}
        for e in names:
            # a frequent name only counts together with the topic, so its newest mentions don't help unless the
            # question is about the person ("who is …") or the name is rare
            newest = 300 if (e["mentions"] <= RARE_MENTIONS or bool(m_about)) else 0
            for mid in self.entity_memories(e["id"], " OR ".join(f'"{w}"*' for w in sorted(topic)), newest=newest):   # stems as prefixes
                ent_ids.setdefault(mid, set()).add(e["name"])
        lists["who"] = list(ent_ids)
        # facts that answer what's asked ("where did the owner grow up" → the owner from …) bring their memories first
        q = text.lower()
        asked = [p for p, rx in ASKS.items() if re.search(rx, q)]
        # one query per side and kind, so each reads its index newest-first instead of sorting all of them
        lists["fact"] = list(dict.fromkeys(
            r[0] for e in names if e["how"] == "named" for side in ("subject_id", "object_id") for p in asked
            for r in self.db.execute(
                f"SELECT f.memory_id FROM facts f JOIN memories m ON m.id=f.memory_id WHERE f.{side}=? AND f.predicate=? "
                f"AND f.retired IS NULL AND m.hidden IS NULL ORDER BY f.memory_id DESC LIMIT 100", (e["id"], p))))
        when = time_range(text, now)
        if when:
            eps = archive.episodes(self.store, when[0], when[1], limit=200)
            lists["when"] = [m.id for m in archive.notes_for_turns(self.store, min(e["first_turn"] for e in eps),
                                                                   max(e["last_turn"] for e in eps))][:200] if eps else []
        else:
            lists["when"] = []

        score: dict[int, float] = {}
        for name, ids in lists.items():
            for rank, mid in enumerate(dict.fromkeys(ids)):
                score[mid] = score.get(mid, 0.0) + 1.0 / (RRF_K + rank)
        hits = []
        mems = self.store.get_many(i for i in score if i not in exclude)   # one query, not one per candidate
        for mid, sc in score.items():
            if not (m := mems.get(mid)):
                continue
            shared = topic & {stem(w) for w in words(m.content)}
            named_here = ent_ids.get(mid, set())
            rare = any(e["mentions"] <= RARE_MENTIONS and e["how"] == "named" and e["name"] in named_here for e in names)
            reasons = []
            if sims.get(mid, 0) >= MEANING_MIN and (zs.get(mid, 0) >= STANDOUT_Z or shared):
                if not (when and not lists["when"] and not shared):   # a time with no conversations: no loose matches
                    reasons.append(f"meaning {sims[mid]:.2f}")
            if len(shared) >= 2:
                reasons.append("words " + ",".join(sorted(shared)))
            if mid in lists["when"]:
                reasons.append("time")
            if mid in lists["fact"]:
                reasons.append("fact")
            if named_here and (rare or (about and any(e["how"] == "named" for e in names))):
                reasons.append("about " + ",".join(sorted(named_here)))
            if named_here and shared:
                reasons.append("with " + ",".join(sorted(named_here)))
            if not reasons:
                continue   # nothing in the message really points here
            boost = (1.1 if m.status == "confirmed" else 1.0) * (1 + 0.5 * len(shared)) * (1 + sims.get(mid, 0))
            hits.append(Hit(m, sc * boost, reasons))
        hits.sort(key=lambda h: -h.score)
        hits = [h for h in hits if h.score >= KEEP_FRACTION * hits[0].score] if hits else hits
        if when:   # a time question: keep the conversation's notes in the order they happened
            timed = [h for h in hits if "time" in h.why]
            hits = timed + [h for h in hits if "time" not in h.why]
        facts = {}
        for e in names:   # named: its best facts; "my"/"you": only the kinds of fact the question asks about
            f = self.facts_for(e["id"], question=text)
            if e["how"] != "named":
                q = text.lower()
                f = [x for x in f if any(re.search(rx, q) and x.startswith(FACT_WORDS.get(p, p)) for p, rx in ASKS.items())]
            if f:
                facts[e["name"]] = f
        return Result(hits[:k], names, when, facts, (time.perf_counter() - t0) * 1000)


def happened(store: MemoryStore, m: Memory) -> float:
    """When it happened: a conversation note's first turn, else when the memory was saved."""
    if (sp := archive.span_of(m)) and (r := store.db.execute("SELECT ts FROM turns WHERE id=?", (sp[0],)).fetchone()):
        return r[0]
    return m.created


def format_for_message(r: Result, store: MemoryStore) -> str:
    """The notes that go with the user's message (not the stable system prompt; see core/server.py build_messages)."""
    parts = []
    for name, lines in r.facts.items():
        parts.append(f"{name}: " + "; ".join(lines))
    for h in r.hits:
        date = time.strftime("%b %-d", time.localtime(happened(store, h.memory)))
        tag = ("" if h.memory.status == "confirmed" else ", my own note" if h.memory.kind == "summary"
               else ", learned from something I was given to read" if h.memory.kind == "knowledge" else ", inferred")
        parts.append(f"{h.memory.content} ({date}{tag})")
    return ("[From my notes, in case it helps. This is all I have on it: if a detail isn't here, I don't know it and "
            "say so, I don't fill it in. " + " | ".join(parts) + "]") if parts else ""
