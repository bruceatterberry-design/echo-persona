"""Spot memories that may contradict each other and set them aside for the owner to decide. Never overwrites anything.

Steps: related() finds kept memories about the same thing (word overlap); a judge (the local language model) says
whether the two can both be true. If no judge is available or it fails, only closely related pairs are flagged.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx

from core.llm import chat_post

from memory.store import FLAG_WITHOUT_JUDGE, Memory, MemoryStore

# judge(old, new) -> (contradicts, why); None if it couldn't decide
Judge = Callable[[str, str], "tuple[bool, str] | None"]

PROMPT = """Two notes about the owner. Do they contradict each other?

A contradiction means they cannot both be true: a different name, number, date or place for the same thing,
a changed preference, or one note denying the other.
NOT a contradiction: extra detail, a narrower case, or a different time of life.

Examples:
"I love dogs." / "I love dogs, especially beagles." -> contradicts: false (more detail)
"I lived in Denver as a kid." / "I live in Austin now." -> contradicts: false (different times)
"My sister is Ann." / "Ann is a nurse." -> contradicts: false (adds a fact)
"I have one son." / "My sons are Joe and Sam." -> contradicts: true (one vs two)
"My cat is Tiger." / "My cat is Smokey." -> contradicts: true (different name)
"I hate cold weather." / "I love cold weather." -> contradicts: true

Note 1: {a}
Note 2: {b}

Answer with contradicts (true/false) and why (at most 12 words)."""

SCHEMA = {"type": "object", "properties": {"contradicts": {"type": "boolean"}, "why": {"type": "string"}},
          "required": ["contradicts", "why"]}


def ollama_judge(host: str, model: str, timeout: float = 60) -> Judge:
    def judge(a: str, b: str):
        try:
            r = chat_post(host, timeout=timeout, body={"think": False,   # background job: no thinking (slow)
        
                "model": model, "stream": False, "keep_alive": "24h", "format": SCHEMA,
                "options": {"temperature": 0, "num_predict": 200},
                "messages": [{"role": "user", "content": PROMPT.format(a=a, b=b)}]})
            r.raise_for_status()
            d = json.loads(r.json()["message"]["content"])
            return bool(d["contradicts"]), str(d.get("why", ""))[:300]
        except Exception:
            return None
    return judge


def check(store: MemoryStore, m: Memory, judge: Judge | None) -> int:
    """Flag possible contradictions between m and kept memories. Returns how many were flagged."""
    if m.kind == "summary" or m.id is None:
        return 0
    n = 0
    for old, score in store.related(m):
        verdict = judge(old.content, m.content) if judge else None
        if verdict is None:
            flag, why = score >= FLAG_WITHOUT_JUDGE, "Closely related; couldn't check automatically."
        else:
            flag, why = verdict
        if flag:
            store.flag_conflict(old.id, m.id, score, why)
            n += 1
    return n
