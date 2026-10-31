"""Echo's safe page reader: fetch a web page and pull out its readable text, title and links.

Used by Learn (memory/learn.py) and, from step 2, by Echo's own tools. Runs entirely on this Mac.

Safety (ideas adapted from JARVIS tools/web_browse.py, MIT):
- only http/https, no user:password@ in the address;
- every address, including every redirect hop, must resolve only to public internet addresses: this Mac
  (127.x, Echo's own services), the home network (192.168.x, 10.x), Tailscale (100.64/10), link-local and cloud
  metadata are refused, so a page can't bounce Echo into his own or the family's devices;
- redirects followed by hand (at most 5), size cap while downloading, timeout, only text-like content types;
- nothing is downloaded to disk here and no page script ever runs (plain HTTP, no browser).
Known limit: DNS can change between the check and the connection (rebinding); acceptable for a personal reader.
"""
from __future__ import annotations

import html.parser
import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Echo/1.0 (personal, local)"
MAX_BYTES = 30 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT = 20.0
TEXT_TYPES = {"text/html", "text/plain", "application/xhtml+xml"}
READ_TYPES = TEXT_TYPES | {"application/pdf"}
TRANSPORT: httpx.BaseTransport | None = None   # tests put a MockTransport here


class UnsafeAddress(ValueError):
    """The address (or a redirect) points somewhere Echo must not go."""


class FetchError(ValueError):
    """The page couldn't be fetched or read (plain-language message)."""


def _resolve(host: str) -> list[str]:
    return [a[4][0] for a in socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)]


def normalise(url: str) -> str:
    url = url.strip().strip("<>\"'")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
        url = "https://" + url
    return url


def check_url(url: str) -> None:
    """Raise UnsafeAddress unless `url` is an http(s) address on the public internet."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeAddress("I only open web addresses (http or https)")
    if parts.username or parts.password:
        raise UnsafeAddress("that address has a login name in it, so I won't open it")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeAddress("that address has no site name")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa", ".ts.net")):
        raise UnsafeAddress(f"{host} is on this Mac or the home network, not the internet")
    try:
        addrs = [host] if _is_ip(host) else _resolve(host)
    except (socket.gaierror, UnicodeError):
        raise FetchError(f"I couldn't find the site {host}") from None
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise UnsafeAddress(f"{host} points to a private address ({ip}), not the internet")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


@dataclass
class Fetched:
    url: str            # what was asked for
    final_url: str      # after redirects
    content_type: str
    body: bytes
    encoding: str | None = None
    seconds: float = 0.0

    @property
    def text(self) -> str:
        return self.body.decode(self.encoding or "utf-8", errors="replace")


def fetch(url: str, *, max_bytes: int = MAX_BYTES, timeout: float = TIMEOUT, allowed: set[str] = READ_TYPES) -> Fetched:
    """GET a public web page safely (see module notes). Raises UnsafeAddress / FetchError."""
    t0 = time.perf_counter()
    url = normalise(url)
    current = url
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=TRANSPORT,
                      headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,application/pdf;q=0.8,*/*;q=0.1"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            check_url(current)
            try:
                with client.stream("GET", current) as r:
                    if r.is_redirect:
                        loc = r.headers.get("location", "")
                        if not loc:
                            raise FetchError("the site sent me somewhere without saying where")
                        current = urljoin(current, loc)
                        continue
                    if r.status_code >= 400:
                        raise FetchError(f"the site answered with error {r.status_code}")
                    kind = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if kind not in allowed:
                        raise FetchError(f"that address gives {kind or 'something'} I can't read")
                    declared = int(r.headers.get("content-length") or 0)
                    if declared > max_bytes:
                        raise FetchError("that page is too big")
                    buf = bytearray()
                    for chunk in r.iter_bytes():
                        buf += chunk
                        if len(buf) > max_bytes:
                            raise FetchError("that page is too big")
                    return Fetched(url, current, kind, bytes(buf), r.charset_encoding, time.perf_counter() - t0)
            except httpx.TimeoutException:
                raise FetchError("the site took too long to answer") from None
            except httpx.HTTPError as e:
                raise FetchError(f"I couldn't reach the site ({type(e).__name__})") from None
    raise FetchError("the site redirected me too many times")


# ---------- readable text ----------
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "form", "button", "select",
              "nav", "footer", "header", "aside", "dialog", "menu"}
_BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th", "section", "article", "main",
          "blockquote", "pre", "dd", "dt", "figcaption", "table", "ul", "ol", "hr"}
_VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr", "param"}
_NOISE = re.compile(r"(^|[\s_-])(nav|navbar|menu|sidebar|footer|header|cookie|consent|banner|advert|ads?|promo|"
                    r"newsletter|subscribe|share|social|related|breadcrumbs?|skip-link|popup|modal)([\s_-]|$)", re.I)
_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "dialog", "alert", "menu", "menubar"}
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿]")


@dataclass
class Link:
    text: str
    url: str


@dataclass
class Page:
    url: str
    title: str = ""
    description: str = ""
    text: str = ""
    links: list[Link] = field(default_factory=list)
    truncated: bool = False
    fetched_at: float = 0.0
    seconds: float = 0.0
    rendered: bool = False   # read with the full browser (core/jsbrowser.py)


class _Reader(html.parser.HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.title, self._in_title = "", False
        self.description = ""
        self.skip_tag, self.skip_depth = "", 0      # the element being skipped and how deep in it we are
        self.main_depth = 0                         # inside <main>/<article>/role=main
        self.all: list[str] = []
        self.main: list[str] = []
        self.links: list[Link] = []
        self._a: tuple[str, list[str]] | None = None

    def _skippable(self, tag: str, a: dict) -> bool:
        if tag in _SKIP_TAGS or "hidden" in a or a.get("aria-hidden") == "true":
            return True
        if (a.get("role") or "").lower() in _ROLES:
            return True
        style = (a.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        ident = f"{a.get('id') or ''} {a.get('class') or ''}"
        return tag in ("div", "section", "ul", "ol", "span", "p", "table") and bool(_NOISE.search(ident))

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if self.skip_tag:
            if tag == self.skip_tag and tag not in _VOID:
                self.skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta" and not self.description and (a.get("name") or a.get("property") or "").lower() in (
                "description", "og:description"):
            self.description = re.sub(r"\s+", " ", a.get("content") or "").strip()[:500]
            return
        if self._skippable(tag, a) and tag not in _VOID:
            self.skip_tag, self.skip_depth = tag, 1
            return
        if tag in ("main", "article") or a.get("role") == "main":
            self.main_depth += 1
        if tag == "a" and a.get("href"):
            self._a = (a["href"], [])
        if tag in _BLOCK:
            self._emit("\n")
        if tag == "li":
            self._emit("• ")

    def handle_endtag(self, tag):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth <= 0:
                    self.skip_tag = ""
            return
        if tag == "title":
            self._in_title = False
        if tag in ("main", "article") and self.main_depth:
            self.main_depth -= 1
        if tag == "a" and self._a:
            href, words = self._a
            self._a = None
            url = urljoin(self.base, href.strip())
            if url.startswith(("http://", "https://")):
                self.links.append(Link(re.sub(r"\s+", " ", "".join(words)).strip()[:120], url.split("#")[0]))
        if tag in _BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self.skip_tag:
            return
        if self._a:
            self._a[1].append(data)
        self._emit(data)

    def _emit(self, s: str):
        self.all.append(s)
        if self.main_depth:
            self.main.append(s)


def _tidy(parts: list[str]) -> str:
    text = _INVISIBLE.sub("", "".join(parts)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    out, blank = [], False
    for ln in lines:
        if not ln or ln == "•":
            blank = bool(out)
            continue
        if blank:
            out.append("")
        out.append(ln)
        blank = False
    return "\n".join(out).strip()


def readable(html_src: str, base_url: str = "") -> Page:
    """Title, main readable text and links of an HTML page (menus, footers, cookie banners, scripts dropped)."""
    r = _Reader(base_url)
    try:
        r.feed(html_src)
        r.close()
    except Exception:  # noqa: BLE001  (html.parser is lenient; keep whatever was read)
        pass
    main, every = _tidy(r.main), _tidy(r.all)
    text = main if len(main) >= 300 or (main and len(main) >= 0.5 * len(every)) else every
    desc = _INVISIBLE.sub("", r.description)
    if desc and desc[:60].lower() not in text.lower():   # the page's own summary, when the text doesn't already start with it
        text = f"{desc}\n\n{text}" if text else desc
    seen, links = set(), []
    for ln in r.links:
        if ln.url not in seen and ln.url.rstrip("/") != base_url.split("#")[0].rstrip("/"):
            seen.add(ln.url)
            links.append(ln)
    return Page(url=base_url, title=re.sub(r"\s+", " ", r.title).strip(), description=desc, text=text, links=links)


def read_page(url: str, max_chars: int = 20000) -> Page:
    """Fetch + extract. Text is cut to `max_chars` (truncated=True)."""
    f = fetch(url, allowed=TEXT_TYPES)
    if f.content_type == "text/plain":
        page = Page(url=f.final_url, text=_tidy([f.text]))
    else:
        page = readable(f.text, f.final_url)
    if len(page.text) > max_chars:
        cut = page.text.rfind("\n", 0, max_chars)
        page.text = page.text[: cut if cut > max_chars // 2 else max_chars].rstrip()
        page.truncated = True
    page.fetched_at, page.seconds = time.time(), f.seconds
    return page

