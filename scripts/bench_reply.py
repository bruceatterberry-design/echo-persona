"""Measure time to Echo's first word over the real WebSocket (adds test turns to the conversation log)."""
import json, statistics, sys, time
from websockets.sync.client import connect
QS = ["How was your day?", "What do you think about the weather today?", "Tell me something that makes you happy.",
      "What should we have for dinner?", "Do you remember what I told you about my favorite food?"]
res = []
for q in QS[: int(sys.argv[1]) if len(sys.argv) > 1 else len(QS)]:
    with connect("ws://127.0.0.1:8765/ws") as ws:
        ws.recv(); t = time.perf_counter(); ws.send(json.dumps({"text": q})); first = None
        while True:
            m = json.loads(ws.recv())
            if m["type"] == "delta" and first is None: first = time.perf_counter() - t
            if m["type"] == "reply": break
    res.append(first); print(f"  first word {first:.2f}s  | {q}")
print(f"median first word: {statistics.median(res):.2f}s")
