"""Stage 6a: repeated procedures → skill candidates (report only)."""
from fastapi.testclient import TestClient

from core import skills
from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore


def rec(kind, tools=(), calls=1, route=None, first=900, total=3000):
    return {"route": route or ("llm_tools" if tools else "llm"), "plan": {"kind": kind}, "tools": list(tools),
            "llm_calls": calls, "first_ms": first, "total_ms": total}


def test_candidates():
    rs = ([rec("research", ["web_search:ok", "read_webpage:ok"], calls=2) for _ in range(5)] +
          [rec("research", ["read_webpage:ok"]) for _ in range(4)] +                       # too few
          [rec("research", ["web_search:no"], calls=2) for _ in range(6)] +               # fails too often
          [rec("chat") for _ in range(20)] + [{"route": "browser"}] * 7)                  # not procedures / no tools
    r = skills.report(rs)
    assert [c["procedure"] for c in r["candidates"]] == ["research: web_search → read_webpage"]
    c = r["candidates"][0]
    assert c["count"] == 5 and c["success"] == "5/5" and c["could_shortcut"] and c["llm_calls_mean"] == 2
    chat = next(p for p in r["procedures"] if p["procedure"] == "chat: (no tools)")
    assert chat["count"] == 20 and not chat["candidate"] and not chat["could_shortcut"]
    assert all(p["procedure"] != "browser" for p in r["procedures"])


def test_errors_count_as_failures():
    assert not skills.ok(rec("chat", route="llm_error")) and skills.signature({"route": "music"}) is None


def test_endpoint(tmp_path):
    client = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"text": "hello"})
        while not ((m := ws.receive_json())["type"] == "state" and m["state"] == "idle"):
            pass
    r = client.get("/api/skills").json()
    assert r["turns"] == 1 and r["procedures"][0]["procedure"] == "chat: (no tools)" and r["candidates"] == []
