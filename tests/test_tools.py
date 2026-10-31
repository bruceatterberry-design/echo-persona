"""Echo's tool loop (core/tools.py): real tool results go back to the model; only addresses the owner gave are opened."""
import asyncio

import pytest

from core import tools, web


@pytest.fixture(autouse=True)
def no_audit(monkeypatch):
    monkeypatch.setattr(tools, "AUDIT", None)


class ScriptedModel:
    """Fake model: each round yields the next scripted list of events and records what it was sent."""

    def __init__(self, rounds):
        self.rounds, self.sent, self.offered = list(rounds), [], []

    async def chat_stream_tools(self, messages, offer):
        self.sent.append([dict(m) for m in messages])
        self.offered.append(offer)
        for ev in self.rounds.pop(0):
            yield ev


def call(name, **args):
    return {"tool_calls": [{"function": {"name": name, "arguments": args}}]}


def run(model, turn, text="hi"):
    async def go():
        return "".join([p async for p in tools.stream_with_tools(model, [{"role": "user", "content": text}], turn)])
    return asyncio.run(go())


@pytest.fixture
def fake_page(monkeypatch):
    pages = {}

    def read_page(url, max_chars=20000):
        u = web.normalise(url)
        u = u if u in pages else u + "/"
        if u not in pages:
            raise web.FetchError("the site answered with error 404")
        title, text, links = pages[u]
        return web.Page(url=u, title=title, text=text[:max_chars], links=[web.Link(t, h) for t, h in links])
    monkeypatch.setattr(web, "read_page", read_page)
    return pages


def test_urls_in_and_wants_tools():
    assert tools.urls_in("look at https://a.org/x?y=1, and nps.gov/yell. mail bob@site.com") == \
        ["https://a.org/x?y=1", "nps.gov/yell"]
    assert tools.wants_tools("What does example.com say?") and tools.wants_tools("can you look it up online")
    assert not tools.wants_tools("How was your day, sweetheart?") and not tools.wants_tools("I love you")


def test_permission_is_loose_on_spelling_but_strict_on_address():
    t = tools.Turn()
    t.allow("please read https://www.Example.org/a/ thanks")
    assert t.permitted("example.org/a") and t.permitted("http://example.org/a#top")
    assert not t.permitted("https://example.org/b") and not t.permitted("https://evil.example/a")


def test_tool_result_reaches_the_model(fake_page):
    fake_page["https://example.org/crappie"] = ("Crappie", "Crappie spawn when water hits 60 degrees.", [])
    model = ScriptedModel([[call("read_webpage", url="https://example.org/crappie")],
                           [{"content": "[smiling] They spawn at about 60 degrees, says example.org."}]])
    turn = tools.Turn(); turn.allow("what does https://example.org/crappie say")
    out = run(model, turn)
    assert out == "[smiling] They spawn at about 60 degrees, says example.org."
    tool_msg = model.sent[1][-1]
    assert tool_msg["role"] == "tool" and "60 degrees" in tool_msg["content"] and "Untrusted" in tool_msg["content"]
    assert model.sent[1][-2]["role"] == "assistant" and model.sent[1][-2]["tool_calls"]
    assert turn.sources == [{"title": "Crappie", "url": "https://example.org/crappie"}]
    assert turn.calls[0]["ok"] and turn.calls[0]["host"] == "example.org"


def test_address_not_given_by_owner_is_refused(fake_page):
    fake_page["https://evil.example/steal"] = ("x", "secret", [])
    model = ScriptedModel([[call("read_webpage", url="https://evil.example/steal")], [{"content": "[neutral] I need to ask."}]])
    turn = tools.Turn(); turn.allow("read https://example.org/")
    run(model, turn)
    assert model.sent[1][-1]["content"].startswith("NOT OPENED") and not turn.sources and not turn.calls[0]["ok"]


def test_failed_fetch_is_reported_not_invented(fake_page):
    model = ScriptedModel([[call("read_webpage", url="https://example.org/missing")], [{"content": "[sad] It wouldn't open."}]])
    turn = tools.Turn(); turn.allow("https://example.org/missing")
    run(model, turn)
    assert "COULDN'T READ THE PAGE" in model.sent[1][-1]["content"] and "404" in model.sent[1][-1]["content"]


def test_rounds_are_limited_and_last_round_offers_no_tools(fake_page):
    fake_page["https://example.org/"] = ("E", "text", [])
    loop = [call("read_webpage", url="https://example.org/")] 
    model = ScriptedModel([loop, loop, loop, [{"content": "[neutral] Done."}]])
    turn = tools.Turn(); turn.allow("https://example.org/")
    assert run(model, turn) == "[neutral] Done."
    assert model.offered[-1] is None and all(model.offered[:-1])


def test_second_expression_tag_is_dropped(fake_page):
    fake_page["https://example.org/"] = ("E", "text", [])
    model = ScriptedModel([[{"content": "[thinking] Let me look. "}, call("read_webpage", url="https://example.org/")],
                           [{"content": "[smil"}, {"content": "ing] It says hello."}]])
    turn = tools.Turn(); turn.allow("https://example.org/")
    assert run(model, turn) == "[thinking] Let me look. It says hello."


def test_unknown_tool_and_bad_arguments(fake_page):
    model = ScriptedModel([[{"tool_calls": [{"function": {"name": "delete_files", "arguments": "{}"}},
                                            {"function": {"name": "read_webpage", "arguments": "not json"}}]}],
                           [{"content": "[neutral] ok"}]])
    run(model, tools.Turn())
    assert "UNKNOWN TOOL" in model.sent[1][-2]["content"] and "NO ADDRESS" in model.sent[1][-1]["content"]


def test_page_links(fake_page):
    fake_page["https://example.org/"] = ("E", "", [("Maps", "https://example.org/maps"), ("Jigs", "https://example.org/jigs")])
    model = ScriptedModel([[call("page_links", url="example.org")], [{"content": "[neutral] two links"}]])
    turn = tools.Turn(); turn.allow("example.org")
    run(model, turn)
    assert "- Maps: https://example.org/maps" in model.sent[1][-1]["content"]


def test_focus_keeps_opening_and_matching_paragraphs():
    paras = ["Intro paragraph about the lake."] + [f"Filler paragraph number {i} about nothing much." for i in range(60)] + \
            ["Crappie spawning depth is four feet."]
    text, cut = tools.focus("\n\n".join(paras), "how deep do crappie spawn", 300)
    assert cut and text.startswith("Intro") and "four feet" in text and len(text) <= 300


def test_turn_budget_limits_total_page_text(fake_page):
    fake_page["https://example.org/a"] = ("A", "word " * 5000, [])
    model = ScriptedModel([[call("read_webpage", url="https://example.org/a")], [call("read_webpage", url="https://example.org/a")],
                           [call("read_webpage", url="https://example.org/a")], [{"content": "[neutral] ok"}]])
    turn = tools.Turn(); turn.allow("https://example.org/a")
    run(model, turn)
    assert turn.used_chars <= tools.TURN_CHARS and "no room left" in model.sent[3][-1]["content"]


def test_prefetch_reads_owners_address_before_the_model(fake_page):
    fake_page["https://example.org/a"] = ("A", "Alpha text.", [])
    q = "What does https://example.org/a say?"
    model = ScriptedModel([[{"content": "[smiling] It says alpha."}]])
    turn = tools.Turn(); turn.allow(q)

    async def go():
        msgs = [{"role": "user", "content": q}]
        return "".join([p async for p in tools.stream_with_tools(model, msgs, turn, prefetch=tools.prefetch_calls(q))])
    assert asyncio.run(go()) == "[smiling] It says alpha."
    sent = model.sent[0]                      # the model's FIRST look already has the real page
    assert [m["role"] for m in sent] == ["user", "assistant", "tool"]
    assert sent[1]["tool_calls"][0]["function"]["name"] == "read_webpage" and "Alpha text." in sent[2]["content"]
    assert turn.sources[0]["url"] == "https://example.org/a"


def test_prefetch_calls_choose_tool():
    c = tools.prefetch_calls("what links are on example.org about boats")[0]["function"]
    assert c["name"] == "page_links" and c["arguments"] == {"url": "example.org", "about": "what links are on about boats"}
    assert tools.prefetch_calls("research crappie at a.org")[0]["function"]["name"] == "page_links"
    c = tools.prefetch_calls("read https://a.org and https://b.org and https://c.org, also https://a.org")
    assert [x["function"]["arguments"]["url"] for x in c] == ["https://a.org", "https://b.org"]
    assert tools.prefetch_calls("how are you") == []


# ---------- step 3: research, following links, permissions ----------
def test_wants_research():
    assert tools.wants_research("research crappie on https://a.org") and tools.wants_research("follow the links please")
    assert tools.wants_research("dig deeper into it") and not tools.wants_research("what does a.org say")


def research_site(fake_page):
    fake_page["https://a.org/"] = ("Home", "Fishing home page.", [("Spawning", "https://a.org/spawn"),
                                                                 ("Partner", "https://b.org/x")])
    fake_page["https://a.org/spawn"] = ("Spawn", "They spawn at 60 degrees.", [])
    fake_page["https://b.org/x"] = ("B", "Other site text.", [])
    fake_page["https://evil.example/leak?d=secret"] = ("E", "x", [])


def test_links_followed_only_when_research_asked(fake_page):
    research_site(fake_page)
    for research, expect in ((False, "NOT OPENED"), (True, "<<WEB PAGE: Spawn")):
        model = ScriptedModel([[call("read_webpage", url="https://a.org/")], [call("read_webpage", url="https://a.org/spawn")],
                               [{"content": "[neutral] ok"}]])
        turn = tools.Turn(research=research); turn.allow("https://a.org/")
        run(model, turn)
        assert model.sent[2][-1]["content"].startswith(expect)
    assert [c["why"] for c in turn.calls] == ["given", "link"]
    assert [s["url"] for s in turn.sources] == ["https://a.org/", "https://a.org/spawn"]


def test_invented_address_never_allowed_even_in_research(fake_page):
    research_site(fake_page)
    model = ScriptedModel([[call("read_webpage", url="https://a.org/")], [call("read_webpage", url="https://evil.example/leak?d=secret")],
                           [{"content": "[neutral] ok"}]])
    turn = tools.Turn(research=True); turn.allow("https://a.org/")
    run(model, turn)
    assert model.sent[2][-1]["content"].startswith("NOT OPENED") and turn.calls[1]["outcome"] == "refused"


def test_same_site_setting_and_page_limit(fake_page):
    research_site(fake_page)
    turn = tools.Turn(research=True, follow="same_site"); turn.allow("https://a.org/")
    model = ScriptedModel([[call("page_links", url="https://a.org/")], [call("read_webpage", url="https://b.org/x")],
                           [{"content": "[neutral] ok"}]])
    run(model, turn)
    assert "another site" in model.sent[2][-1]["content"]
    turn = tools.Turn(research=True, max_pages=1); turn.allow("https://a.org/")
    model = ScriptedModel([[call("read_webpage", url="https://a.org/")], [call("read_webpage", url="https://a.org/spawn")],
                           [{"content": "[neutral] ok"}]])
    run(model, turn)
    assert "1-page limit" in model.sent[2][-1]["content"]


def test_never_setting_blocks_links(fake_page):
    research_site(fake_page)
    turn = tools.Turn(research=True, follow="never"); turn.allow("https://a.org/")
    turn.saw_links("https://a.org/", [web.Link("s", "https://a.org/spawn")])
    assert turn.why_allowed("https://a.org/spawn") is None and turn.why_allowed("https://a.org/") == "given"


def test_page_links_ranked_by_topic(fake_page):
    fake_page["https://a.org/"] = ("H", "", [("Boats", "https://a.org/boats"), ("Crappie spawning", "https://a.org/spawn")])
    model = ScriptedModel([[call("page_links", url="https://a.org/", about="crappie spawning")], [{"content": "[neutral] ok"}]])
    turn = tools.Turn(); turn.allow("https://a.org/")
    run(model, turn)
    body = model.sent[1][-1]["content"]
    assert body.index("Crappie spawning") < body.index("Boats")


def test_audit_log_records_without_content(tmp_path, fake_page, monkeypatch):
    from core import permissions
    audit = permissions.Audit(tmp_path / "a.sqlite")
    monkeypatch.setattr(tools, "AUDIT", audit)
    fake_page["https://a.org/"] = ("Home", "Private-looking page text.", [])
    model = ScriptedModel([[call("read_webpage", url="https://a.org/", question="secret question")],
                           [call("read_webpage", url="https://nope.org/")], [{"content": "[neutral] ok"}]])
    turn = tools.Turn(); turn.allow("https://a.org/")
    run(model, turn)
    rows = audit.recent()
    assert [(r["tool"], r["host"], r["why"], r["outcome"], r["risk"]) for r in rows] == [
        ("read_webpage", "nope.org", "-", "refused", "low"), ("read_webpage", "a.org", "given", "ok", "low")]
    dump = repr(audit.db.execute("SELECT * FROM tool_audit").fetchall())
    assert "Private-looking" not in dump and "secret question" not in dump


def test_settings_file_is_validated(tmp_path):
    from core import permissions
    p = tmp_path / "tools.json"
    assert permissions.load_settings(p) == permissions.DEFAULTS
    p.write_text('{"follow_links": "everywhere", "max_pages": 99, "extra": 1}')
    s = permissions.load_settings(p)
    assert s["follow_links"] == "same_site" and s["max_pages"] == 8 and "extra" not in s
