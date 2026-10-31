"""End-to-end check of Echo's tool loop with the REAL local model and REAL web pages (nothing saved to memory).

Run: .venv/bin/python scripts/bench_tools.py [label]   → docs/bench/tools-<label>.json
Each case: the owner's question → qwen decides → tools run → answer. Scored on: did he call the tool (or rightly not),
did the answer contain the expected facts, did he avoid asking the owner to open/paste the page, refused addresses he
wasn't given, time to first word, total time, prompt tokens (context use).
"""
import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import tools  # noqa: E402
from core.config import load_config, load_personality  # noqa: E402
from core.llm import OllamaBackend  # noqa: E402
from core.server import SYSTEM_EXTRA  # noqa: E402

MIN_PAGES = {"research: follow 1 link": 1, "research: wikipedia hop": 1, "research: author page": 1,
             "search: band formed": 1, "search: mountain": 1, "search: song year": 1, "js-only page": 1}  # linked pages really read
CASES = [  # (question, expect tool?, facts that must appear (any one of each group), label)
    ("What does https://example.com say?", True, [["example", "documentation", "illustrat"]], "simple page"),
    ("Can you read https://en.wikipedia.org/wiki/Crappie and tell me the scientific genus of crappie?", True,
     [["pomoxis"]], "fact in long page"),
    ("Read https://www.nps.gov/yell/index.htm and tell me which park it's about.", True, [["yellowstone"]], "gov page"),
    ("What is on www.loc.gov/about/ ?", True, [["library of congress", "library"]], "bare address"),
    ("Look at https://docs.python.org/3/library/json.html and tell me which function turns a Python object into a JSON string.",
     True, [["dumps"]], "docs question"),
    ("What links are on https://www.gutenberg.org/ebooks/1342 ?", True, [["epub", "kindle", "plain text", "read online"]], "links"),
    ("Please read https://this-site-does-not-exist-echo-test.com and summarise it.", True,
     [["couldn", "could not", "can't", "cannot", "unable", "wasn't able", "not able", "doesn't", "trouble"]], "dead site"),
    ("Research this for me: start at https://docs.python.org/3/library/index.html and follow the links to find what the "
     "json module's dumps function does.", True, [["dumps"], ["json"], ["string", "str", "serializ"]], "research: follow 1 link"),
    ("Start at https://en.wikipedia.org/wiki/Special_education and follow the links to the article about the Individuals "
     "with Disabilities Education Act. What year was it first passed?", True, [["1975", "1990"]], "research: wikipedia hop"),
    ("Dig into https://www.gutenberg.org/ebooks/1342 and follow the links to find other books by the same author.",
     True, [["emma", "sense and sensibility", "persuasion", "mansfield", "northanger"]], "research: author page"),
    ("Can you look up when Steely Dan was formed?", True, [["1971", "1972"]], "search: band formed"),
    ("Search the web for how tall Mount Rainier is.", True, [["14,4", "14 4", "4,39", "4,3", "4 39"]], "search: mountain"),
    ("Look up what year Dan Fogelberg released the song Longer.", True, [["1979", "1980"]], "search: song year"),
    ("Who are some of the people quoted on https://quotes.toscrape.com/js/ ?", True, [["einstein", "rowling", "austen",
     "monroe", "eleanor"]], "js-only page"),
    ("How are you feeling today?", False, [], "plain chat (no tools offered)"),
    ("Is the internet useful for finding recipes?", None, [], "web word, no address"),
]
ASKS_THE OWNER = re.compile(r"(paste|copy).{0,30}(text|page|content)|open (it|the page|the link) (for me|yourself)|"
                        r"I (can't|cannot|am unable to) (browse|access the internet|open links)", re.I)


PREFETCH = "--no-prefetch" not in sys.argv
NO_TOOLS = "--no-tools" in sys.argv   # "before": the same questions with Echo's web tools switched off


async def main(label):
    cfg = load_config()
    backend = OllamaBackend(cfg.llm)
    system = load_personality(cfg) + SYSTEM_EXTRA
    rows = []
    only = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), "")
    for q, expect_tool, facts, name in [c for c in CASES if only in c[3]]:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": q}]
        turn = None
        t0 = time.perf_counter(); first = None; out = []
        if tools.wants_tools(q) and not NO_TOOLS:
            turn = tools.Turn(research=tools.wants_research(q)); turn.allow(q)
            msgs[-1]["content"] += "\n\n" + tools.HINT
            if turn.research:
                msgs[-1]["content"] += "\n" + tools.RESEARCH_HINT.format(n=turn.max_pages)
            stream = tools.stream_with_tools(backend, msgs, turn, prefetch=tools.prefetch_calls(q) if PREFETCH else None)
        else:
            stream = backend.chat_stream(msgs)
        async for piece in stream:
            first = first or time.perf_counter()
            out.append(piece)
        text = "".join(out)
        low = text.lower().replace("\u2019", "'").replace("\u2018", "'")
        called = bool(turn and turn.calls)
        facts_ok = all(any(f in low for f in group) for group in facts) and \
            (len(turn.sources) if turn else 0) >= MIN_PAGES.get(name, 0)
        tool_ok = expect_tool is None or called == expect_tool
        row = {"case": name, "question": q, "answer": text, "tool_called": called,
               "calls": turn.calls if turn else [], "sources": turn.sources if turn else [],
               "tool_ok": tool_ok, "facts_ok": facts_ok, "asked_owner_to_open": bool(ASKS_THE OWNER.search(text)),
               "first_word_s": round((first or time.perf_counter()) - t0, 2), "total_s": round(time.perf_counter() - t0, 2),
               "prompt_tokens": backend.last_stats.get("prompt_tokens"), "rounds": backend.last_stats.get("rounds", 1),
               "page_chars": turn.used_chars if turn else 0, "pages_read": len(turn.sources) if turn else 0}
        rows.append(row)
        print(f"{'PASS' if tool_ok and facts_ok and not row['asked_owner_to_open'] else 'FAIL'} {name:28} tool={called} "
              f"pages={row['pages_read']} first={row['first_word_s']}s total={row['total_s']}s ctx={row['prompt_tokens']} | {text[:150]!r}")
    ok = [r for r in rows if r["tool_ok"] and r["facts_ok"] and not r["asked_owner_to_open"]]
    tool_rows = [r for r in rows if r["tool_called"]]
    summary = {"cases": len(rows), "passed": len(ok),
               "tool_calls": sum(len(r["calls"]) for r in rows),
               "tool_calls_ok": sum(c["ok"] for r in rows for c in r["calls"]),
               "median_total_s_tools": sorted(r["total_s"] for r in tool_rows)[len(tool_rows) // 2] if tool_rows else None,
               "max_prompt_tokens": max((r["prompt_tokens"] or 0) for r in rows)}
    print(summary)
    (ROOT / f"docs/bench/tools-{label}.json").write_text(json.dumps({"when": time.strftime("%Y-%m-%d %H:%M"),
                                                                     "model": cfg.llm.model, "summary": summary,
                                                                     "rows": rows}, indent=1))

asyncio.run(main(next((a for a in sys.argv[1:] if not a.startswith("--")), "run")))
