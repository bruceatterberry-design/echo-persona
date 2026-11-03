"""Executive controller, Stage 2a: SHADOW MODE (approved by the owner 2026-11-01).

classify(text) decides, with plain rules and no LLM call (< 1 ms), what kind of task a message is and which
cognitive functions it would need. In this stage the decision is only WRITTEN into the turn record
(core/workspace.py) beside what Echo actually did; nothing acts on it yet. After real use, the owner and I compare the
two before the controller is allowed to steer anything (Stage 2b).

Kinds:  command   — open / play / music list: handled by core/browser.py or core/music.py, no LLM needed
        research  — needs the web (an address, "look up", news, weather…): language + memory + tools
        reasoning — maths, logic, plans, comparisons, "why": language + memory + reasoning + metacognition
        memory    — about people, places or events Echo should remember: language + memory (+ metacognition)
        chat      — everything else: language + light memory

`why` holds rule names only, never words from the message (the record keeps no text).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core import browser, people, tools

THINK_BUDGET_S = 10.0   # the owner, 2026-11-01: the most a hard question may "think" before answering (used from Stage 4)

NEEDS = {
    "command": ["executive"],
    "chat": ["executive", "language", "memory_light"],
    "memory": ["executive", "language", "memory", "metacognition"],
    "reasoning": ["executive", "language", "memory_light", "reasoning", "metacognition"],
    "research": ["executive", "language", "memory", "perception", "learning", "metacognition"],
}

_MATH = re.compile(r"\d\s*[-+*/x×÷^%]\s*\d|\b\d+(?:\.\d+)?\s*(?:percent|%)|\bpercent of\b|\bsquare root\b|"
                   r"\b(?:plus|minus|times|divided by|multiplied by)\b", re.I)
_QUANT = re.compile(r"\bhow (?:many|much|long|far|old|often)\b|\bwhat time\b|\bwhat day will\b|\bnext number\b|"
                    r"\bsale price\b|\bon average\b", re.I)
_LOGIC = re.compile(r"\bif\b.+\bthen\b|\bif\b.{3,80}\b(?:what|how|who|which|when)\b|\bwhich is (?:heavier|bigger|better|"
                    r"faster|cheaper|older|more)\b|\bwho is (?:the )?(?:youngest|oldest|tallest|shortest)\b|"
                    r"\b(?:older|younger|taller|faster|heavier) than\b|\ball (?:the |my )?\w+(?: \w+){0,4} (?:are|is|have)\b.{0,80}\?", re.I)
_THINK = re.compile(r"^\s*(?:why|explain|compare|figure out|work out|help me (?:decide|plan|figure|think|work out)|"
                    r"what (?:are|would be) the (?:pros|cons|steps|options|differences?)|should i|plan (?:a|my|our|the)|"
                    r"what(?:'s| is) the (?:best|difference))\b|\bpros and cons\b|\bstep by step\b|\btrade-?offs?\b", re.I)
_MEMORY = re.compile(r"\b(?:do you )?remember\b|\bwhat happened\b|\bwho (?:is|was|are|were)\b|\btell me about\b|\bwhat do you know about\b|"
                     r"\b(?:when|where) did (?:i|we|you|he|she|they)\b|\blast (?:time|week|weekend|year|month|"
                     r"summer|christmas)\b|\bour (?:trip|wedding|house|cats?|dog|family|kids?|son|daughter)\b|"
                     r"\bmy (?:mom|mother|dad|father|brother|sister|wife|husband|son|daughter|"
                     r"grand\w+|first|favou?rite)\b|\bgrow up\b|\bwhen (?:i|we|you) (?:was|were)\b", re.I)
_WEB = re.compile(r"\b(?:weather|forecast|news|headlines?|stock price|scores?\b.{0,20}\bgame|latest on|going to (?:rain|snow|storm))\b", re.I)
_PAST = re.compile(r"\b(?:did|have|had) (?:we|i)\b|\bwhat did (?:we|i|you)\b|\bdid you (?:tell|show|say|make|open|suggest|"
                   r"mind|promise|ask|play|try|remember|learn)\b|\bhave you (?:tried|told|shown|made|opened|learned)\b|"
                   r"\btalk(?:ed)? about\b|\bdiscuss(?:ed)?\b|\byesterday\b|\bday before\b|"
                   r"\bon (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
                   r"\bon (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d", re.I)
_PERSONAL = re.compile(r"\bwhat(?:'s| is| are) my\b|\b(?:do|did|am) i\b(?! think)|\bdo we have\b|\bhow do i like\b|"
                       r"\bwho (?:made|built|designed|created) you\b|\byour old name\b|\bthe cats?'?s?'? names?\b", re.I)
_READ = re.compile(r"\b(?:read|follow|tell me|what does|what do|summari[sz]e|find|look (?:for|at|up)|according to)\b", re.I)
_REMEMBER = re.compile(r"\b(?:do|did|can) you (?:still )?remember\b|\bremember when\b", re.I)
_ASK = re.compile(r"^\s*(?:(?:hey|hi|okay|ok|so|and|well)[, ]+)?(?:echo[, ]+)?(?:who|what|when|where|why|how|which|whose|do|"
                  r"does|did|is|are|was|were|can|could|will|would|have|has|had|tell me|describe|remind me)\b", re.I)


def _is_question(t: str) -> bool:
    return "?" in t or bool(_ASK.search(t))


_SELF = re.compile(r"^\s*(?:who are you|what(?:'s| is) your name|are you (?:a )?real|what are you)\b", re.I)


@dataclass
class Decision:
    kind: str
    needs: list[str]
    why: list[str] = field(default_factory=list)     # rule names, no message words
    budget_s: float | None = None                    # thinking-time budget for hard questions

    def as_record(self) -> dict:
        return {"kind": self.kind, "needs": self.needs, "why": self.why,
                **({"budget_s": self.budget_s} if self.budget_s else {})}


def classify(text: str, known_names: set[str] | frozenset[str] = frozenset()) -> Decision:
    """Deterministic: same text (and names) → same decision. `known_names` = lower-case names of people/places Echo
    has in memory (memory/entities.py), so "How is Tom doing?" counts as a memory question."""
    t = (text or "").strip()
    why: list[str] = []
    low = t.lower()

    def d(kind: str) -> Decision:
        return Decision(kind, list(NEEDS[kind]), why, THINK_BUDGET_S if kind == "reasoning" else None)

    if tools.urls_in(t) and (tools.wants_research(t) or _READ.search(t)):   # "start at <address> and follow the links"
        why.append("address_read")
        return d("research")
    try:
        if browser.wants_action(t):
            why.append("action_request")
            return d("command")
    except Exception:  # noqa: BLE001  (music list unreadable: just skip this signal)
        pass
    if tools.urls_in(t):
        why.append("address")
        return d("research")
    if tools.wants_tools(t) or tools.wants_research(t) or _WEB.search(t):
        why.append("web_words")
        return d("research")
    for name, rx in (("math", _MATH), ("quantity", _QUANT), ("logic", _LOGIC), ("deliberate", _THINK)):
        if rx.search(t):
            why.append(name)
    if why:
        return d("reasoning")
    if _SELF.search(t):
        why.append("about_me")
        return d("chat")
    asking = _is_question(t)   # names / past / personal words count only in a question ("the loved one said hi" is chat)
    if _REMEMBER.search(t) or (asking and _MEMORY.search(t)):
        why.append("memory_words")
    if asking and _PAST.search(t):
        why.append("past_talk")
    if asking and _PERSONAL.search(t):
        why.append("personal")
    if asking and re.search(r"\b(?:" + people.alternation() + r")(?:'s)?\b", low):   # names from this install's settings
        why.append("household_name")
    words = set(re.findall(r"[a-z][a-z'’-]+", low))
    if asking and known_names and any((n in words) if " " not in n else (n in low) for n in known_names):
        why.append("known_name")
    if why:
        return d("memory")
    if len(t.split()) > 40 and "?" in t:
        why.append("long_question")
        return d("reasoning")
    return d("chat")
