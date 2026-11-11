"""Find the phone number of a place that isn't in the owner's contacts ("call Walmart pharmacy", "call the county clerk").

A web search (the same DuckDuckGo search Echo uses for questions) plus, if the results' summaries don't show a number,
a read of the top pages. The number seen most often (earlier results count more) wins. the owner sees the place, the
number and the website it came from, and approves before anything is dialed. Searches near home (config/home.json)
unless he names a town.
"""
from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlsplit

from echolink import home as _home

HOME = _home.load()["name"]   # config/home.json; blank = search without a town
PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?([2-9]\d{2})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?!\d)")
PLACE_WORDS = re.compile(r"\b(?:in|near|at|on)\s+[A-Z][a-z]+|\b(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|"
                         r"Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming)\b|"
                         r"\b\d{5}\b")


def query_for(what: str) -> str:
    w = re.sub(r"\s+", " ", what or "").strip(" .?!")
    if HOME and not PLACE_WORDS.search(w):
        w += f" {HOME}"
    return f"{w} phone number"


def numbers_in(text: str) -> list[str]:
    return [f"({a}) {b}-{c}" for a, b, c in PHONE.findall(text or "")]


def find_number(what: str, search=None, read=None) -> dict:
    """{"query", "best": {"number", "name", "source", "url"} | None, "others": [...]}"""
    if search is None:
        from core.search import search
    if read is None:
        from core.web import read_page

        def read(url):
            return read_page(url, max_chars=15000).text
    q = query_for(what)
    hits = search(q, limit=8)
    score: Counter = Counter()
    where: dict[str, tuple[str, str]] = {}
    for rank, h in enumerate(hits):
        for n in numbers_in(f"{h.title} {h.snippet}"):
            score[n] += 1.0 / (1 + rank * 0.3)
            where.setdefault(n, (h.title, h.url))
    if not score:                                # summaries had no number: read the top pages
        for rank, h in enumerate(hits[:5]):
            try:
                text = read(h.url)
            except Exception:  # noqa: BLE001
                continue
            for n in numbers_in(text)[:6]:
                score[n] += 1.0 / (1 + rank * 0.3)
                where.setdefault(n, (h.title, h.url))
    ranked = [n for n, _ in score.most_common(4)]

    def item(n):
        title, url = where[n]
        return {"number": n, "name": " ".join(w if w.isupper() else w.capitalize() for w in what.split())[:80],
                "title": title[:120], "source": urlsplit(url).netloc.removeprefix("www."), "url": url}
    return {"query": q, "best": item(ranked[0]) if ranked else None, "others": [item(n) for n in ranked[1:]]}
