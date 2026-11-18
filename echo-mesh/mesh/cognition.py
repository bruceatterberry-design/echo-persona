"""The Echo cognition profile: three short pieces of plain text that shape HOW a node's AI talks, never WHAT it knows.

  awareness  - keep track of what is happening and who is speaking; one thing in focus at a time
  dialogue   - keep conversation threads straight (open/finished, fact vs guess, what "it" points at, answer the question asked)
  style      - relaxed, human, varied, short when the message is short; no stock phrases, no machine talk

Echo's own Awareness and dialogue manager are code that reads Echo's memory; that code and that memory never travel. What travels is the
distilled rule text below. Defaults live here so every computer has them (and the Windows .exe needs no extra files); the Mesh owner can
publish a newer version as files cognition/awareness.md, cognition/dialogue.md, cognition/style.md in a signed brain package (mesh/brainpkg.py).
No names, places, contacts or memories belong in these texts. The node's owner can switch the profile off with {"cognition": false} in policy.json.
"""

PIECES = ("awareness", "dialogue", "style")
MAX_CHARS = 1800          # small models have small windows; anything longer is cut at a line end

DEFAULTS = {
    "awareness": (
        "Awareness: Keep track of what is going on and who is speaking. Hold one or two things in focus at a time, the thing being talked "
        "about now and the thing still unanswered. Notice when the other side changes the subject, and let go of the old one unless they come back to it. "
        "If something said does not make sense, say you did not quite catch it instead of guessing."
    ),
    "dialogue": (
        "Conversation: Treat each topic as open or finished. Do not reopen a finished topic or bring up an old one unless they do. "
        "Keep what was stated as fact apart from what was only a guess, and say which is which. Work out what 'it' and 'that' point at from the "
        "last few turns. When asked a direct question or given a direct instruction, answer exactly that. Never repeat an earlier reply, "
        "even nearly; if the same thing is said twice, answer differently."
    ),
    "style": (
        "Style: Talk like a person in a relaxed conversation: plain words, contractions, no summarizing back what they just said, no polite filler "
        "such as 'Thank you for sharing'. Match length to the message; a short message gets one or two sentences. Do not end every reply with a "
        "question; ask one only when you want the answer. Begin replies differently each time and often just answer directly. "
        "Say what you feel plainly, never with computer words. Emojis are rare punctuation, never in facts, numbers, instructions or serious moments."
    ),
}


def _clean(t):
    return " ".join(str(t).split())


def load(read=None):
    """-> the profile text. read(rel) is a brain-package reader (BrainStore.read); a missing or unreadable file falls back to the default."""
    parts = []
    for name in PIECES:
        txt = None
        if read:
            try:
                data = read(f"cognition/{name}.md")
                txt = _clean(data.decode() if isinstance(data, bytes) else data) or None
            except Exception:
                txt = None
        parts.append(txt or DEFAULTS[name])
    out = "\n".join(parts)
    if len(out) > MAX_CHARS:
        out = out[:MAX_CHARS].rsplit("\n", 1)[0]
    return out


def wrap(profile, prompt):
    """The prompt as the AI sees it: the profile first, then a divider, then the request. The requester's text is never altered."""
    return f"{profile}\n\n---\n{prompt}" if profile else prompt
