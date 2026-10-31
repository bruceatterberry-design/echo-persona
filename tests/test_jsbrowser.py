"""Full browser fallback (core/jsbrowser.py): only when the plain reader finds almost nothing."""
import pytest

from core import jsbrowser, web


def page(text):
    return web.Page(url="https://a.org/", title="t", text=text)


def test_plain_page_never_starts_the_browser(monkeypatch):
    monkeypatch.setattr(web, "read_page", lambda url, max_chars=0: page("word " * 200))
    monkeypatch.setattr(jsbrowser, "render", lambda url: pytest.fail("browser started for a normal page"))
    p = jsbrowser.read_smart("https://a.org/", max_chars=50)
    assert not p.rendered and p.truncated and len(p.text) == 50


def test_empty_page_is_rendered(monkeypatch):
    monkeypatch.setattr(web, "read_page", lambda url, max_chars=0: page(""))
    monkeypatch.setattr(jsbrowser, "render", lambda url: ("<body><main><p>" + "Quote by Einstein. " * 30 +
                                                           "</p></main></body>", "https://a.org/js"))
    p = jsbrowser.read_smart("https://a.org/js")
    assert p.rendered and "Einstein" in p.text and p.url == "https://a.org/js"


def test_links_only_request_still_judges_the_whole_page(monkeypatch):
    monkeypatch.setattr(web, "read_page", lambda url, max_chars=0: page("word " * 200))
    monkeypatch.setattr(jsbrowser, "render", lambda url: pytest.fail("browser started just because max_chars=1"))
    assert len(jsbrowser.read_smart("https://a.org/", max_chars=1).text) == 1


def test_blocked_by_site_tries_browser_but_reports_original_reason(monkeypatch):
    def forbidden(url, max_chars=0):
        raise web.FetchError("the site answered with error 403")
    monkeypatch.setattr(web, "read_page", forbidden)
    with pytest.raises(web.FetchError, match="403"):          # browser fails too (conftest) → the real reason
        jsbrowser.read_smart("https://a.org/")
    monkeypatch.setattr(jsbrowser, "render", lambda url: ("<p>" + "Real content here. " * 30 + "</p>", url))
    assert jsbrowser.read_smart("https://a.org/").rendered       # a real browser got in


def test_no_such_site_skips_the_browser(monkeypatch):
    def missing(url, max_chars=0):
        raise web.FetchError("I couldn't find the site nope.example")
    monkeypatch.setattr(web, "read_page", missing)
    monkeypatch.setattr(jsbrowser, "render", lambda url: pytest.fail("no point"))
    with pytest.raises(web.FetchError, match="couldn't find"):
        jsbrowser.read_smart("https://nope.example/")


def test_needs_js():
    assert jsbrowser.needs_js(page("short"))
    assert jsbrowser.needs_js(page("x " * 400 + "Please enable JavaScript to continue"))
    assert not jsbrowser.needs_js(page("A normal article. " * 40))
