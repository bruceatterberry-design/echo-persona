# JARVIS → Echo: repository analysis & compatibility (Phases 1–2)

Source inspected: github.com/bertrandmbanwi/Jarvis @ d17d396 (2026-09-30), **MIT licence** (compatible with Echo's MIT).
Inspected read-only in a scratch folder; nothing from it was installed or run. 253 files, Python (FastAPI) + Next.js UI.

## Phase 1: where each capability lives

| Capability | JARVIS files / functions | How it works |
|---|---|---|
| Plain page reading | `tools/web_browse.py`: `fetch_page_text`, `_fetch_html`, `_is_url_safe`, `_extract_text_bs4`, `fetch_page_links` | httpx + BeautifulSoup. Strips script/nav/footer/aside, prefers `<main>`/`<article>`, cuts to 5,000 chars. **SSRF guard**: blocks localhost, private/reserved IPs, cloud-metadata, and re-checks *every redirect hop* (redirects followed by hand). |
| Playwright browser | `tools/browser_agent.py`: `BrowserAgent`, `_launch_persistent_browser`, `navigate`, `_run_openai_loop`, `_run_anthropic_loop` | Visible persistent Chromium. The "agent" loop is **screenshot-based computer-use from OpenAI or Anthropic** (cloud only); there is no local-model loop. |
| Login carry-over | `tools/chrome_sync.py`, `browser_agent.sync_browser_sessions` | Copies your real Chrome cookies (pycookiecheat) into the Playwright browser. |
| Chrome extension | `extensions/chrome/*`, `tools/chrome_extension.py` | MV3 extension with `<all_urls>`, cookies, downloads, scripting; WebSocket to the server; click/type/read/find/screenshot in your real tabs. |
| Web search | `tools/web_search.py` | DuckDuckGo (`ddgs`). |
| Tool loop (local) | `core/providers/local_provider.py`: `run_tools`, `_run_call`, `to_chat_tool` | OpenAI-compatible tool calling against Ollama: call model → run tool calls → append `role:"tool"` results → repeat up to N rounds. Real results go back to the model. |
| Tool registry / schemas | `agent/tools_schema.py`, `agent/tool_selector.py`, `core/dispatch_registry.py` | JSON-schema tool list; selector trims the list per request so small models aren't flooded. |
| Deterministic routing | `core/local_router.py` | Regex routes for cheap common actions before any LLM. (Echo already does this: `core/browser.py`, wake/commands.) |
| Orchestration | `agent/planner.py`, `coordinator.py`, `executor.py`, `qa_agent.py` | Planner splits tasks into subtasks; mostly aimed at cloud models. |
| MCP | `core/mcp_client.py`, `jarvis/mcp_server.py` | Client for external MCP servers + exposes JARVIS as one. Needs `mcp` package. |
| Permissions | `core/permissions.py`, `core/confirmation.py`, `core/pending_actions.py`, `core/http_security.py` | Every tool has a Capability + RiskLevel; risky calls need confirmation; SQLite audit log with secrets redacted. |
| Feedback | `core/feedback.py` | JSON list of corrections (max 500). No 👍/👎 UI, no training loop. |
| Memory | `memory/sqlite_store.py`, `facts.py`, `preferences.py`, `conversation_store.py` (+ ChromaDB) | Regex fact extraction with confidence decay; topic/tool usage stats; ChromaDB vectors. |

## Phase 2: compatibility with Echo

| JARVIS piece | Verdict | Why |
|---|---|---|
| `web_browse` SSRF guard + redirect re-check | **Adapt (high value)** | Echo's `memory/learn.add_web` follows redirects with no address check: a page could redirect it to Echo's own localhost services. Fixes a real gap. ~60 lines, no new dependency (httpx already in Echo). |
| Main-content extraction | **Adapt** | Echo's `learn.html_text` uses the stdlib parser and keeps nav/footer. Same idea in the stdlib, so there's no new bs4 dependency. |
| `local_provider.run_tools` loop | **Adapt (core of Phase 4)** | Echo's LLM backend has no tool calling. The installed qwen3.5:9b reports the `tools` capability in Ollama 0.40.11, so the native `/api/chat` `tools` field works with no new package. |
| Tool permission catalogue + audit | **Adapt (small)** | Fits Echo's existing "ask before outside actions" rules; use Echo's own SQLite, not a new DB. |
| Playwright browser | **Later, optional** | Needed only for JavaScript-only pages. Chromium is ~150 MB of download + ~300–500 MB RAM per page on a 16 GB Mac that already runs LLM + voice + STT (swap was at 1.7/2 GB today). Needs the owner's OK. |
| Computer-use agent loop | **Don't use** | Cloud-only (OpenAI/Anthropic screenshots), which goes against "must not depend on Claude". A 9B local model can't drive screenshots reliably. |
| Chrome cookie sync | **Reject** | Reads all of the owner's/the loved one's browser logins into an automated browser; a malicious page could then act as them. |
| Chrome extension | **Reject for now** | `<all_urls>` + cookies + downloads permissions in the family Chrome profile ("the loved one"); huge prompt-injection surface. |
| MCP client/server | **Not now** | Adds `mcp` dependency for no measured benefit; a plain in-process registry is lighter. Can wrap later. |
| Planner / coordinator | **Duplicate / too heavy** | Built for cloud models; a simple bounded loop (max rounds) suits the 9B model better. |
| Memory (facts, ChromaDB) | **Don't import** | Duplicates and would conflict with Echo's memory (confirmed/inferred status, sources, vector index, backups). Useful *ideas* only: confidence decay over time, tool-usage stats. |
| Voice, UI, Telegram/iMessage, calendar | **Out of scope / duplicate** | Echo has its own. |

Dependencies JARVIS would add: openai, anthropic, mcp, chromadb, playwright, pycookiecheat, ddgs, kokoro, faster-whisper,
openwakeword… The plan above adds **none** for steps 1–3.

## Proposed build order (each step = one checkpoint, tests + measurement)
1. **Safe page reader** (`core/web.py`): SSRF-guarded fetch, readable-text extraction, links, size/type limits; Learn switches to it. Baseline benchmark on a fixed URL set.
2. **Tool loop** (`core/tools.py`): registry + bounded loop over Ollama native tools; first tools `read_webpage`, `page_links`. Pages are wrapped as untrusted data (prompt-injection guard). Citations carried through.
3. **Multi-page research + follow links when allowed** + citations in the reply/UI; permission + audit log.
4. **Feedback 👍/👎 + "Train Echo"** with Claude (see below).
5. Optional: web search, Playwright for JavaScript pages (ask first).
6. Phase 8 report: before/after graphs.

## "Train Echo": what's realistic
- "Improving the model" here means changing **Echo's prompts, rules, tools and code**, checked by re-running the logged input,
  not retraining the 9B weights (LoRA training on 16 GB is possible but slow and risky; can be a later option).
- On "Train Echo", the 👎 entries (the owner's input + Echo's reply + the owner's note) are sent to Claude. That data is personal
  conversation, so it goes **only** on that command, after "are you sure", and is shown before sending.
- Claude Code changes code only after the owner says yes; each change is a git commit, so it can be undone.
