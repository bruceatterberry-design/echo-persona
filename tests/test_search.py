"""Web search (core/search.py + the web_search tool): ads skipped, real addresses, results openable this message only."""
import asyncio

from core import search, tools, web

PAGE = """<html><body>
<div class="result results_links results_links_deep result--ad"><div class="links_main result__body">
 <h2 class="result__title"><a class="result__a" href="https://duckduckgo.com/y.js?ad_provider=x">Buy Steely Dan tickets</a></h2>
 <a class="result__snippet" href="#">Cheap tickets now</a></div></div>
<div class="result results_links results_links_deep web-result "><div class="links_main links_deep result__body">
 <h2 class="result__title"><a rel="nofollow" class="result__a"
   href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FSteely_Dan&amp;rut=abc">Steely Dan - <b>Wikipedia</b></a></h2>
 <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">Steely Dan is an American rock band <b>formed</b> in 1971 in New York.</a>
</div></div>
<div class="result results_links web-result"><div class="links_main result__body">
 <h2 class="result__title"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.steelydan.com%2F&amp;rut=d">Steely Dan Official</a></h2>
 <a class="result__snippet">Official site.</a></div></div>
<div class="result web-result"><h2 class="result__title"><a class="result__a" href="javascript:alert(1)">Bad</a></h2></div>
</body></html>"""


def test_parse_skips_ads_and_unwraps_addresses():
    hits = search.parse(PAGE)
    assert [(h.title, h.url) for h in hits] == [("Steely Dan - Wikipedia", "https://en.wikipedia.org/wiki/Steely_Dan"),
                                                ("Steely Dan Official", "https://www.steelydan.com/")]
    assert hits[0].snippet.startswith("Steely Dan is an American rock band formed in 1971")


def test_search_words():
    assert tools.wants_search("Can you look up when Steely Dan was formed?")
    assert tools.search_query("Can you look up when Steely Dan was formed?") == "when Steely Dan was formed"
    assert not tools.wants_search("How was your day?") and not tools.wants_search("look up https://example.com")
    c = tools.prefetch_calls("search the web for the height of Mount Rainier")
    assert c == [{"function": {"name": "web_search", "arguments": {"query": "the height of Mount Rainier"}}}]


class Model:
    def __init__(self, rounds):
        self.rounds, self.sent = list(rounds), []

    async def chat_stream_tools(self, messages, offer):
        self.sent.append(list(messages))
        for ev in self.rounds.pop(0):
            yield ev


def test_search_then_read_a_result(monkeypatch):
    monkeypatch.setattr(tools, "AUDIT", None)
    monkeypatch.setattr(search, "search", lambda q, limit=6: search.parse(PAGE))
    monkeypatch.setattr(web, "read_page", lambda url, max_chars=0: web.Page(url=url, title="Steely Dan",
                                                                           text="Formed in 1971 by Becker and Fagen."))
    q = "look up when Steely Dan was formed"
    model = Model([[{"tool_calls": [{"function": {"name": "read_webpage", "arguments": {"url": "https://evil.example/x"}}}]}],
                   [{"content": "[smiling] 1971, says Wikipedia."}]])
    turn = tools.Turn(); turn.allow(q)

    async def go():
        msgs = [{"role": "user", "content": q}]
        return "".join([p async for p in tools.stream_with_tools(model, msgs, turn, prefetch=tools.prefetch_calls(q))])
    assert asyncio.run(go()) == "[smiling] 1971, says Wikipedia."
    assert "Search results for: when Steely Dan was formed" in model.sent[0][-3]["content"]
    assert "Formed in 1971" in model.sent[0][-1]["content"]               # the top result is read before he starts
    assert model.sent[1][-1]["content"].startswith("NOT OPENED")          # any other address still may not
    assert [c["name"] for c in turn.calls] == ["web_search", "read_webpage", "read_webpage"]
    assert turn.sources == [{"title": "Steely Dan", "url": "https://en.wikipedia.org/wiki/Steely_Dan"}]
