"""Build a draft personality profile from the owner's own words. Local only (Ollama on this Mac).

Sources (all authorized recordings, transcribed locally): Voice Studio "about me" + free-talk takes, phone
own-words answers, phone talking videos. Every trait must quote the owner's exact words as evidence; traits whose
quote can't be found in the source are dropped, so the profile can't invent a personality. Nothing here is used by
Echo until the owner approves it in the "How I talk" page.

Outputs (data/personality/, gitignored):
  draft.json    traits + evidence, style statistics, suggested facts (all "pending")
  profile.json  what the owner approved (written by the review page)
"""
from __future__ import annotations

import collections
import json
import re
import time
from pathlib import Path

import httpx

from core.llm import chat_post

CATEGORIES = ["communication", "explaining", "values", "interests", "humor", "empathy", "disagreement", "storytelling"]
TRAIT_SCHEMA = {
    "type": "object",
    "properties": {
        "traits": {"type": "array", "items": {"type": "object", "properties": {
            "category": {"type": "string", "enum": CATEGORIES},
            "trait": {"type": "string"},
            "evidence": {"type": "string"},
            "confidence": {"type": "number"}},
            "required": ["category", "trait", "evidence", "confidence"]}},
        "facts": {"type": "array", "items": {"type": "object", "properties": {
            "fact": {"type": "string"}, "evidence": {"type": "string"}}, "required": ["fact", "evidence"]}},
    },
    "required": ["traits", "facts"],
}
PROMPT = """Below is a transcript of the owner speaking in their own words{question}.

Describe HOW the owner talks and what they care about, as short traits written in the second person for an assistant that
will speak like them (e.g. "Explain with a practical, everyday example first"). Categories: {cats}.
For EVERY trait, copy an exact short quote (5-25 words) from the transcript as evidence. Do not paraphrase the quote.
Only include traits clearly supported by the transcript; it's fine to return few or none. Confidence 0-1.
Also list any facts the owner states about their own life (people, places, work, likes), each with an exact quote.
Do not guess or add anything that isn't said.

Transcript:
\"\"\"{text}\"\"\""""

FILLERS = ["you know", "i mean", "kind of", "sort of", "like", "well", "actually", "basically", "honestly", "pretty much", "um", "uh"]


def strip_question(text: str, question: str | None) -> str:
    """The owner often reads the question aloud first; those words are the prompt, not his. Drop matching sentences."""
    if not question:
        return text
    qw = set(norm(question).split())
    keep = [s for s in re.split(r"(?<=[.!?])\s+", text)
            if not (sw := set(norm(s).split())) or len(sw & qw) / max(1, len(sw)) < 0.7]
    return " ".join(keep).strip()


def collect(dataset: Path) -> list[dict]:
    from voice.studio import load_prompts
    prompts = load_prompts()
    out = []
    for p in sorted(dataset.glob("derived/voice/qc-v1/*/take-*.json")):
        q = json.loads(p.read_text())
        pid = q.get("prompt_id", "")
        if q.get("status") == "kept" and q.get("transcript") and (pid.startswith("about_me") or pid == "custom"):
            question = prompts.get(pid, {}).get("text")
            text = strip_question(q["transcript"], question)
            if len(text.split()) >= 8:
                out.append({"source": str(p.relative_to(dataset)), "kind": "own words", "prompt_id": pid,
                            "question": question, "text": text})
    ex = dataset / "derived/personality/examples-v1.jsonl"
    if ex.exists():
        for line in ex.read_text().splitlines():
            e = json.loads(line)
            text = strip_question(e["answer"], e.get("question"))
            if len(text.split()) >= 8:
                out.append({"source": e["source"], "kind": "phone answer", "question": e.get("question"), "text": text})
    for p in sorted(dataset.glob("derived/face/phone-frames-v1/*/*/frames.json")):
        f = json.loads(p.read_text())
        if f.get("task_id", "").startswith("face-talk") and len((f.get("transcript") or "").split()) >= 15:
            out.append({"source": f["source"], "kind": "talking video", "text": f["transcript"]})
    return out


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", t.lower().replace("’", "'"))).strip()


def style_stats(examples: list[dict]) -> dict:
    text = " ".join(e["text"] for e in examples)
    sents = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    words = norm(text).split()
    bigrams = collections.Counter(zip(words, words[1:], words[2:]))
    stop = {"the", "a", "and", "to", "of", "i", "it", "that", "is", "in", "you", "was", "for", "my", "me", "on", "be"}
    phrases = [" ".join(g) for g, n in bigrams.most_common(60) if n >= 2 and not set(g) <= stop][:12]
    n = max(len(words), 1)
    return {
        "words": len(words), "examples": len(examples),
        "avg_sentence_words": round(len(words) / max(len(sents), 1), 1),
        "contractions_per_100_words": round(100 * sum("'" in w for w in words) / n, 1),
        "first_person_per_100_words": round(100 * sum(w in ("i", "i'm", "i've", "me", "my") for w in words) / n, 1),
        "vocabulary_variety": round(len(set(words)) / n, 2),
        "fillers": {f: c for f in FILLERS if (c := len(re.findall(rf"\b{f}\b", norm(text))))},
        "repeated_phrases": phrases,
    }


def ask_llm(host: str, model: str, ex: dict, helper=None) -> dict:
    q = f' (answering: "{ex["question"]}")' if ex.get("question") else ""
    prompt = PROMPT.format(question=q, cats=", ".join(CATEGORIES), text=ex["text"][:6000])
    if helper and isinstance(got := helper.ask("personality", prompt, TRAIT_SCHEMA), dict):
        return got   # the owner's Claude account, if they switched it on; the same grounding checks apply below
    r = chat_post(host, timeout=180, body={"think": False,   # background job: no thinking (slow)
        "model": model, "stream": False, "format": TRAIT_SCHEMA, "options": {"temperature": 0.2}, "keep_alive": "24h",
        "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"])


def grounded(quote: str, text: str) -> bool:
    """The evidence must really be the owner's words: present in the transcript (ignoring punctuation/case)."""
    q = norm(quote)
    return len(q.split()) >= 3 and q in norm(text)


def similar(a: str, b: str) -> bool:
    wa, wb = set(norm(a).split()), set(norm(b).split())
    return len(wa & wb) / max(1, min(len(wa), len(wb))) > 0.6


def build(dataset: Path, out_dir: Path, host: str = "http://127.0.0.1:11434", model: str = "llama3.1:8b",
          progress=lambda m: None, incremental: bool = True, helper=None) -> dict:
    """Analyze recordings into traits/facts. With an existing draft and incremental=True, only recordings not seen before
    are analyzed, and everything the owner decided (approved/rejected, edited wording, saved facts, summary) is kept."""
    old = json.loads((out_dir / "draft.json").read_text()) if (out_dir / "draft.json").exists() else None
    all_examples = collect(dataset)
    seen = set(old.get("analyzed_sources", [q["source"] for q in old.get("quotes", [])])) if old and incremental else set()
    examples = [e for e in all_examples if e["source"] not in seen]
    traits, facts, dropped = [], [], 0
    for i, ex in enumerate(examples, 1):
        progress(f"{i}/{len(examples)}")
        try:
            got = ask_llm(host, model, ex, helper)
        except (httpx.HTTPError, json.JSONDecodeError, KeyError):
            continue
        for t in got.get("traits", []):
            quote = t.get("evidence", "")
            if (not grounded(quote, ex["text"]) or not t.get("trait", "").strip()
                    or (ex.get("question") and similar(quote, ex["question"]))):
                dropped += 1
                continue
            # confidence comes from how many separate quotes support the trait (the model's own number is unreliable)
            item = {"category": t["category"], "trait": t["trait"].strip(), "confidence": 0.55,
                    "evidence": [{"quote": t["evidence"].strip(), "source": ex["source"]}]}
            same = next((x for x in traits if x["category"] == item["category"] and similar(x["trait"], item["trait"])), None)
            if same:   # the same trait seen again is stronger evidence
                same["evidence"] += item["evidence"]
                same["confidence"] = round(min(1.0, 0.4 + 0.15 * len(same["evidence"])), 2)
            else:
                traits.append(item)
        for f in got.get("facts", []):
            if (grounded(f.get("evidence", ""), ex["text"]) and len(f.get("fact", "").split()) >= 5
                    and not (ex.get("question") and similar(f["evidence"], ex["question"]))):
                if not any(similar(f["fact"], x["fact"]) for x in facts):
                    facts.append({"fact": f["fact"].strip(), "quote": f["evidence"].strip(), "source": ex["source"]})
            else:
                dropped += 1
    if old and incremental:
        traits, facts, added = merge(old, traits, facts)
    else:
        traits.sort(key=lambda t: (CATEGORIES.index(t["category"]), -len({e["source"] for e in t["evidence"]})))
        for k, t in enumerate(traits):
            t |= {"id": f"t{k:03d}", "status": "pending"}
        for k, f in enumerate(facts):
            f |= {"id": f"f{k:03d}", "status": "pending"}
        added = {"traits": len(traits), "facts": len(facts)}
    draft = {"built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model, "examples": len(all_examples),
             "dropped_ungrounded": dropped + (old.get("dropped_ungrounded", 0) if old and incremental else 0),
             "style": style_stats(all_examples), "traits": traits, "facts": facts,
             "quotes": [{"text": e["text"], "source": e["source"]} for e in all_examples],
             "analyzed_sources": sorted({e["source"] for e in all_examples}),
             "last_update": {"new_recordings": len(examples), **added}}
    if old:   # the owner's summary and whether to use it are his: never replaced by a rebuild
        for k in ("condensed", "use_condensed"):
            if k in old:
                draft[k] = old[k]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "draft.json").write_text(json.dumps(draft, indent=2))
    return draft


def merge(old: dict, new_traits: list[dict], new_facts: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """Fold newly found traits/facts into the existing draft. A new one that matches an existing one adds its evidence
    and keeps the owner's decision and wording; genuinely new ones arrive as "pending" for review."""
    traits = [dict(t) for t in old["traits"]]
    facts = [dict(f) for f in old["facts"]]
    next_t = 1 + max((int(t["id"][1:]) for t in traits), default=-1)
    next_f = 1 + max((int(f["id"][1:]) for f in facts), default=-1)
    added = {"traits": 0, "facts": 0, "matched": 0}
    for t in new_traits:
        same = next((x for x in traits if x["category"] == t["category"] and
                     (similar(x["trait"], t["trait"]) or similar(x.get("trait_original", x["trait"]), t["trait"]))), None)
        if same:
            have = {(e["quote"], e["source"]) for e in same["evidence"]}   # the same words in a new recording count again
            same["evidence"] += [e for e in t["evidence"] if (e["quote"], e["source"]) not in have]
            same["confidence"] = round(min(1.0, 0.4 + 0.15 * len({e["source"] for e in same["evidence"]})), 2)
            added["matched"] += 1
        else:
            traits.append(t | {"id": f"t{next_t:03d}", "status": "pending"}); next_t += 1; added["traits"] += 1
    for f in new_facts:
        if not any(similar(f["fact"], x["fact"]) for x in facts):
            facts.append(f | {"id": f"f{next_f:03d}", "status": "pending"}); next_f += 1; added["facts"] += 1
    return traits, facts, added


# ---- using the approved profile ----
def load_profile(out_dir: Path) -> dict | None:
    p = out_dir / "profile.json"
    return json.loads(p.read_text()) if p.exists() else None


def style_examples(profile: dict, message: str, k: int = 2, max_words: int = 60) -> list[str]:
    """A couple of the owner's real sentences most related to what's being discussed (keyword overlap)."""
    words = set(norm(message).split()) - {"the", "a", "and", "to", "you", "i", "is", "what", "do", "how", "of"}
    pool = []
    for q in profile.get("quotes", []):
        for chunk in re.split(r"(?<=[.!?])\s+", q):
            if 8 <= len(chunk.split()) <= max_words:
                pool.append((len(words & set(norm(chunk).split())), chunk))
    pool.sort(key=lambda x: -x[0])
    return [c for _, c in pool[:k]]


def traits_block(profile: dict | None) -> str:
    """The stable part: approved traits (condensed version if the owner has one) and style numbers."""
    if not profile or not (profile.get("condensed") or profile.get("traits")):
        return ""
    traits = profile.get("condensed") or [t["trait"] for t in profile["traits"]]
    lines = ["How I talk (approved by the owner from their own recordings):"] + [f"- {t}" for t in traits]
    s = profile.get("style", {})
    if s.get("avg_sentence_words"):
        lines.append(f"- Typical sentence length: about {round(s['avg_sentence_words'])} words. Use contractions naturally.")
    ex = fixed_examples(profile)
    if ex:
        lines.append("Examples of the owner's own words (match the style, don't repeat them):")
        lines += [f'  "{e}"' for e in ex]
    return "\n".join(lines)


def fixed_examples(profile: dict, k: int = 3) -> list[str]:
    """Three of the owner's sentences, always the same ones (from different recordings), so this part of the prompt
    never changes and the model can reuse its earlier reading of it."""
    out = []
    for q in profile.get("quotes", []):
        for chunk in re.split(r"(?<=[.!?])\s+", q):
            if 12 <= len(chunk.split()) <= 30:
                out.append(chunk)
                break
        if len(out) == k:
            break
    return out


def examples_block(profile: dict | None, message: str) -> str:
    """Per-message examples are no longer used: Ollama merges every system message into the first one, so anything
    that changes per message forces the whole conversation to be re-read (≈ 3 s). Kept for reference/tests."""
    return ""
    ex = style_examples(profile, message) if profile and profile.get("traits") else []
    if not ex:
        return ""
    return "Examples of the owner's own words (match the style, don't repeat them):\n" + "\n".join(f'  "{e}"' for e in ex)


def prompt_block(profile: dict | None, message: str) -> str:
    if not profile or not profile.get("traits"):
        return ""
    lines = ["How I talk (approved by the owner from their own recordings):"]
    lines += [f"- {t['trait']}" for t in profile["traits"]]
    s = profile.get("style", {})
    if s.get("avg_sentence_words"):
        lines.append(f"- Typical sentence length: about {round(s['avg_sentence_words'])} words. Use contractions naturally.")
    ex = style_examples(profile, message)
    if ex:
        lines.append("Examples of the owner's own words (match the style, don't repeat them):")
        lines += [f'  "{e}"' for e in ex]
    return "\n".join(lines)


CONDENSE_SCHEMA = {"type": "object", "properties": {"condensed": {"type": "array", "items": {"type": "object", "properties": {
    "text": {"type": "string"}, "from": {"type": "array", "items": {"type": "string"}}}, "required": ["text", "from"]}}},
    "required": ["condensed"]}


def _condense_group(host: str, model: str, cat: str, items: dict, most: int, helper=None) -> list[dict]:
    listing = "\n".join(f"{i}: {t['trait']}" for i, t in items.items())
    prompt = (f"Traits of how the owner talks, category \"{cat}\" (id: trait):\n{listing}\n\n"
              f"Combine these into 1 to {most} instructions for an assistant speaking as the owner. Merge overlapping traits, "
              "keep each under 25 words, use only ideas from the list, and list the ids each instruction combines. "
              "Every id should be used.")
    got = helper.ask("condense", prompt, CONDENSE_SCHEMA) if helper else None
    if not isinstance(got, dict):
        r = chat_post(host, timeout=300, body={"think": False,   # background job: no thinking (slow)
        "model": model, "stream": False, "format": CONDENSE_SCHEMA,
                       "options": {"temperature": 0.2}, "keep_alive": "24h", "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        got = json.loads(r.json()["message"]["content"])
    out = []
    for c in got.get("condensed", []):
        ids = [i for i in c.get("from", []) if i in items]
        text = re.sub(r"\s*\(\s*t\d{3}(?:\s*,\s*t\d{3})*\s*\)", "", c.get("text", "")).strip()   # stray ids
        if ids and text and len(text.split()) <= 35:
            out.append({"text": text, "from": ids, "category": cat})
    return out[:most]


def condense(draft: dict, host: str = "http://127.0.0.1:11434", model: str = "llama3.1:8b", helper=None) -> list[dict]:
    """Merge the owner's approved traits into a short list, one category at a time so every kind of trait is kept
    (1–3 lines per category). Only combines what they approved; each line lists the trait ids it came from."""
    approved = [t for t in draft["traits"] if t["status"] == "approved"]
    out = []
    for cat in CATEGORIES:
        items = {t["id"]: t for t in approved if t["category"] == cat}
        if not items:
            continue
        most = 1 if len(items) <= 2 else 2 if len(items) <= 8 else 3
        got = _condense_group(host, model, cat, items, most, helper)
        if not got:   # the model returned nothing usable: keep the strongest original trait instead
            best = max(items.values(), key=lambda t: len(t["evidence"]))
            got = [{"text": best["trait"], "from": [best["id"]], "category": cat}]
        out += got
    return out
