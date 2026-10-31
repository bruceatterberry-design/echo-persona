"""Web search for Echo (JARVIS integration step 5; JARVIS used the `ddgs` package, this needs none).

DuckDuckGo's plain HTML results page, fetched through core/web.py (public internet only, size limits). Ads are skipped,
DuckDuckGo's redirect links are unwrapped to the real address, and only http(s) results are kept. The search words go to
DuckDuckGo (no account, no cookies); nothing else does. Results are untrusted data like any web page.
"""
from __future__ import annotations

import html.parser
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, urlsplit

from core import web

ENDPOINT = "https://html.duckduckgo.com/html/?q="


@dataclass
class Hit:
    title: str
    url: str
    snippet: str


class _Results(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hits: list[Hit] = []
        self._ad = False          # inside an ad block
        self._field = None        # "title" | "snippet"
        self._href = ""
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "div" and "result" in cls.split():
            self._ad = "result--ad" in cls
        if self._ad:
            return
        if tag == "a" and "result__a" in cls.split():
            self._field, self._href, self._buf = "title", a.get("href") or "", []
        elif tag in ("a", "div", "td") and "result__snippet" in cls.split():
            self._field, self._buf = "snippet", []

    def handle_endtag(self, tag):
        if self._field == "title" and tag == "a":
            url = unwrap(self._href)
            if url:
                self.hits.append(Hit(_clean("".join(self._buf)), url, ""))
            self._field = None
        elif self._field == "snippet" and tag in ("a", "div", "td"):
            if self.hits and not self.hits[-1].snippet:
                self.hits[-1].snippet = _clean("".join(self._buf))
            self._field = None

    def handle_data(self, data):
        if self._field:
            self._buf.append(data)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def unwrap(href: str) -> str | None:
    """DuckDuckGo's //duckduckgo.com/l/?uddg=<real address> → the real address (http/https only)."""
    if href.startswith("//"):
        href = "https:" + href
    p = urlsplit(href)
    if (p.hostname or "").endswith("duckduckgo.com") and p.path.startswith("/l/"):
        href = (parse_qs(p.query).get("uddg") or [""])[0]
        p = urlsplit(href)
    if p.scheme not in ("http", "https") or not p.hostname or (p.hostname or "").endswith("duckduckgo.com"):
        return None
    return href


def parse(html_src: str, limit: int = 6) -> list[Hit]:
    r = _Results()
    r.feed(html_src)
    seen, out = set(), []
    for h in r.hits:
        if h.url not in seen:
            seen.add(h.url)
            out.append(h)
    return out[:limit]


def search(query: str, limit: int = 6) -> list[Hit]:
    query = _clean(query)[:300]
    if not query:
        return []
    f = web.fetch(ENDPOINT + quote_plus(query), max_bytes=2 * 1024 * 1024, allowed=web.TEXT_TYPES)
    return parse(f.text, limit)
