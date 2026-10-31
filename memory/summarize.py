"""Long-term conversation memory: short summaries of conversation that has slid out of Echo's view.

Runs only when nobody has spoken for a while, so it never slows a reply or competes with the owner's voice.
Summaries are saved as memories of kind "summary", status "inferred" (never confirmed facts), source
"auto:conversation-summary", and can be read, corrected or forgotten in the "What I remember" panel.
"""
from __future__ import annotations

import time

import httpx

from core.llm import chat_post

from memory.store import Memory, MemoryStore

PROMPT = """Below is part of a conversation. "user" is the person talking with me (usually the person Echo was made for); "assistant" is me,
Echo, the owner's digital self.

Write a short note for my own memory: 1 to 3 sentences, in the first person, beginning with "On {date}".
Say what we talked about and anything the person shared (plans, events, feelings, people, things they like).
Only include what was actually said. No advice, no guesses, no opinions about the conversation.

Conversation:
{text}

Note:"""


def summarize(host: str, model: str, turns: list[dict], helper=None) -> str:
    date = time.strftime("%A, %B %-d", time.localtime(turns[0]["ts"]))
    text = "\n".join(f'{t["role"]}: {t["content"][:600]}' for t in turns)
    if helper and isinstance(note := helper.ask("summaries", PROMPT.format(date=date, text=text)), str) and note.strip():
        return note.strip().strip('"').split("\n\n")[0].strip()[:600]
    r = chat_post(host, timeout=300, body={"think": False,   # background job: no thinking (slow)
        
        "model": model, "stream": False, "keep_alive": "24h", "options": {"temperature": 0.2, "num_predict": 160},
        "messages": [{"role": "user", "content": PROMPT.format(date=date, text=text)}]})
    r.raise_for_status()
    note = r.json()["message"]["content"].strip().strip('"')
    return note.split("\n\n")[0].strip()[:600]


def summarize_pending(store: MemoryStore, window: int, host: str, model: str, max_chunks: int = 4, helper=None) -> int:
    """Summarize up to `max_chunks` chunks of out-of-view conversation. Returns how many notes were saved."""
    n = 0
    for _ in range(max_chunks):
        chunk = store.turns_to_summarize(window)
        if not chunk:
            break
        note = summarize(host, model, chunk, helper)
        if note:
            store.save(Memory(content=note, kind="summary", status="inferred", confidence=0.6,
                              source="auto:conversation-summary", span=f"turns:{chunk[0]['id']}-{chunk[-1]['id']}"))
            n += 1
        store.set_meta("summarized_upto", str(chunk[-1]["id"]))   # move on even if the note was empty
    return n
