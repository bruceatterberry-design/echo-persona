"""Validated structured response from the LLM. Never executed, only displayed/spoken."""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

Expression = Literal["neutral", "smiling", "thoughtful", "attentive", "concerned"]

RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "expression": {"type": "string", "enum": list(Expression.__args__)},
        "intensity": {"type": "number"},
    },
    "required": ["text", "expression", "intensity"],
}


class EchoReply(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    expression: Expression = "neutral"
    intensity: float = Field(0.25, ge=0.0, le=1.0)


def parse_reply(raw: str) -> EchoReply:
    """Parse model output. Falls back to plain text + neutral if malformed."""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            # clamp rather than reject an over-eager intensity
            if isinstance(data.get("intensity"), (int, float)):
                data["intensity"] = min(max(float(data["intensity"]), 0.0), 1.0)
            if data.get("expression") not in Expression.__args__:
                data["expression"] = "neutral"
            return EchoReply.model_validate(data)
    except (json.JSONDecodeError, ValidationError):
        pass
    text = raw.strip() or "Sorry, I lost my train of thought there."
    return EchoReply(text=text[:8000], expression="neutral", intensity=0.0)


TAG_RE = re.compile(r"^\s*\[\s*([a-zA-Z]+)\s*\]\s*")


class StreamParser:
    """Splits a streamed reply that starts with an expression tag, e.g. "[smiling] Well, hello!".

    feed() returns the new spoken-text delta (tag removed); finish() returns the validated reply.
    A missing or unknown tag means neutral, so a model that forgets the tag still works.
    """

    def __init__(self, allow_name: bool = True, name: str = "") -> None:
        self.names = NameFilter(name, allow=allow_name)
        self.buf = ""
        self.expression: str = "neutral"
        self.tag_done = False
        self.text = ""
        self.leftover = ""

    def feed(self, piece: str) -> str:
        """Returns the new text to show and speak (tag and stage directions removed)."""
        return self._actions(self._tag(piece))

    def _actions(self, piece: str, final: bool = False) -> str:
        if not hasattr(self, "_af"):
            self._af = ActionFilter()
        clean, expr = self._af.feed(piece, final)
        clean = self.names.feed(clean, final)
        if expr:
            self.expression = expr   # the avatar shows it from here on
        self.text += clean
        return clean

    def _tag(self, piece: str) -> str:
        if self.tag_done:
            return piece
        self.buf += piece
        stripped = self.buf.lstrip()
        if stripped and not stripped.startswith("["):
            self.tag_done = True
        elif "]" in stripped or len(stripped) > 24:
            m = TAG_RE.match(stripped)
            if m and m.group(1).lower() in Expression.__args__:
                self.expression = m.group(1).lower()
                stripped = stripped[m.end():]
            elif m:   # another one-word stage direction, e.g. "[smiles]": map it if we can
                self.expression = action_expression(m.group(1)) or self.expression
                stripped = stripped[m.end():]
            self.tag_done = True
        else:
            return ""
        return stripped

    def finish(self) -> EchoReply:
        if not self.tag_done:
            self.feed(" ")  # flush a reply that was only a tag or very short
            if not self.tag_done:
                self.tag_done = True
                self._actions(self.buf)
        self.leftover = self._actions("", final=True)   # anything held back at the very end
        text = self.text.strip() or "Sorry, I lost my train of thought there."
        return EchoReply(text=text[:8000], expression=self.expression,
                            intensity=0.0 if self.expression == "neutral" else 0.3)


# Stage directions the model sometimes writes ("[smiles]", "*chuckles softly*"). They are not spoken or shown;
# they become an expression on the avatar instead.
ACTION_WORDS = {
    "smiling": ("smil", "grin", "laugh", "chuckl", "beam", "wink", "happ", "warm", "giggl"),
    "thoughtful": ("think", "ponder", "paus", "hmm", "consider", "reflect", "wonder", "recall", "rememb"),
    "concerned": ("concern", "worr", "frown", "sigh", "sad", "sorry", "gentl", "soft"),
    "attentive": ("nod", "listen", "lean", "attent", "tilt", "surpris", "raise", "eyebrow", "look"),
}
MAX_ACTION = 120   # an unclosed [ or * longer than this is ordinary text


def action_expression(inner: str) -> str | None:
    t = inner.lower()
    for expr, stems in ACTION_WORDS.items():   # order matters: "smiles gently" is a smile
        if any(re.search(rf"\b{st}", t) for st in stems):
            return expr
    return None


class ActionFilter:
    """Removes stage directions from streamed text and reports the expression they describe."""

    def __init__(self) -> None:
        self.hold = ""
        self.last = ""   # last character sent, so a removed direction never leaves a double space

    def feed(self, piece: str, final: bool = False) -> tuple[str, str | None]:
        text, expr, out = self.hold + piece, None, []
        self.hold = ""
        i = 0
        while i < len(text):
            ch = text[i]
            if ch in "[*":
                close = "]" if ch == "[" else "*"
                j = text.find(close, i + 1)
                if j < 0:                                    # not closed yet: wait for more text
                    if not final and len(text) - i <= MAX_ACTION:
                        self.hold = text[i:]
                        break
                    out.append(text[i:] if ch == "*" else text[i + 1:]); break
                inner = text[i + 1:j]
                e = action_expression(inner)
                if ch == "[" or e or len(inner.split()) > 3:  # a stage direction: drop it
                    expr = e or expr
                else:                                         # *really* is emphasis: keep the words
                    out.append(inner)
                i = j + 1
                continue
            out.append(ch)
            i += 1
        clean = re.sub(r"[ \t]{2,}", " ", "".join(out))
        if self.last in (" ", "") and clean.startswith(" "):
            clean = clean.lstrip(" ")
        if clean:
            self.last = clean[-1]
        return clean, expr


class NameFilter:
    """Drops the loved one's name when it's only used to address them ("Goodnight, Sam." → "Goodnight.", "Sam, I'm here" →
    "I'm here"), unless allowed this time. Mentions like "Sam's garden" are kept. Works on streamed text: a possible
    start of ", Sam" is held back until it's clear. No name set (loved_one_name in config) = nothing is dropped."""

    def __init__(self, name: str = "", allow: bool = False) -> None:
        self.name, self.allow = name, allow or not name.strip()
        self.buf, self.sentence_start = "", True
        n = re.escape(name)
        self.after_comma = re.compile(rf",\s*{n}(?=\s*[,.!?;:]|\s*$)(\s*,)?", re.I)   # ", Sam." / ", Sam," → "." / ","
        self.leading = re.compile(rf"^(\s*){n},\s*(\w)", re.I)                      # "Sam, I'm…" at a sentence start
        self.stems = [s.lower() for s in (f", {name}", f",{name}", f"{name},", f"{name}.", f"{name}!", f"{name}?", f"{name} ")]

    def _hold(self, text: str) -> int:
        low = text.lower()
        hold = max((k for s in self.stems for k in range(1, len(s)) if low.endswith(s[:k])), default=0)
        i = low.rfind(self.name.lower())   # keep a recent name together with the punctuation around it
        if i != -1 and len(text) - i <= len(self.name) + 2:
            while i > 0 and text[i - 1] in ", ":
                i -= 1
            hold = max(hold, len(text) - i)
        return hold

    def feed(self, piece: str, final: bool = False) -> str:
        if self.allow:
            return piece
        self.buf += piece
        hold = 0 if final else min(self._hold(self.buf), len(self.buf))
        emit, self.buf = (self.buf[:-hold], self.buf[-hold:]) if hold else (self.buf, "")
        emit = self.after_comma.sub(lambda m: "," if m.group(1) else "", emit)
        if self.sentence_start:
            emit = self.leading.sub(lambda m: m.group(1) + m.group(2).upper(), emit)
        emit = re.sub(r"([.!?]\s+)" + re.escape(self.name) + r",\s*(\w)", lambda m: m.group(1) + m.group(2).upper(), emit, flags=re.I)
        if emit.strip():
            self.sentence_start = emit.rstrip()[-1] in ".!?"
        return emit
