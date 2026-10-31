"""Level 2, symbols (Phase 2 of docs/MEMORY_ARCHITECTURE.md): people, places and things, and facts about them.

Each memory is read once by the local language model, which lists the people/places/things in it and short facts
(subject, predicate, object) with the exact words that support each one. A fact is kept only if
  * its quote really is in the memory (otherwise the model invented it),
  * its predicate is on the list below (anything else stays a plain memory: nothing is forced into symbols),
  * its subject is known or named in the memory (no guessing who "you" was).
Plus checks for the model's usual mistakes (measured on the live memories, 2026-10-31):
  * the subject must be named in the memory, or be who "I" means (no guessing who "you" was),
  * the object, if it's a known person/thing, must be named in the memory; a fact can't point at its own subject,
  * "can"/"cannot" not from plans, wishes or questions ("will let me", "if I could", "wanted to know"),
  * "has_name" only for a name that's in the quote,
  * the quote must back the fact: a named object must be in the quote, and plain-word objects must share a word with
    it; "can" looks at the words just before the quote too ("will allow me to | recognize voices");
  * likes/dislikes/favorite need a feeling word in the quote; happened_on/happened_at only for events and places.
Extracted facts are always "inferred": they're Echo's reading of a memory, not something a person confirmed.
Their confidence follows the memory's (a confirmed memory → 0.8, a conversation note → 0.5).
When a memory is edited it is read again; the old facts are retired (kept, marked), never deleted.
Hidden (forgotten) memories' facts are left out everywhere, like the memories themselves.

Who "I" is depends on the memory: in the owner's facts it's the owner; in Echo's conversation notes it's Echo.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

from core.llm import chat_post

from memory.store import Memory, MemoryStore

ROOT = Path(__file__).resolve().parents[1]
SEEDS = ROOT / "config" / "entities.json"
TYPES = ("person", "place", "pet", "thing", "org", "event", "concept", "self")
PREDICATES = {
    "is_a": "what kind of person/thing it is (\"teacher\", \"digital self\")",
    "married_to": "spouse", "parent_of": "parent of", "child_of": "child of", "sibling_of": "brother/sister of",
    "friend_of": "friend of", "relative_of": "other family", "pet_of": "an animal belonging to someone",
    "created": "made or built", "created_by": "was made by",
    "lives_in": "home town or address", "from": "grew up in / comes from", "works_as": "job", "works_at": "employer",
    "first_job": "first job (object = the job)", "teaches": "who or what they teach",
    "likes": "likes/loves", "dislikes": "dislikes", "favorite": "favourite thing (object = the thing)",
    "interested_in": "interest or hobby (also what they work on)", "values": "something they value", "believes": "a belief or principle",
    "wants": "a wish or request", "plans": "a plan", "owns": "owns", "has_name": "a name, nickname or middle name",
    "visited": "went to a place", "took_part_in": "was part of an event", "happened_at": "an event's place",
    "happened_on": "an event's date", "talked_about": "a conversation's topic", "can": "an ability",
    "cannot": "something they can't do now", "said": "something they said",
}
SPEC = "\n".join(f"- {k}: {v}" for k, v in PREDICATES.items())

PROMPT = """Read one memory and list who and what it's about, and simple facts it states.

In this memory, "I", "me" and "my" mean {i_am}. {you_rule}
Known names: {known}.

Memory: "{text}"

Answer in JSON:
{{"entities": [{{"name": "...", "type": "person|place|pet|thing|org|event|concept"}}],
  "facts": [{{"subject": "...", "predicate": "...", "object": "...", "quote": "..."}}]}}

Rules:
- Use full names for subject and object (never "I", "me", "you", "he", "she"); replace "I" with {i_am}.
- predicate must be one of:
{spec}
- quote: the exact words from the memory that say this fact (copy them, a short part of the memory).
- Only facts the memory clearly states. No guesses. If it's unclear who someone is, leave that fact out.
- Things about the conversation itself (what was asked, tested, explained) are talked_about at most.
- works_as is only for a job someone has; a first job is first_job; what someone works on is interested_in.
- can/cannot only for what someone is able to do now, stated as fact. Not plans, wishes, tries or questions.
- has_name only when the memory gives a name (a nickname, middle name, former name).
- "wants" needs the whole wish as the object ("Echo to learn and grow"), not just a name.
- Few good facts are better than many. Empty lists are fine."""

YOU_RULE = {"fact": "\"You\" and \"your\" mean Echo, the owner's digital self.",
            "summary": "\"You\" means whoever was talking with Echo; if the memory doesn't say who, skip facts about \"you\"."}


HEDGE = re.compile(r"\b(will|would|could|might|if|whether|wanted to know|asked|try|trying|tried|going to|plan|hope)\b")
FEELING = re.compile(r"\b(like[sd]?|lov(e|es|ed|ing)|enjoy\w*|prefer\w*|favou?rite|great|appreciat\w*|fond|hate[sd]?|"
                     r"dislike[sd]?|can't stand|didn't like|doesn't like)\b")
STOPWORDS = set("a an the of to in on at for with and or his her their my your its about from".split())
FUTURE = re.compile(r"\b(will|would|could|going to|plan\w*|hop\w+ to)\s+(\w+\s+)?(allow|let|help|enable|give|be able)\b")
PRONOUNS = {"i", "me", "my", "you", "your", "he", "she", "him", "her", "they", "we", "us", "it"}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", s.lower().replace("’", "'"))).strip()


def load_seeds(path: Path = SEEDS) -> dict:
    return json.loads(path.read_text()) if path.exists() else {"self": "the owner", "echo": "Echo", "entities": []}


class Entities:
    def __init__(self, store: MemoryStore, seeds: dict | None = None):
        self.store, self.db = store, store.db
        self.seeds = seeds if seeds is not None else load_seeds()
        for e in self.seeds.get("entities", []):
            eid = self.find(e["name"]) or self.add_entity(e["name"], e.get("type", "person"), e.get("note"), by="seed")
            for a in e.get("aliases", []):
                self.add_alias(eid, a)

    # ---- entities and names ----
    def find(self, name: str) -> int | None:
        r = self.db.execute("SELECT entity_id FROM aliases WHERE alias=? COLLATE NOCASE", (norm(name),)).fetchone()
        return r[0] if r else None

    def add_entity(self, name: str, type_: str, note: str | None = None, by: str = "auto:extract") -> int:
        now = time.time()
        with self.db:
            cur = self.db.execute("INSERT INTO entities(type,name,note,first_seen,last_seen,created,by) VALUES (?,?,?,?,?,?,?)",
                                  (type_ if type_ in TYPES else "thing", name.strip()[:100], note, now, now, now, by))
        self.add_alias(cur.lastrowid, name)
        return cur.lastrowid

    def add_alias(self, eid: int, alias: str) -> None:
        if (a := norm(alias)):
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO aliases(entity_id, alias) VALUES (?,?)", (eid, a))

    def resolve(self, name: str, type_: str = "thing") -> int | None:
        """A known entity for this name, or a new one. Pronouns and empty names → None."""
        n = norm(name)
        if not n or n in PRONOUNS:
            return None
        if (eid := self.find(n)):
            return eid
        n = re.sub(r"^(the|a|an|my|our|his|her|their) ", "", n)    # "my cat Pepper" style leftovers
        if (eid := self.find(n)):
            return eid
        return self.add_entity(name.strip(), type_)

    def entity(self, eid: int) -> dict | None:
        r = self.db.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
        if not r:
            return None
        return dict(r) | {"aliases": [a[0] for a in self.db.execute("SELECT alias FROM aliases WHERE entity_id=? ORDER BY alias", (eid,))]}

    # ---- extraction ----
    def pending(self, limit: int = 8) -> list[Memory]:
        """Memories in use that were never read, or edited since."""
        rows = self.db.execute(
            "SELECT m.* FROM memories m LEFT JOIN extracted x ON x.memory_id=m.id "
            "WHERE m.hidden IS NULL AND m.kind != 'knowledge' "   # learned material never becomes facts about our lives
            "AND (x.memory_id IS NULL OR x.updated < m.updated) ORDER BY m.id LIMIT ?", (limit,))
        return [Memory(**dict(r)) for r in rows]

    def prompt_for(self, m: Memory) -> str:
        known = sorted({e["name"] for e in self.seeds.get("entities", [])})
        i_am = self.i_am(m)
        return PROMPT.format(i_am=i_am, you_rule=YOU_RULE["summary" if m.kind == "summary" else "fact"],
                             known=", ".join(known) or "none yet", text=m.content.replace('"', "'"), spec=SPEC)

    def named_in(self, eid: int, text: str) -> bool:
        """Is this entity (any of its names) in the text?"""
        return any(re.search(rf"\b{re.escape(a)}\b", text) for a, in
                   self.db.execute("SELECT alias FROM aliases WHERE entity_id=?", (eid,)))

    def i_am(self, m: Memory) -> str:
        return self.seeds.get("echo", "Echo") if m.kind == "summary" else (
            m.told_by.title() if m.told_by else self.seeds.get("self", "the owner"))

    def apply(self, m: Memory, answer: dict, model: str) -> dict:
        """Check the model's answer against the memory and keep what holds up. Returns counts."""
        text = norm(m.content)
        me = self.find(self.i_am(m))
        you = self.find(self.seeds.get("echo", "Echo")) if m.kind != "summary" else None   # "you" in the owner's facts
        kept, dropped = 0, {"quote": 0, "predicate": 0, "subject": 0, "object": 0, "hedge": 0}
        conf = 0.8 if m.status == "confirmed" else 0.5
        now = time.time()
        types = {norm(e.get("name", "")): e.get("type", "thing") for e in answer.get("entities", []) if isinstance(e, dict)}
        mentioned: set[int] = set()
        facts = []
        for f in answer.get("facts", []):
            if not isinstance(f, dict):
                continue
            pred = str(f.get("predicate", "")).strip().lower()
            quote = norm(str(f.get("quote", "")))
            if pred not in PREDICATES:
                dropped["predicate"] += 1
                continue
            if len(quote) < 3 or quote not in text:
                dropped["quote"] += 1
                continue
            sname = str(f.get("subject", ""))
            if norm(sname) in PRONOUNS:
                dropped["subject"] += 1
                continue
            known_s = self.find(sname)
            if not (known_s in (me, you) and known_s) and norm(sname) not in text and not (known_s and self.named_in(known_s, text)):
                dropped["subject"] += 1   # someone the memory doesn't name: a guess
                continue
            subj = known_s or self.resolve(sname, types.get(norm(sname), "thing"))
            if not subj:
                dropped["subject"] += 1
                continue
            before = " ".join(text[:text.find(quote)].split()[-4:])   # "will allow me to | recognize voices"
            sentence = next((norm(x) for x in re.split(r"[.!?;]", m.content) if quote in norm(x)), quote)
            if pred in ("can", "cannot") and (HEDGE.search(before + " " + quote) or FUTURE.search(sentence)):
                dropped["hedge"] += 1
                continue
            if pred in ("likes", "dislikes", "favorite") and not FEELING.search(quote):
                dropped["object"] += 1
                continue
            obj_name = str(f.get("object", "")).strip()
            obj = self.find(obj_name) if obj_name else None
            if not obj and obj_name and norm(obj_name) in types:   # the model listed it as a person/place/thing
                obj = self.resolve(obj_name, types[norm(obj_name)])
            if pred == "pet_of" and obj:   # "the owner pet_of Biscuit": the animal must be the subject → turn it round
                types_ = dict(self.db.execute("SELECT id, type FROM entities WHERE id IN (?,?)", (subj, obj)).fetchall())
                if types_.get(subj) in ("person", "self") and types_.get(obj) not in ("person", "self"):
                    subj, obj = obj, subj
            words = set(norm(obj_name).split()) - STOPWORDS
            subj_type = self.db.execute("SELECT type FROM entities WHERE id=?", (subj,)).fetchone()[0]
            if obj == subj or (obj and obj not in (me, you) and not self.named_in(obj, quote)) or \
                    (not obj and obj_name and not words & set(quote.split())) or \
                    (pred == "has_name" and norm(obj_name) not in quote) or \
                    (pred in ("happened_on", "happened_at") and subj_type not in ("event", "thing")) or \
                    (not obj and set(norm(sname).split()) & words and pred in ("wants", "likes")):
                dropped["object"] += 1
                continue
            facts.append((subj, pred, obj, None if obj else obj_name[:200], quote))
            mentioned |= {subj} | ({obj} if obj else set())
        for name, t in types.items():   # entities named in the memory, even without a fact
            if name and name in text and (eid := self.resolve(name, t)):
                mentioned.add(eid)
        with self.db:
            self.db.execute("UPDATE facts SET retired=? WHERE memory_id=? AND retired IS NULL", (now, m.id))
            self.db.execute("DELETE FROM relations WHERE a_type='memory' AND a_id=? AND rel='mentions'", (m.id,))
            for subj, pred, obj, val, quote in dict.fromkeys(facts):
                self.db.execute("INSERT INTO facts(subject_id,predicate,object_id,value,status,confidence,source,span,"
                                "memory_id,created) VALUES (?,?,?,?, 'inferred', ?, ?, ?, ?, ?)",
                                (subj, pred, obj, val, conf, f"memory:{m.id}", quote[:300], m.id, now))
                kept += 1
            for eid in mentioned:
                self.db.execute("INSERT OR IGNORE INTO relations(a_type,a_id,rel,b_type,b_id,weight,status,source,created) "
                                "VALUES ('memory',?,'mentions','entity',?,1.0,'inferred',?,?)", (m.id, eid, model, now))
                self.db.execute("UPDATE entities SET mentions=mentions+1, last_seen=? WHERE id=?", (now, eid))
            self.db.execute("INSERT INTO extracted(memory_id,updated,at,model,facts) VALUES (?,?,?,?,?) ON CONFLICT(memory_id) "
                            "DO UPDATE SET updated=excluded.updated, at=excluded.at, model=excluded.model, facts=excluded.facts",
                            (m.id, m.updated, now, model, kept))
        return {"kept": kept, "dropped": dropped, "entities": len(mentioned)}

    def extract_pending(self, host: str, model: str, limit: int = 4, timeout: float = 120) -> int:
        """Read up to `limit` memories with the local model. Returns how many were read."""
        n = 0
        for m in self.pending(limit):
            r = chat_post(host, timeout=timeout, body={"think": False,   # background job: no thinking (slow)
        
                "model": model, "stream": False, "format": "json", "keep_alive": "24h",
                "options": {"temperature": 0, "num_predict": 600},
                "messages": [{"role": "user", "content": self.prompt_for(m)}]})
            r.raise_for_status()
            try:
                answer = json.loads(r.json()["message"]["content"])
            except (ValueError, KeyError):
                answer = {}
            self.apply(m, answer if isinstance(answer, dict) else {}, model)
            n += 1
        return n

    # ---- "everything about X" ----
    def about(self, name_or_id: str | int) -> dict | None:
        """Everything Echo has about one person/place/thing: names, facts (both directions), memories that mention it,
        the conversations those came from, conversations where they spoke, and who/what is connected."""
        from memory import archive
        eid = name_or_id if isinstance(name_or_id, int) else self.find(str(name_or_id))
        if not eid or not (e := self.entity(eid)):
            return None
        live = "f.retired IS NULL AND (f.memory_id IS NULL OR (SELECT hm.hidden FROM memories hm WHERE hm.id=f.memory_id) IS NULL)"
        sql = (f"SELECT f.*, s.name AS subject, o.name AS object FROM facts f JOIN entities s ON s.id=f.subject_id "
               f"LEFT JOIN entities o ON o.id=f.object_id WHERE {live} AND f.{{side}}=? ORDER BY f.id DESC LIMIT 2000")
        rows = [*self.db.execute(sql.format(side="subject_id"), (eid,)), *self.db.execute(sql.format(side="object_id"), (eid,))]
        rows.sort(key=lambda r: (-r["confidence"], r["id"]))   # newest 2,000 each way: enough for a page, fast for the loved one
        facts: dict[tuple, dict] = {}   # the same fact from several memories is listed once, with how many say it
        for r in rows:
            obj = r["object"] or r["value"]
            f = facts.setdefault((r["subject"], r["predicate"], norm(obj or "")), {
                "id": r["id"], "subject": r["subject"], "predicate": r["predicate"], "object": obj, "status": r["status"],
                "confidence": r["confidence"], "quote": r["span"], "memory_id": r["memory_id"], "memories": []})
            f["memories"].append(r["memory_id"])
            f["confidence"] = max(f["confidence"], r["confidence"])
        facts = sorted(facts.values(), key=lambda f: -len(f["memories"]))[:200]
        total = self.db.execute("SELECT COUNT(*) FROM relations WHERE b_type='entity' AND b_id=? AND rel='mentions' "
                                "AND a_type='memory'", (eid,)).fetchone()[0]
        mids = [r[0] for r in self.db.execute(
            "SELECT a_id FROM relations WHERE b_type='entity' AND b_id=? AND rel='mentions' AND a_type='memory' "
            "ORDER BY a_id DESC LIMIT 100", (eid,))][::-1]   # the newest 100, oldest first
        mems = [m for i in mids if (m := self.store.get(i))]
        eps: dict[int, dict] = {}
        for m in mems:
            if (sp := archive.span_of(m)) and (ep := archive.episode_for_turn(self.store, sp[0])):
                eps[ep["id"]] = ep
        names = {norm(a) for a in e["aliases"]}
        for ep in self.db.execute("SELECT * FROM episodes WHERE speakers IS NOT NULL ORDER BY start DESC LIMIT 2000"):
            if names & {norm(x) for x in ep["speakers"].split(",")}:
                eps[ep["id"]] = dict(ep)
        linked = {}
        for r in rows:
            other = r["object_id"] if r["subject_id"] == eid else r["subject_id"]
            if other and other != eid:
                linked.setdefault(other, set()).add(r["predicate"])
        return {"entity": e, "facts": facts, "memories": [m.model_dump() for m in mems], "memories_total": total,
                "episodes": sorted(eps.values(), key=lambda x: x["start"])[-50:],
                "connected": [{"id": i, "name": self.entity(i)["name"], "via": sorted(p)} for i, p in linked.items()]}

    def list(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM entities ORDER BY mentions DESC, name")]
