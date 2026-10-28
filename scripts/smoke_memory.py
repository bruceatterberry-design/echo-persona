"""Real-model smoke test: memory survives restart + identity. Uses a throwaway DB."""
import sys, tempfile, time
from pathlib import Path
from fastapi.testclient import TestClient
from core.config import load_config
from core.server import create_app
from memory.store import MemoryStore

db = Path(tempfile.mkdtemp()) / "smoke.sqlite"
cfg = load_config()

def turn(ws, text):
    ws.send_json({"text": text}); t = time.perf_counter()
    while True:
        m = ws.receive_json()
        if m["type"] in ("reply", "error"):
            print(f"[{time.perf_counter()-t:.1f}s] {text!r}\n   -> {m.get('text', m.get('message'))}  ({m.get('expression')})")
            return m

with TestClient(create_app(cfg, store=MemoryStore(db))).websocket_connect("/ws") as ws:
    ws.receive_json()
    turn(ws, "Who are you?")
    turn(ws, "Remember that my favorite food is my mom's chicken and dumplings")
print("--- restart ---")
with TestClient(create_app(cfg, store=MemoryStore(db))).websocket_connect("/ws") as ws:
    ws.receive_json()
    ws.send_json  # noqa
    from memory.store import MemoryStore as M; M(db).clear_turns()  # recall must come from durable memory, not chat log
    r = turn(ws, "What's my favorite food?")
    ok = "dumpling" in r.get("text", "").lower()
    print("RECALL:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
