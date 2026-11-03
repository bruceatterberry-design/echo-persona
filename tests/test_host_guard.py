"""Stage 8: DNS-rebinding guard — only 127.0.0.1 / localhost (and the test client) are answered."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core.config import PersonaConfig
from core.llm import EchoBackend
from core.server import create_app
from memory.store import MemoryStore


def test_only_local_host_names(tmp_path):
    c = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    for host in ("127.0.0.1:8765", "localhost:8765", "testserver"):
        assert c.get("/health", headers={"Host": host}).status_code == 200
    for host in ("evil.example", "evil.example:8765", "192.168.1.20:8765", "my-mac.tailnet.ts.net"):
        assert c.get("/api/memories", headers={"Host": host}).status_code == 400
        assert c.post("/api/learning/objectives/1", json={"decision": "dismiss"}, headers={"Host": host}).status_code == 400


def test_websocket_refused_for_other_hosts(tmp_path):
    c = TestClient(create_app(PersonaConfig(), EchoBackend(), MemoryStore(tmp_path / "m.sqlite")))
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers={"Host": "evil.example"}) as ws:
            ws.receive_json()
    with c.websocket_connect("/ws", headers={"Host": "127.0.0.1:8765"}) as ws:
        assert ws.receive_json()["type"] == "history"
