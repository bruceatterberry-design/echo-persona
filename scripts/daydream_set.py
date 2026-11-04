"""Fixed Daydream test set: the same harder, multi-step tasks every time, so runs can be compared over time (like
scripts/baseline.py). Each task has expected-answer regexes (all must match = accurate); one task also checks identity
(baseline's "Who are you?" rule) and one checks that Echo says he doesn't know instead of inventing.

For every task, three answers are made and scored (core/daydream_score.py): Echo alone, Echo + tower, full Daydream.
Consistency = the same task repeated (--repeat, default from config/echo.yaml daydream.consistency_runs) for the two
modes that use no Claude; the full Daydream runs --full-repeat times (default 1) because each run costs 3 Claude
calls of the owner's daily cap.

    ~/EchoPersona/.venv/bin/python scripts/daydream_set.py [--only t01,t05] [--repeat 3] [--full-repeat 1] [--force]
        → one card per task in docs/bench/daydream/, plus set-<time>.json with the run's averages

Uses Echo's real model and instructions (personality + lessons); never his memories. Refuses to start if someone
talked to Echo in the last 10 minutes (Echo's model is shared), unless --force.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import daydream, daydream_score as score, trainer   # noqa: E402
from core.claude_helper import ClaudeHelper                     # noqa: E402
from core.config import load_config, load_personality          # noqa: E402
from personality.evals import DONT_KNOW                         # noqa: E402

SUITE_VERSION = "daydream-v1"   # change the tasks only with a new version, so old runs stay comparable
N = r"(?:\$\s?)?"
TASKS = [
    {"id": "t01-recipe", "task": "A recipe for 4 people needs 300 g of flour and 2 eggs. I'm cooking for 10 people. How "
     "much flour and how many eggs do I need, and if eggs come in boxes of 6, how many boxes do I buy?",
     "expect": [r"\b750\b|seven hundred (?:and )?fifty", r"\b5 eggs|\bfive eggs", r"\b(?:1|one) (?:box|carton)"]},
    {"id": "t02-budget", "task": "We take home $2,400 a month. Rent is $1,050, the car payment $310, groceries $450 and "
     "utilities $180. How much is left each month, and how many whole months until we've saved $3,000 if we save all of it?",
     "expect": [N + r"410\b|four hundred (?:and )?ten", r"\b8 months|\beight months|\b8 whole|\beight whole"]},
    {"id": "t03-drive", "task": "I leave at 7:40 AM, drive 2 hours 35 minutes, stop 25 minutes for lunch, then drive 1 "
     "hour 50 minutes more. What time do I arrive?", "expect": [r"12:30|twelve[- ]thirty"]},
    {"id": "t04-cat-meds", "task": "My cat weighs 5 kg. The vet says one tablet per 2.5 kg of body weight, twice a day, "
     "for 7 days. How many tablets is that in total, and if a pack holds 10, how many packs do I need?",
     "expect": [r"\b28\b|twenty[- ]eight", r"\b3 packs|\bthree packs"]},
    {"id": "t05-workday", "task": "I work 9 to 5 with meetings from 10 to 11 and from 2 to 3. I have three tasks that take "
     "45, 90 and 30 minutes. Can I finish all three today, and how much free working time is left after them?",
     "expect": [r"\byes\b|\bcan\b", r"\b195\b|3 hours (?:and )?15|three hours and (?:fifteen|15)|3h ?15"]},
    {"id": "t06-ages", "task": "Anna is older than Ben. Ben is older than Cara. Dan is younger than Cara. Who is the "
     "youngest, and who is the second oldest?", "expect": [r"\bdan\b.{0,60}youngest|youngest.{0,60}\bdan\b",
                                                         r"\bben\b.{0,60}second|second.{0,60}\bben\b"]},
    {"id": "t07-interest", "task": "I put $1,000 in savings at 5% a year, compounded once a year. How much is there after "
     "3 years, to the cent, and how much of that is interest?", "expect": [N + r"1,?157\.63", N + r"157\.63"]},
    {"id": "t08-discount", "task": "An $80 jacket is 25% off, and the register takes another 10% off the sale price. "
     "What do I pay, and is that the same as 35% off?",
     "expect": [N + r"54\b|fifty[- ]four", r"\bno\b|not the same|isn'?t the same|different|less"]},
    {"id": "t09-gift-identity", "task": "Help me plan a surprise for the loved one's birthday on a $150 budget: dinner is $70 and "
     "flowers are $35. How much can the gift cost? Then remind me in one sentence who you are.",
     "expect": [N + r"45\b|forty[- ]five", score.IDENTITY_RX]},
    {"id": "t10-unknown", "task": "What is my neighbor's dog called, and what breed is it? Then suggest two polite ways "
     "I could find out.", "expect": ["(?:" + "|".join(d.replace("'", "'?") for d in DONT_KNOW) + "|no way (?:for me )?to know|"
                                     r"can'?t know|haven'?t (?:told|mentioned)|wasn'?t told|isn'?t something i|"
                                     r"don'?t have (?:that|this|any|the) information|no information|"
                                     r"(?:my )?notes don'?t (?:mention|say|include))"]},
    {"id": "t11-flooring", "task": "A room is 12 feet by 15 feet. How many square feet is it, and how many boxes of "
     "flooring do I need if one box covers 22 square feet and I add 10% for waste?",
     "expect": [r"\b180\b|one hundred (?:and )?eighty", r"\b9 boxes|\bnine boxes"]},
    {"id": "t12-doses", "task": "A medicine is taken every 8 hours, starting at 6 AM on day one. How many doses are "
     "taken over 3 full days, and what time is the last dose on day three?",
     "expect": [r"\b9 doses|\bnine doses|\b9 total|\bnine total|\b9\b.{0,20}doses", r"10(?::00)? ?p\.?m|22:00|ten (?:at night|p\.?m)"]},
]


DATA = Path(__file__).resolve().parents[1] / "data"   # Echo's live data: lessons, conversation times, Claude usage (read only)


def instructions(cfg) -> str:
    """Exactly what the live Echo uses (core/server.py): personality + SYSTEM_EXTRA + the owner's lessons."""
    from core.server import SYSTEM_EXTRA
    return load_personality(cfg) + SYSTEM_EXTRA + trainer.lessons_block(DATA / "lessons.json")


def last_talk_minutes() -> float | None:
    try:
        db = sqlite3.connect(f"file:{DATA / 'memory.sqlite'}?mode=ro", uri=True)
        ts = db.execute("SELECT max(ts) FROM turns").fetchone()[0]
        return (time.time() - ts) / 60 if ts else None
    except sqlite3.Error:
        return None


async def one_task(t: dict, local: daydream.Local, dcfg: dict, helper, repeat: int, full_repeat: int) -> dict:
    card = score.new_card(t["task"], "testset", f"{SUITE_VERSION}-{t['id']}-{datetime.now():%H%M%S}",
                          suite=SUITE_VERSION, task_id=t["id"], task=t["task"])

    async def timed_ask(prompt: str) -> tuple[str, int, dict]:
        before, t0 = dict(local.tokens), time.time()
        out = await local.ask(prompt, voice=True)
        return out, round((time.time() - t0) * 1000), {k: local.tokens[k] - before[k] for k in local.tokens}

    runs = [await timed_ask(t["task"]) for _ in range(repeat)]
    card["modes"]["echo"] = score.measure([r[0] for r in runs], t["expect"], [r[1] for r in runs], [r[2] for r in runs],
                                          {"echo": "used"})
    tower_runs, helpers = [], {}
    for _ in range(repeat):
        res = await daydream.run(t["task"], local, dcfg, {"tower": daydream.tower_drafter(dcfg)})
        helpers = res.helpers
        if res.ok:
            tower_runs.append(res)
    if tower_runs and any(r.helpers.get("tower") == "used" for r in tower_runs):
        card["modes"]["echo_tower"] = score.measure([r.answer for r in tower_runs], t["expect"],
                                                    [r.ms["total"] for r in tower_runs], [r.tokens for r in tower_runs], helpers)
    else:
        card["modes"]["echo_tower"] = {"status": f"tower {helpers.get('tower', 'unavailable')}", "runs": 0}
    full = []
    for _ in range(full_repeat):
        res = await daydream.run(t["task"], local, dcfg,
                                 {"tower": daydream.tower_drafter(dcfg), "claude": daydream.claude_drafter(helper)},
                                 daydream.claude_critic(helper), daydream.claude_checker(helper),
                                 on_event=daydream.bridge_reporter())
        if res.ok:
            full.append(res)
    if full:
        iss = score.issues_count([x for r in full for x in r.issues + r.final_issues])
        card["issues"], card["invented_caught"] = iss, iss["invented"]
        card["revised"] = any(r.revised for r in full)
        card["modes"]["daydream"] = score.measure([r.answer for r in full], t["expect"], [r.ms["total"] for r in full],
                                                  [r.tokens for r in full], full[-1].helpers, issues=iss,
                                                  revised=card["revised"])
        card["answers"] = {"echo": runs[-1][0], "echo_tower": tower_runs[-1].answer if tower_runs else None,
                           "daydream": full[-1].answer}   # fixed, non-personal tasks: kept on the card for review
    else:
        card["modes"]["daydream"] = {"status": "failed", "runs": 0}
    score.write(card)
    return card


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma-separated task ids (prefixes ok), e.g. t01,t07")
    ap.add_argument("--repeat", type=int, default=None, help="runs per task for Echo alone and Echo + tower")
    ap.add_argument("--full-repeat", type=int, default=1, help="runs per task for the full daydream (3 Claude calls each)")
    ap.add_argument("--force", action="store_true", help="run even if someone talked to Echo in the last 10 minutes")
    a = ap.parse_args()
    if (m := last_talk_minutes()) is not None and m < 10 and not a.force:
        print(f"Someone talked to Echo {m:.0f} minutes ago; not starting (Echo's model is shared). Use --force to override.")
        return 2
    cfg = load_config(ROOT / "config" / "echo.yaml")
    dcfg = daydream.load_config(daydream.CONFIG)
    text = instructions(cfg)
    local = daydream.Local(cfg.llm.host, cfg.llm.model, {"temperature": cfg.llm.temperature, **cfg.llm.options},
                           lambda: text, think=dcfg["local_think"], timeout_s=300)
    helper = ClaudeHelper(ROOT / "config" / "claude.json", DATA / "claude-usage.json")   # shares the live daily cap
    repeat = a.repeat or int(dcfg["consistency_runs"])
    tasks = [t for t in TASKS if not a.only or any(t["id"].startswith(x.strip()) for x in a.only.split(","))]
    need = len(tasks) * a.full_repeat * 3
    print(f"{SUITE_VERSION}: {len(tasks)} tasks, Echo/tower ×{repeat}, full daydream ×{a.full_repeat} "
          f"(up to {need} Claude calls; {helper.used_today()}/{helper.config()['daily_limit']} used today)")
    t0, cards = time.time(), []
    for t in tasks:
        c = await one_task(t, local, dcfg, helper, repeat, a.full_repeat)
        cards.append(c)
        print(c["summary"], flush=True)

    def avg(mode: str, k: str):
        v = [c["modes"][mode][k] for c in cards if c["modes"].get(mode, {}).get(k) is not None]
        return round(statistics.mean(v), 3) if v else None
    summary = {"suite": SUITE_VERSION, "at": datetime.now().isoformat(timespec="seconds"), "tasks": len(cards),
               "repeat": repeat, "full_repeat": a.full_repeat, "minutes": round((time.time() - t0) / 60, 1),
               "model": cfg.llm.model, "claude_used_today": helper.used_today(),
               "modes": {m: {k: avg(m, k) for k in ("accuracy", "consistency", "personality", "steadiness", "ms")}
                         for m in score.MODES},
               "invented_caught": sum(c["invented_caught"] for c in cards), "cards": [c["id"] for c in cards]}
    p = score.CARDS / f"set-{datetime.now():%Y%m%d-%H%M%S}.json"
    p.write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary["modes"], indent=1))
    print(f"saved {p}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
