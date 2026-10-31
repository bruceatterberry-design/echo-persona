"""Page reading benchmark: old Learn fetcher (backups/jarvis-step1/learn.py) vs core/web.py.

Run: .venv/bin/python scripts/bench_web.py [label]   → docs/bench/web-<label>.json
Metrics per page: success, seconds, characters returned, expected phrases found (main content present),
boilerplate phrases found (menus/footers/cookie text leaking in). Safety: private-address cases must be refused.
"""
import importlib.util
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import web  # noqa: E402

PAGES = [  # (url, phrases that should be in the readable text)
    ("https://en.wikipedia.org/wiki/Crappie", ["Pomoxis", "black crappie"]),
    ("https://en.wikipedia.org/wiki/Special_education", ["disabilities", "individualized"]),
    ("https://docs.python.org/3/library/json.html", ["json.dumps", "JSON"]),
    ("https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/main", ["dominant content"]),
    ("https://www.gutenberg.org/ebooks/1342", ["Pride and Prejudice", "Austen"]),
    ("https://github.com/bertrandmbanwi/Jarvis", ["JARVIS"]),
    ("https://www.nps.gov/yell/index.htm", ["Yellowstone"]),
    ("https://example.com", ["documentation examples"]),
    ("https://www.loc.gov/about/", ["Library of Congress"]),
    ("https://www.ruby-lang.org/en/", ["Ruby"]),
    ("https://www.cdc.gov/ncbddd/developmentaldisabilities/facts.html", ["developmental disabilities"]),
    ("https://text.npr.org/", ["NPR"]),
]
BOILER = ["privacy policy", "cookie", "all rights reserved", "skip to", "sign in", "log in", "subscribe",
          "terms of use", "main menu", "toggle navigation", "javascript", "accessibility statement"]
UNSAFE = ["http://127.0.0.1:8765/health", "http://localhost:11434/api/tags"]
# Redirect-into-private-address is covered by tests/test_web.py (a live redirect service blocked us with 403).


def old_reader():
    spec = importlib.util.spec_from_file_location("old_learn", ROOT / "backups/jarvis-step1/learn.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

    def read(url):
        r = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Echo (personal, local)"})
        r.raise_for_status()
        return m.html_text(r.text)[1] if "html" in r.headers.get("content-type", "") else r.text
    return read


def new_reader(url):
    return web.read_page(url, max_chars=10**9).text


def score(text, phrases):
    low = text.lower()
    return (sum(p.lower() in low for p in phrases), len(phrases), sum(b in low for b in BOILER))


def run(name, read):
    rows = []
    for url, phrases in PAGES:
        t0 = time.perf_counter()
        try:
            text = read(url); ok, err = True, ""
        except Exception as e:  # noqa: BLE001
            text, ok, err = "", False, f"{type(e).__name__}: {e}"[:160]
        hit, n, boiler = score(text, phrases)
        rows.append({"url": url, "ok": ok, "error": err, "seconds": round(time.perf_counter() - t0, 3),
                     "chars": len(text), "phrases_found": hit, "phrases": n, "boilerplate": boiler})
        print(f"{name:4} {'OK ' if ok else 'ERR'} {rows[-1]['seconds']:6.2f}s {len(text):7d}ch {hit}/{n} boiler={boiler} {url} {err}")
    safety = []
    for url in UNSAFE:
        try:
            read(url); refused = False
        except Exception as e:  # noqa: BLE001
            refused = True; err = type(e).__name__
        safety.append({"url": url, "refused": refused})
        print(f"{name:4} safety {'REFUSED' if refused else 'FETCHED (unsafe)'} {url}")
    return {"pages": rows, "safety": safety}


if __name__ == "__main__":
    label = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y%m%d")
    out = {"when": time.strftime("%Y-%m-%d %H:%M"), "old": run("old", old_reader()), "new": run("new", new_reader)}
    for k in ("old", "new"):
        p = [r for r in out[k]["pages"]]
        out[k]["summary"] = {
            "success": sum(r["ok"] for r in p), "pages": len(p),
            "phrase_recall": round(sum(r["phrases_found"] for r in p) / sum(r["phrases"] for r in p), 3),
            "boilerplate_hits": sum(r["boilerplate"] for r in p),
            "median_seconds": sorted(r["seconds"] for r in p)[len(p) // 2],
            "total_chars": sum(r["chars"] for r in p),
            "unsafe_refused": sum(s["refused"] for s in out[k]["safety"]), "unsafe_cases": len(out[k]["safety"])}
        print(k, out[k]["summary"])
    path = ROOT / f"docs/bench/web-{label}.json"
    path.write_text(json.dumps(out, indent=1))
    print("saved", path)
