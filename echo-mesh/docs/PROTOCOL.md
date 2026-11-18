# Protocol (Phase 1, as built)

Transport: HTTPS (TLS) from node to coordinator. Nodes connect OUT only; no inbound port on a node.
Coordinator ports: `47821` node API (TLS), `47830` admin API (127.0.0.1 only, plain HTTP), `47820/udp` discovery.

## Identity and authentication
- Coordinator has a self-signed certificate. A node pins its SHA-256 fingerprint at enrollment and refuses any other later.
- Enrollment uses a one-time 4-digit pairing code (5 min life, burned after 5 wrong guesses across all live codes).
  The code is a human confirmation only. The node then holds a unique 256-bit device secret.
- Every later request is signed: `HMAC-SHA256(secret, METHOD \n path \n ts \n nonce \n sha256(body))`, headers
  `X-Mesh-Node / -Ts / -Nonce / -Sig`. Clock skew limit 60 s; reused nonces are refused (replay).
- Statuses: `pending` (awaiting approval), `approved`, `paused`, `revoked`. Only `approved` may heartbeat or take work.
- Node type (`primary`, `brain`, `compute`) is fixed by the pairing code the owner creates. It never changes automatically.

## Node API (TLS)
| Request | Meaning |
|---|---|
| `POST /enroll` (unsigned) `{code,name,inventory}` -> `{node_id,secret,status,node_type}` | Join with a pairing code |
| `POST /heartbeat` `{load, inventory?}` | Alive + resource report |
| `GET /jobs/next?wait=N` -> job or 204 | Long-poll for work the node is able to run |
| `POST /jobs/{id}/result` `{ok,result,error}` | Return result. 409 if the job is no longer this node's |

## Jobs
Only `ping` and `generate` (Ollama `/api/generate`: model, prompt, think). Nothing else is accepted by the coordinator or run by the
agent. No shell, no file access. A job may require a model that the node reported having.
Recovery: a job held by a node silent for 30 s, or running over 10 min, goes back to the queue (max 3 tries, then `failed`).

## Admin API (127.0.0.1)
`GET /status /jobs /jobs/{id} /audit`, `POST /pairing /approve /revoke /pause /resume /jobs`.

## Known limits (Phase 1)
- First contact trusts the LAN for a short window: someone able to relay the very first enrollment could capture the device
  secret. Mitigation planned: show the fingerprint on both screens for the owner to compare.
- Device secrets are HMAC keys (not ed25519), stored 0600 on the node and in the coordinator database. No `cryptography`
  dependency was added; stdlib has no ed25519.
- Not built yet: share-limit enforcement, quotas/ledger, brain-release distribution, message center, the dashboard.
