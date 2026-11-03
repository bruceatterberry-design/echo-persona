"""Date and clock arithmetic done exactly, not by the language model (the owner, 2026-11-01, after Stage 8).

The local model got "If today is Wednesday, what day will it be in 10 days?" wrong in 17 of the benchmark runs and a
"leaves at 2:45, takes 1 hour 50 minutes" train sum wrong 4 times. Here plain code works it out; core/server.py adds
the result as a one-time note ("[Date check: …]", stripped from history) and the model just says it in his words.
No LLM call, no network. Recognises:
  weekday + N days/weeks       "if today is Wednesday, what day will it be in 10 days" / "what day is it in 3 days"
  date + N days/weeks           "what's the date in 2 weeks" / "what date is 45 days from today"
  days until a date             "how many days until Christmas" / "until March 14"
  clock time + duration         "leaves at 2:45 PM and takes 1 hour 50 minutes, what time does it arrive"
"""
from __future__ import annotations

import datetime as dt
import re

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
          "december"]
WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
WORDS |= {"a": 1, "an": 1, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "ninety": 90, "hundred": 100}
NUM = r"(\d+|" + "|".join(sorted(WORDS, key=len, reverse=True)) + r")"
HOLIDAYS = {"christmas": (12, 25), "christmas eve": (12, 24), "new year's eve": (12, 31), "new years eve": (12, 31),
            "new year's day": (1, 1), "new year": (1, 1), "halloween": (10, 31), "valentine's day": (2, 14),
            "independence day": (7, 4), "the fourth of july": (7, 4)}


def _n(s: str) -> int:
    return int(s) if s.isdigit() else WORDS[s.lower()]


def _span(n: int, unit: str) -> int:
    return n * 7 if unit.startswith("week") else n


def _fmt(d: dt.date) -> str:
    return f"{DAYS[d.weekday()].title()}, {MONTHS[d.month - 1].title()} {d.day}, {d.year}"


def _clock(t: dt.datetime) -> str:
    return t.strftime("%-I:%M %p")


_WD_PLUS = re.compile(r"\b(?:if )?today(?:'s| is) (" + "|".join(DAYS) + r")\b.{0,40}?\b(?:in|after) " + NUM +
                      r" (days?|weeks?)\b", re.I)
_DAY_IN = re.compile(r"\bwhat (?:day|weekday)(?: of the week)? (?:will it be|is it|would it be|will be|is)\b.{0,15}?"
                     r"\b(?:in|after) " + NUM + r" (days?|weeks?)\b|\b" + NUM + r" (days?|weeks?) from (?:now|today)\b"
                     r".{0,30}\bwhat day\b|\bwhat day\b.{0,30}\b" + NUM + r" (days?|weeks?) from (?:now|today)\b", re.I)
_DATE_IN = re.compile(r"\bwhat(?:'s| is| will be| will) the date\b.{0,20}?\b(?:in|after) " + NUM + r" (days?|weeks?)\b|"
                      r"\bwhat date (?:is|will it be|would it be)\b.{0,10}?\b" + NUM + r" (days?|weeks?) from (?:now|today)\b",
                      re.I)
_UNTIL = re.compile(r"\bhow many (?:days|weeks) (?:until|till|til|to|before) (?:the )?(?:(" + "|".join(MONTHS) +
                    r") (\d{1,2})(?:st|nd|rd|th)?|(" + "|".join(re.escape(h) for h in sorted(HOLIDAYS, key=len, reverse=True))
                    + r"))\b", re.I)
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?=[\s,.?]|$)", re.I)
_DUR = re.compile(r"\b(?:" + NUM + r" (?:hours?|hrs?))?(?:,? (?:and )?)?(?:" + NUM + r" (?:minutes?|mins?))?\b", re.I)
_TIME_Q = re.compile(r"\b(?:what time|when (?:will|does|do|would)|arrive|get there|finish|be done|end)\b", re.I)


def answer(text: str, today: dt.date | None = None) -> str | None:
    """The exact answer as a short fact, or None if this isn't a date/time sum."""
    t = text or ""
    today = today or dt.date.today()
    if (m := _WD_PLUS.search(t)):
        start, n, unit = DAYS.index(m.group(1).lower()), _n(m.group(2)), m.group(3)
        days = _span(n, unit)
        return f"{m.group(1).title()} + {days} days = {DAYS[(start + days) % 7].title()}"
    if (m := _DAY_IN.search(t)):
        g = [x for x in m.groups() if x]
        days = _span(_n(g[0]), g[1])
        d = today + dt.timedelta(days=days)
        return f"today is {_fmt(today)}; in {days} days it will be {_fmt(d)}"
    if (m := _DATE_IN.search(t)):
        g = [x for x in m.groups() if x]
        days = _span(_n(g[0]), g[1])
        return f"today is {_fmt(today)}; {days} days from today is {_fmt(today + dt.timedelta(days=days))}"
    if (m := _UNTIL.search(t)):
        if m.group(3):
            mo, day = HOLIDAYS[m.group(3).lower()]
            name = m.group(3)
        else:
            mo, day, name = MONTHS.index(m.group(1).lower()) + 1, int(m.group(2)), f"{m.group(1).title()} {m.group(2)}"
        target = dt.date(today.year, mo, day)
        if target < today:
            target = dt.date(today.year + 1, mo, day)
        return f"today is {_fmt(today)}; {name} is {_fmt(target)}, {(target - today).days} days away"
    if _TIME_Q.search(t):
        times = [m for m in _TIME.finditer(t) if m.group(2) or m.group(3)]   # "2:45" or "3 pm", not bare numbers
        durs = [m for m in _DUR.finditer(t) if m.group(1) or m.group(2)]
        if len(times) == 1 and durs:
            m, d = times[0], durs[0]
            h, mi = int(m.group(1)), int(m.group(2) or 0)
            ap = (m.group(3) or "").lower().replace(".", "")
            if ap == "pm" and h < 12:
                h += 12
            if ap == "am" and h == 12:
                h = 0
            if h > 23 or mi > 59:
                return None
            hours, mins = _n(d.group(1)) if d.group(1) else 0, _n(d.group(2)) if d.group(2) else 0
            start = dt.datetime(2000, 1, 1, h, mi)
            end = start + dt.timedelta(hours=hours, minutes=mins)
            span = " and ".join(x for x in (f"{hours} hour{'s' * (hours != 1)}" if hours else "",
                                             f"{mins} minute{'s' * (mins != 1)}" if mins else "") if x)
            nextday = " the next day" if end.date() > start.date() else ""
            return f"{_clock(start)} + {span} = {_clock(end)}{nextday}" + ("" if ap else " (am/pm not given)")
    return None


def note(text: str, today: dt.date | None = None) -> str:
    a = answer(text, today)
    return f"[Date check, worked out exactly: {a}. I use this result.]" if a else ""
