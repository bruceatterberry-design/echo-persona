"""The Claude helper must be off by default and fall back to local on any failure."""
import json
import os
import stat

from core.claude_helper import DEFAULT, ClaudeHelper


def fake_claude(tmp_path, body: str):
    b = tmp_path / "claude"
    b.write_text("#!/bin/sh\ncat > /dev/null\n" + body)
    b.chmod(b.stat().st_mode | stat.S_IEXEC)
    return b


def helper(tmp_path, cfg: dict | None, binary=None, monkeypatch=None):
    c = tmp_path / "claude.json"
    if cfg is not None:
        c.write_text(json.dumps(cfg))
    h = ClaudeHelper(c, tmp_path / "usage.json")
    if binary:
        h.binary = lambda: str(binary)
    return h


ON = DEFAULT | {"enabled": True, "jobs": {"personality": True, "condense": True, "summaries": False}}


def test_off_by_default(tmp_path):
    h = helper(tmp_path, None, fake_claude(tmp_path, 'echo \'{"result":"x"}\''))
    assert h.config()["enabled"] is False and h.ask("personality", "hi") is None and h.stats["calls"] == 0


def test_job_must_be_switched_on(tmp_path):
    h = helper(tmp_path, ON, fake_claude(tmp_path, 'echo \'{"result":"x"}\''))
    assert h.ask("summaries", "hi") is None and h.ask("unknown", "hi") is None


def test_structured_answer(tmp_path):
    h = helper(tmp_path, ON, fake_claude(tmp_path, 'echo \'{"is_error":false,"structured_output":{"traits":[]}}\''))
    assert h.ask("personality", "hi", {"type": "object"}) == {"traits": []}
    assert h.used_today() == 1 and h.stats["ok"] == 1


def test_failures_fall_back(tmp_path):
    for body in ['exit 1', 'echo not-json', 'echo \'{"is_error":true,"result":"limit"}\'', 'sleep 5']:
        h = helper(tmp_path, ON | {"timeout_s": 1}, fake_claude(tmp_path, body))
        assert h.ask("personality", "hi", {"type": "object"}) is None
        assert h.stats["failed"] == 1 and h.stats["last_error"]


def test_daily_limit(tmp_path):
    h = helper(tmp_path, ON | {"daily_limit": 2}, fake_claude(tmp_path, 'echo \'{"result":"ok"}\''))
    assert [h.ask("condense", "hi") for _ in range(3)] == ["ok", "ok", None]


def test_missing_binary(tmp_path):
    h = helper(tmp_path, ON)
    h.binary = lambda: None
    assert h.ask("personality", "hi") is None and h.status()["installed"] is False


def test_personality_uses_helper_then_local(tmp_path, monkeypatch):
    from personality import build as persona

    class H:
        def __init__(self, answer): self.answer, self.jobs = answer, []
        def ask(self, job, prompt, schema=None): self.jobs.append(job); return self.answer

    got = {"traits": [], "facts": []}
    assert persona.ask_llm("http://x", "m", {"text": "hello there"}, H(got)) == got
    called = {}
    class R:
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": '{"traits": [], "facts": [1]}'}}
    monkeypatch.setattr(persona.httpx, "post", lambda *a, **k: called.setdefault("local", R()))
    assert persona.ask_llm("http://x", "m", {"text": "hello there"}, H(None)) == {"traits": [], "facts": [1]}
    assert "local" in called


def test_summary_falls_back_locally(monkeypatch):
    from memory import summarize as s

    class H:
        def ask(self, job, prompt, schema=None): assert job == "summaries"; return None
    class R:
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": "On Monday we talked."}}
    monkeypatch.setattr(s.httpx, "post", lambda *a, **k: R())
    assert s.summarize("h", "m", [{"ts": 0, "role": "user", "content": "hi"}], H()) == "On Monday we talked."


def test_connection_test_needs_only_enabled(tmp_path):
    h = helper(tmp_path, DEFAULT | {"enabled": True}, fake_claude(tmp_path, 'echo \'{"result":"OK"}\''))
    assert h.test()["ok"] is True and h.ask("personality", "x") is None
