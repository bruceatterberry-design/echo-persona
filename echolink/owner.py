"""The owner's first name in what Echo says and in his call/text prompts (owner_name in config/echo.yaml).

Blank: "my owner". Only whole phrases "the owner" / "the owner's" change; at the start of a sentence it is capitalised.
"""
from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=1)
def name() -> str:
    try:
        from core.config import load_config
        return (load_config().owner_name or "").strip()
    except Exception:  # noqa: BLE001  (no config yet, or a test)
        return ""


def personalise(text: str) -> str:
    n = name() or "my owner"
    cap = n[:1].upper() + n[1:]
    text = re.sub(r"(^|[.!?]\s+|\n\s*|\"|“)the owner\b", lambda m: m.group(1) + cap, text or "")
    return re.sub(r"\bthe owner\b", n, text)


@lru_cache(maxsize=1)
def _names() -> tuple[str, str]:
    try:
        from core.config import load_config
        c = load_config()
        return (c.owner_name or "").strip(), (c.loved_one_name or "").strip()
    except Exception:  # noqa: BLE001
        return "", ""


def display(speaker: str | None) -> str:
    """'owner' / 'loved_one' as people say them: the configured first name, else "the owner" / "the loved one"."""
    own, loved = _names()
    return (loved or "the loved one") if speaker == "loved_one" else (own or "the owner")
