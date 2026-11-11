"""What Echo calls someone when he greets them, from the name as it's saved in the owner's contacts.

  the owner's loved one → the loved one            Neighbor Jo Miller → Jo       Terry (cell) → Terry
  Dr. Alex Grant → Dr. Grant         Aunt May → Aunt May               Mom → Mom
  VZ Roadside Assistance, Apple Inc., job world → ""  (a business: "Hello, this is Echo …", no name)
The tablet has the same rules (Phonebook.greetName in Calls.kt).
"""
from __future__ import annotations

import re

TITLES = {"dr", "mr", "mrs", "ms", "miss", "mx", "pastor", "rev", "reverend", "father", "fr", "coach", "officer",
          "judge", "sister", "brother", "professor", "prof", "deacon", "sgt", "captain", "capt"}
ABBR = {"dr", "mr", "mrs", "ms", "mx", "rev", "fr", "prof", "sgt", "capt"}
FAMILY = {"aunt", "auntie", "uncle", "grandma", "grandpa", "granny", "nana", "papa", "cousin", "mom", "mother", "owner",
          "father-in-law", "mother-in-law"}
DESCRIBERS = {"neighbor", "neighbour", "coworker", "co-worker", "work", "boss", "friend", "old", "new", "my", "the",
              "home", "cell", "mobile", "office"}
BUSINESS = re.compile(r"\b(?:inc|llc|ltd|co|corp|company|assistance|roadside|services?|service|shop|store|pharmacy|"
                      r"clinic|dental|dentist|bank|insurance|auto|repair|restaurant|pizza|hospital|school|church|"
                      r"office|support|center|centre|world|plumbing|electric|roofing|molding|construction|"
                      r"contractors?|salon|vet|veterinary|hotel|delivery|nailed it)\b\.?", re.I)


def greeting_name(saved: str) -> str:
    n = re.sub(r"\(.*?\)|\[.*?\]", " ", saved or "")                  # "Terry (cell)" → "Terry"
    n = re.sub(r"[^\w.'\- ]", " ", n)
    words = [w for w in n.split() if w]
    if not words:
        return ""
    if BUSINESS.search(" ".join(words)) and not words[0].lower().rstrip(".") in TITLES | FAMILY:
        # a person's name before a trade ("Noe Contractor Crown Molding") is still the person
        first = words[0]
        if len(words) >= 2 and words[1].lower().rstrip("s") in {"contractor", "plumber", "electrician", "painter"}:
            return first
        return ""
    if all(w.islower() for w in words):                                  # "job world": not how people are saved
        return ""
    w0 = words[0].lower().rstrip(".")
    if w0 in TITLES:                                                     # "Dr. Alex Grant" → "Dr. Grant"
        title = words[0] if words[0].endswith(".") or w0 not in ABBR else words[0] + "."
        return f"{title} {words[-1]}" if len(words) >= 2 else title
    if w0 in FAMILY:
        return " ".join(words[:2]) if len(words) >= 2 else words[0]
    while len(words) > 1 and words[0].lower() in DESCRIBERS:              # "Neighbor Jo Miller" → "Jo"
        words = words[1:]
    return words[0]


def hello(saved: str) -> str:
    g = greeting_name(saved)
    return f"Hello {g}" if g else "Hello"


def thanks(saved: str) -> str:
    g = greeting_name(saved)
    return f"Thank you for your time, {g}. Talk to you soon." if g else "Thank you for your time. Talk to you soon."
