"""A full browser for pages that show nothing without JavaScript (JARVIS step 5b; JARVIS used Playwright too).

Only used when the plain reader (core/web.py) finds almost no text. Playwright drives the Google Chrome already on this
Mac (channel "chrome", no extra browser download), invisibly, with a fresh empty profile every time:
- none of the owner's or the loved one's logins, cookies or history; nothing kept afterwards;
- every request the page makes (scripts, data, frames, redirects) must pass core/web.check_url: public internet only,
  so a page can't reach Echo's own services or the home network; images, video and fonts are skipped (speed, memory);
- no downloads, no pop-up windows, dialogs dismissed, no permissions (camera, mic, location, notifications);
- one page at a time, closed straight after (about 4 s and a few hundred MB while it runs).
"""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlsplit

from core import web

log = logging.getLogger("echo.jsbrowser")
TIMEOUT_MS = 20000
SETTLE_MS = 6000
MAX_HTML = 5 * 1024 * 1024
SKIP_TYPES = {"image", "media", "font"}
_lock = threading.Lock()
THIN = 300   # fewer readable characters than this → try the full browser
JS_HINT = ("enable javascript", "javascript is required", "requires javascript", "turn on javascript",
           "javascript is disabled", "please enable js", "you need to enable javascript")


def needs_js(page: web.Page, raw_html: str = "") -> bool:
    low = (page.text + " " + raw_html[:20000]).lower()
    return len(page.text) < THIN or any(h in low for h in JS_HINT)


def guard():
    """The request filter for a page: (route handler, stats). Every request must be http(s) to the public internet
    (core/web.check_url, once per site name); images, video and fonts are skipped."""
    ok_hosts: dict[str, bool] = {}
    stats = {"blocked": 0, "unsafe": 0}

    def allowed(req_url: str) -> bool:
        p = urlsplit(req_url)
        if p.scheme in ("data", "blob"):
            return True
        if p.scheme not in ("http", "https"):
            return False
        key = f"{p.hostname}:{p.port}"
        if key not in ok_hosts:
            try:
                web.check_url(req_url)
                ok_hosts[key] = True
            except (web.UnsafeAddress, web.FetchError):
                ok_hosts[key] = False
        return ok_hosts[key]

    def route(r):
        if r.request.resource_type in SKIP_TYPES:
            stats["blocked"] += 1
            return r.abort()
        if not allowed(r.request.url):
            stats["blocked"] += 1
            stats["unsafe"] += 1
            log.info("blocked a request to a non-public address (%s)", urlsplit(r.request.url).hostname)
            return r.abort()
        return r.continue_()
    return route, stats


def render(url: str) -> tuple[str, str]:
    """(html, final address) after the page's scripts have run. Raises web.UnsafeAddress / web.FetchError."""
    url = web.normalise(url)
    web.check_url(url)
    try:
        from playwright.sync_api import Error as PWError, sync_playwright
    except ImportError:
        raise web.FetchError("the full browser isn't installed") from None
    t0 = time.perf_counter()
    route, stats = guard()
    with _lock, sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome", headless=True,
                                         args=["--disable-extensions", "--no-first-run", "--disable-sync"])
        except PWError:
            raise web.FetchError("I couldn't start the full browser (is Google Chrome installed?)") from None
        try:
            ctx = browser.new_context(accept_downloads=False, service_workers="block", permissions=[],
                                      user_agent=web.USER_AGENT, java_script_enabled=True)

            ctx.route("**/*", route)
            page = ctx.new_page()
            ctx.on("page", lambda p: p.close())   # any further window (pop-ups) is closed at once
            page.on("dialog", lambda d: d.dismiss())
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            except PWError as e:
                raise web.FetchError(f"the page wouldn't load ({str(e).splitlines()[0][:80]})") from None
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
            except PWError:
                pass   # busy pages never go quiet; read what's there
            final = page.url
            web.check_url(final)
            html = page.content()
        finally:
            browser.close()
    if len(html) > MAX_HTML:
        html = html[:MAX_HTML]
    log.info("rendered host=%s ms=%d blocked=%d", urlsplit(final).hostname, (time.perf_counter() - t0) * 1000,
             stats["blocked"])
    return html, final


def read_page(url: str, max_chars: int = 20000) -> web.Page:
    """Like web.read_page, but with the page's JavaScript run first."""
    html, final = render(url)
    page = web.readable(html, final)
    if len(page.text) > max_chars:
        page.text, page.truncated = page.text[:max_chars], True
    page.fetched_at = time.time()
    return page


def read_smart(url: str, max_chars: int = 20000) -> web.Page:
    """Plain reader first (fast); the full browser only when that finds almost nothing. Judged on the whole text,
    then cut to max_chars."""
    first_error = None
    try:
        page = web.read_page(url, max_chars=10**8)
    except web.FetchError as e:
        if "can't read" in str(e) or "couldn't find the site" in str(e):   # a file, or no such site: no help there
            raise
        page, first_error = None, e        # e.g. 403 for robots: a real browser often gets in
    if page is None or needs_js(page):
        try:
            js = read_page(url, 10**8)
            if page is None or len(js.text) > len(page.text):
                js.rendered, page = True, js
        except (web.FetchError, web.UnsafeAddress):
            if page is None:
                raise first_error from None   # say why the page really failed
    if len(page.text) > max_chars:
        page.text, page.truncated = page.text[:max_chars], True
    return page
