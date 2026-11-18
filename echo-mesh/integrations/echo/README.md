# Echo client for the Mesh

`meshoffload.py` lets a program hand one small text request to a computer on the Mesh and get the answer back, or `None`
(so the caller does it the old way). It is what Echo's maths-and-logic check uses.

```python
from meshoffload import offload
reply = offload.generate("lean_formalize", "…the prompt…")   # None if the Mesh is off, slow, down or has no such model
if reply is None:
    ...  # do it locally, exactly as before
```

- **Config** `config/meshoffload.json`: `enabled`, `models` per task (first one an online computer has), `max_wait_s`
  (default 12), `max_concurrent` (default 1: a second request goes straight to your local model), `block_terms`.
- **Token** `~/.echo-mesh/client.token`: the Mesh coordinator's *limited* token (it can send text jobs and read its own answers; nothing else).
- **Private text**: `offload.private(text)` says why a text must stay home (names from `config/entities.json`, school and
  student words, emails, phone numbers, street addresses). Call it before every request.
- Off switch: `"enabled": false`, or `SWEETIE_MESHOFFLOAD=0`.
