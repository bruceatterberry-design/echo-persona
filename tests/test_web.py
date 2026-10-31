"""Safe page reader (core/web.py): address checks, redirects, size/type limits, readable text."""
import httpx
import pytest

from core import web

PUBLIC = "93.184.215.14"


@pytest.fixture
def net(monkeypatch):
    """Fake DNS + fake internet. routes: url -> httpx.Response or callable."""
    dns = {"example.org": [PUBLIC], "evil.example": [PUBLIC], "inside.example": ["192.168.1.20"],
           "tail.example": ["100.101.102.103"], "mixed.example": [PUBLIC, "127.0.0.1"], "v6local.example": ["::1"],
           "mapped.example": ["::ffff:10.0.0.1"]}
    routes: dict[str, httpx.Response] = {}
    seen: list[str] = []

    def resolve(host):
        if host not in dns:
            import socket
            raise socket.gaierror("no such host")
        return dns[host]

    def handler(req: httpx.Request):
        seen.append(str(req.url))
        r = routes.get(str(req.url))
        return r if r is not None else httpx.Response(404)

    monkeypatch.setattr(web, "_resolve", resolve)
    monkeypatch.setattr(web, "TRANSPORT", httpx.MockTransport(handler))
    return routes, seen


def html(body, title="T"):
    return httpx.Response(200, text=f"<html><head><title>{title}</title></head><body>{body}</body></html>",
                          headers={"content-type": "text/html; charset=utf-8"})


@pytest.mark.parametrize("url", [
    "http://localhost:8765/health", "http://127.0.0.1:8765/", "http://[::1]/", "http://0.0.0.0/",
    "http://169.254.169.254/latest/meta-data", "http://192.168.1.1/", "http://10.0.0.5/", "http://100.64.0.1/",
    "http://inside.example/", "http://tail.example/", "http://mixed.example/", "http://v6local.example/",
    "http://mapped.example/", "http://echo.local/", "http://mac.tail1234.ts.net/",
    "file:///etc/passwd", "ftp://example.org/x", "javascript:alert(1)", "https://user:pw@example.org/"])
def test_refuses_private_and_odd_addresses(net, url):
    with pytest.raises(web.UnsafeAddress):
        web.fetch(url)


def test_redirect_into_home_network_is_blocked(net):
    routes, seen = net
    routes["https://evil.example/"] = httpx.Response(302, headers={"location": "http://127.0.0.1:8765/api/memories"})
    with pytest.raises(web.UnsafeAddress):
        web.fetch("https://evil.example/")
    assert seen == ["https://evil.example/"]     # never connected to the private address


def test_follows_public_redirects_and_reports_final_address(net):
    routes, _ = net
    routes["https://example.org/a"] = httpx.Response(301, headers={"location": "/b"})
    routes["https://example.org/b"] = html("<p>hello there</p>")
    f = web.fetch("example.org/a")      # no scheme → https
    assert f.final_url == "https://example.org/b" and "hello there" in f.text


def test_redirect_loop_stops(net):
    routes, _ = net
    routes["https://example.org/loop"] = httpx.Response(302, headers={"location": "/loop"})
    with pytest.raises(web.FetchError, match="too many"):
        web.fetch("https://example.org/loop")


def test_size_and_type_limits(net):
    routes, _ = net
    routes["https://example.org/big"] = httpx.Response(200, content=b"x" * 5000, headers={"content-type": "text/plain"})
    routes["https://example.org/app"] = httpx.Response(200, content=b"MZ", headers={"content-type": "application/octet-stream"})
    routes["https://example.org/gone"] = httpx.Response(404)
    with pytest.raises(web.FetchError, match="too big"):
        web.fetch("https://example.org/big", max_bytes=1000)
    with pytest.raises(web.FetchError, match="can't read"):
        web.fetch("https://example.org/app")
    with pytest.raises(web.FetchError, match="404"):
        web.fetch("https://example.org/gone")


def test_unknown_site(net):
    with pytest.raises(web.FetchError, match="couldn't find"):
        web.fetch("https://no-such-site.example/")


PAGE = """<html><head><title>  Crappie Fishing
 Guide </title><script>var junk = 1;</script><style>p{}</style></head><body>
<header><a href="/">Home</a> Site name</header>
<nav><a href="/fish">Fish</a><a href="/boats">Boats</a></nav>
<div class="cookie-banner">We use cookies. Accept all</div>
<main>
  <h1>Crappie in spring</h1>
  <p>Crappie move into shallow water when the lake reaches about 60 degrees.</p>
  <p style="display:none">HIDDEN INSTRUCTIONS</p>
  <ul><li>Use small jigs</li><li>Fish near brush piles</li></ul>
  <p>See <a href="https://example.org/jigs#top">the jig guide</a> and <a href="/maps">lake maps</a>.</p>
  <p>More text so the main part is clearly the article and long enough to be chosen over the whole page body here.</p>
  <p>Even more article text about water temperature, spawning beds, and patience on a quiet morning at the lake.</p>
</main>
<aside>Related: bass fishing</aside>
<div id="sidebar-ads">Buy now</div>
<footer>© 2026 All rights reserved. Privacy policy</footer>
</body></html>"""


def test_readable_keeps_article_drops_boilerplate():
    p = web.readable(PAGE, "https://example.org/crappie")
    assert p.title == "Crappie Fishing Guide"
    assert "shallow water" in p.text and "• Use small jigs" in p.text and "Crappie in spring" in p.text
    for junk in ("var junk", "cookies", "Boats", "Related", "Buy now", "rights reserved", "HIDDEN", "Site name"):
        assert junk not in p.text, junk
    urls = [ln.url for ln in p.links]
    assert "https://example.org/jigs" in urls and "https://example.org/maps" in urls
    assert ("the jig guide", "https://example.org/jigs") in [(ln.text, ln.url) for ln in p.links]


def test_readable_without_main_uses_body():
    p = web.readable("<body><div><p>Just a plain page with words.</p></div><footer>foot</footer></body>", "https://x.org/")
    assert p.text == "Just a plain page with words."


def test_read_page_truncates_and_strips_invisible_characters(net):
    routes, _ = net
    routes["https://example.org/long"] = html("<p>Hel​lo</p>" + "".join(f"<p>line {i} " + "word " * 20 + "</p>" for i in range(300)))
    p = web.read_page("https://example.org/long", max_chars=2000)
    assert p.truncated and len(p.text) <= 2000 and p.text.startswith("Hello") and p.url == "https://example.org/long"


def test_page_summary_kept_when_not_in_text():
    p = web.readable('<head><meta name="description" content="The main element holds the dominant content."></head>'
                     '<body><main><p>Example code only.</p></main></body>', "https://x.org/")
    assert p.description.startswith("The main element") and p.text.startswith("The main element")
    q = web.readable('<head><meta name="description" content="Plain words here"></head><body><p>Plain words here and more.</p></body>')
    assert q.text == "Plain words here and more."
