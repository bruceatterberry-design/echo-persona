"""Checker for a Synthetic Dreams file (docs/dream/SYNTHETIC_DREAMS_FORMAT.md). Run it before taking a file to the tower:

    python -m bridge check-synthetic ~/Downloads/synthetic_dreams.json

It never changes the file and never sends anything. It reads Echo's people/pet names (read-only) only to make sure
the synthetic file doesn't use them: synthetic dreams must never look like real memories of real people.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import export

PREFIX = "[Speculation: this is a synthetic dream, not a memory.]"
ID = r"^syn-\d{4}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SeedMemory(Strict):
    ref: str = Field(pattern=r"^synthetic:syn-\d{4}:m\d{1,2}$")
    kind: Literal["memory", "summary", "fact", "knowledge"]
    content: str = Field(min_length=10, max_length=2000)
    year: int | None = Field(default=None, ge=1900, le=2100)
    when_text: str | None = Field(default=None, max_length=200)
    people: str | None = Field(default=None, max_length=300)
    place: str | None = Field(default=None, max_length=200)
    told_by: Literal["synthetic"] = "synthetic"


class SeedEntity(Strict):
    ref: str = Field(pattern=r"^synthetic:syn-\d{4}:e\d{1,2}$")
    type: Literal["person", "place", "pet", "thing", "org", "event", "concept"]
    name: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=500)


class Evidence(Strict):
    ref: str
    quote: str = Field(min_length=3, max_length=500)


class Scores(Strict):
    identity: float = Field(ge=0, le=1)
    evidence: float = Field(ge=0, le=1)
    novelty: float = Field(ge=0, le=1)
    consistency: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    usefulness: float = Field(ge=0, le=1)


class Goal(Strict):
    horizon: Literal["short", "medium", "long"]
    priority: int = Field(ge=1, le=5)
    dependencies: list[str] = Field(default_factory=list, max_length=10)
    review_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class Proposal(Strict):
    proposal_id: str = Field(pattern=r"^syn-\d{4}-p\d{1,2}$")
    kind: Literal["association", "question", "goal", "personality", "contradiction", "outdated", "discovery"]
    proposed_change: str = Field(min_length=5, max_length=1000)
    reason: str = Field(min_length=5, max_length=1000)
    supporting_refs: list[str] = Field(min_length=1, max_length=10)
    evidence: list[Evidence] = Field(min_length=1, max_length=10)
    confidence: float = Field(ge=0, le=1)
    consequences: str = Field(min_length=3, max_length=1000)
    scores: Scores
    goal: Goal | None = None

    @model_validator(mode="after")
    def goal_only_for_goals(self):
        if (self.kind == "goal") != (self.goal is not None):
            raise ValueError("'goal' must be present for kind 'goal' and only then")
        return self


class Quality(Strict):
    label: Literal["good", "flawed"]
    flaw: Literal["invented_memory", "speculation_as_fact", "bad_quote", "irrelevant", "repetitive"] | None = None

    @model_validator(mode="after")
    def flaw_matches(self):
        if (self.label == "flawed") != (self.flaw is not None):
            raise ValueError("'flaw' is required for flawed records and must be null for good ones")
        return self


class Record(Strict):
    synthetic_id: str = Field(pattern=ID)
    synthetic: Literal[True]
    theme: str = Field(min_length=3, max_length=200)
    dream_type: Literal["association", "reflection", "curiosity", "goal", "personality", "discovery", "contradiction"]
    seed_memories: list[SeedMemory] = Field(min_length=1, max_length=10)
    seed_entities: list[SeedEntity] = Field(default_factory=list, max_length=15)
    narrative: str = Field(min_length=len(PREFIX) + 20, max_length=4000)
    proposals: list[Proposal] = Field(min_length=1, max_length=8)
    quality: Quality

    @model_validator(mode="after")
    def inside_record(self):
        if not self.narrative.startswith(PREFIX):
            raise ValueError(f"narrative must start with {PREFIX!r}")
        own = f"synthetic:{self.synthetic_id}:"
        texts = ({m.ref: " ".join(x for x in (m.content, m.when_text, m.people, m.place) if x) for m in self.seed_memories}
                 | {e.ref: f"{e.name} {e.note or ''}" for e in self.seed_entities})
        if len(texts) != len(self.seed_memories) + len(self.seed_entities):
            raise ValueError("seed refs must be unique within the record")
        for r in texts:
            if not r.startswith(own):
                raise ValueError(f"{r}: seed refs must start with {own}")
        for p in self.proposals:
            if not p.proposal_id.startswith(self.synthetic_id + "-"):
                raise ValueError(f"{p.proposal_id}: must start with {self.synthetic_id}-")
            for r in p.supporting_refs + [e.ref for e in p.evidence]:
                if r not in texts:
                    raise ValueError(f"{p.proposal_id}: {r} is not a seed in this record (real refs like memory:12 are never allowed)")
            if self.quality.flaw != "bad_quote":
                for e in p.evidence:
                    if e.quote not in texts[e.ref]:
                        raise ValueError(f"{p.proposal_id}: quote not found word for word in {e.ref}")
        return self


class Collection(Strict):
    format: Literal["echo-synthetic-dreams"]
    version: Literal[1]
    generator: str = Field(min_length=1, max_length=200)
    created: str
    count: int = Field(ge=1, le=5000)
    records: list[Record] = Field(min_length=1, max_length=5000)


def real_names() -> set[str]:
    """Names of real people and pets in Echo's memory (read-only), which synthetic records must not use."""
    try:
        with export._ro(export._db("memory.sqlite")) as con:
            rows = con.execute("SELECT name FROM entities WHERE type IN ('person', 'pet')").fetchall()
    except Exception:
        return set()
    names = {r[0].strip().lower() for r in rows if r[0] and len(r[0].strip()) >= 3}
    return {n for n in names if n not in PUBLIC and not set(n.split()) <= GENERIC}


# descriptions rather than names, and public figures: fine in synthetic dreams
GENERIC = {"a", "an", "the", "and", "young", "old", "woman", "man", "girl", "boy", "mom", "owner", "mother", "father",
           "grandmother", "grandfather", "cat", "dog", "kitten", "tabby", "calico", "gray", "grey", "black", "tan",
           "white", "orange", "brown", "friend", "neighbor", "firefighter"}
PUBLIC = {"e e cummings", "e. e. cummings", "steely dan", "dan fogelberg"}


def check(path: str | Path) -> dict:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"ok": False, "errors": [f"not valid JSON: {e}"]}
    errors, warnings = [], []
    try:
        col = Collection.model_validate(data)
    except ValidationError as e:
        for err in e.errors()[:40]:
            loc = ".".join(str(x) for x in err["loc"])
            errors.append(f"{loc}: {err['msg']}")
        return {"ok": False, "errors": errors, "more": max(0, e.error_count() - 40)}
    if col.count != len(col.records):
        errors.append(f"count says {col.count} but there are {len(col.records)} records")
    ids = [r.synthetic_id for r in col.records]
    if len(set(ids)) != len(ids):
        errors.append("synthetic_id values repeat")
    names = real_names()
    pat = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")\b", re.I) if names else None
    for r in col.records:
        blob = json.dumps(r.model_dump(), ensure_ascii=False)
        if export.PRIVATE_WORDS.search(blob):
            errors.append(f"{r.synthetic_id}: mentions students/IEP/classroom (never allowed)")
        if pat and (m := pat.search(blob)):
            errors.append(f"{r.synthetic_id}: uses the real name {m.group(0)!r}; use made-up names")
    # agreed with the tower: ratio rules are errors from 10 records up, warnings below 10
    ratio = errors if len(col.records) >= 10 else warnings
    flawed = sum(r.quality.label == "flawed" for r in col.records)
    if flawed > len(col.records) * 0.3:
        ratio.append(f"{flawed} flawed records (more than 30%)")
    themes = {r.theme.lower() for r in col.records}
    if len(themes) < len(col.records) * 0.5:
        ratio.append(f"only {len(themes)} different themes for {len(col.records)} records (repetitive)")
    types = {}
    for r in col.records:
        types[r.dream_type] = types.get(r.dream_type, 0) + 1
    return {"ok": not errors, "records": len(col.records), "flawed": flawed, "dream_types": types,
            "errors": errors[:60], "warnings": warnings}


def main(path: str) -> int:
    r = check(path)
    print(json.dumps(r, indent=1, ensure_ascii=False))
    print("\nOK: ready to take to the tower." if r["ok"] else "\nNOT OK: fix the errors above (or ask ChatGPT to) and check again.")
    return 0 if r["ok"] else 1
