"""Who Echo lives with, from this install's settings (config/echo.yaml: owner_name, loved_one_name).

Every rule that needs a person's name (who may be addressed by name, what counts as "This is <name>", a stock opening
like "That's wonderful, <name>.") asks here, so nothing is tied to one household. Blank names are simply left out.
"""
from __future__ import annotations

import re

_names = {"owner": "", "loved_one": ""}


def configure(owner_name: str = "", loved_one_name: str = "") -> None:
    _names.update(owner=(owner_name or "").strip(), loved_one=(loved_one_name or "").strip())


def owner() -> str:
    return _names["owner"]


def loved_one() -> str:
    return _names["loved_one"]


def names() -> list[str]:
    """The configured names, owner first (empty ones left out)."""
    return [n for n in (_names["owner"], _names["loved_one"]) if n]


def alternation(extra: tuple[str, ...] = ()) -> str:
    """A regex alternation of the names (lower case), or a pattern that never matches when none are set."""
    ns = [re.escape(n.lower()) for n in [*names(), *extra] if n]
    return "|".join(ns) if ns else r"(?!x)x"


def display(speaker: str | None) -> str:
    """A voice-profile id as a name: "owner" → the owner's name (or "the owner"), "loved_one" → theirs; others as is."""
    if speaker == "owner":
        return owner() or "the owner"
    if speaker == "loved_one":
        return loved_one() or "my loved one"
    return (speaker or "").replace("_", " ").title()
