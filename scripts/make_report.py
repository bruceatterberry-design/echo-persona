"""Before/after report for the 2026-10-31 work (web, tools, music, training, voice): SVG charts + a Markdown page.

Run: .venv/bin/python scripts/make_report.py  → docs/report/*.svg + docs/report/README.md (GitHub shows both).
Data: docs/bench/*.json (saved by the benchmark scripts) and the voice/memory measurements in STATUS.md.
Colours: dataviz reference palette, slot 1 blue = after, slot 2 orange = before (validated: all checks pass).
"""
import json
import re
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
B = ROOT / "docs" / "bench"
OUT = ROOT / "docs" / "report"
SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8984", "#e4e3df"
AFTER, BEFORE = "#2a78d6", "#eb6834"
FONT = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
W = 760


def bar(x0, y, w, h, color):
    """Horizontal bar, square at the baseline, 4 px rounded data end."""
    if w <= 0.5:
        return f'<rect x="{x0}" y="{y}" width="1.5" height="{h}" fill="{color}"/>'
    r = min(4, w / 2, h / 2)
    return (f'<path d="M{x0},{y} H{x0 + w - r} Q{x0 + w},{y} {x0 + w},{y + r} V{y + h - r} '
            f'Q{x0 + w},{y + h} {x0 + w - r},{y + h} H{x0} Z" fill="{color}"/>')


def svg(height, body, title, subtitle):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" viewBox="0 0 {W} {height}" '
            f'role="img" aria-label="{escape(title)}"><rect width="{W}" height="{height}" fill="{SURFACE}" rx="8"/>'
            f'<text x="24" y="34" style="{FONT};font-size:17px;font-weight:600" fill="{INK}">{escape(title)}</text>'
            f'<text x="24" y="56" style="{FONT};font-size:13px" fill="{INK2}">{escape(subtitle)}</text>{body}</svg>')


def legend(y, items):
    out, x = "", 24
    for name, color in items:
        out += (f'<rect x="{x}" y="{y - 10}" width="12" height="12" rx="3" fill="{color}"/>'
                f'<text x="{x + 18}" y="{y}" style="{FONT};font-size:13px" fill="{INK2}">{escape(name)}</text>')
        x += 30 + 7.2 * len(name)
    return out


def grouped(title, subtitle, cats, series, xmax, fmt, ticks, unit="", ref=None):
    """cats: labels; series: [(name, color, values)]; fmt(series_index, cat_index, value) -> label text."""
    left, right, top = 210, 84, 96
    bh, gap, group_gap = 16, 2, 18
    gh = len(series) * bh + (len(series) - 1) * gap
    h = top + len(cats) * (gh + group_gap) + 40
    plot_w = W - left - right
    sx = lambda v: left + plot_w * v / xmax   # noqa: E731
    body = legend(80, [(n, c) for n, c, _ in series]) if len(series) > 1 else ""
    for t in ticks:
        x = sx(t)
        body += (f'<line x1="{x}" y1="{top - 6}" x2="{x}" y2="{h - 34}" stroke="{GRID}" stroke-width="1"/>'
                 f'<text x="{x}" y="{h - 16}" text-anchor="middle" style="{FONT};font-size:11px" fill="{MUTED}">'
                 f'{escape(str(t))}{unit}</text>')
    if ref:
        x = sx(ref[0])
        body += (f'<line x1="{x}" y1="{top - 10}" x2="{x}" y2="{h - 34}" stroke="{INK2}" stroke-width="1.5" '
                 f'stroke-dasharray="4 3"/><text x="{x - 4}" y="{top - 14}" text-anchor="end" '
                 f'style="{FONT};font-size:11px" fill="{INK2}">{escape(ref[1])}</text>')
    for i, cat in enumerate(cats):
        y0 = top + i * (gh + group_gap)
        body += (f'<text x="{left - 12}" y="{y0 + gh / 2 + 4}" text-anchor="end" style="{FONT};font-size:13px" '
                 f'fill="{INK}">{escape(cat)}</text>')
        for j, (_name, color, vals) in enumerate(series):
            v = vals[i]
            y = y0 + j * (bh + gap)
            w = max(0.0, sx(v) - left)
            body += bar(left, y, w, bh, color)
            body += (f'<text x="{left + w + 6}" y="{y + bh - 4}" style="{FONT};font-size:12px" fill="{INK2}">'
                     f'{escape(fmt(j, i, v))}</text>')
    body += f'<line x1="{left}" y1="{top - 6}" x2="{left}" y2="{h - 34}" stroke="{MUTED}" stroke-width="1"/>'
    return svg(h, body, title, subtitle)


def small_multiples(title, subtitle, panels):
    """panels: [(label, unit_note, before, after, max, fmt)], one mini chart each (own scale, no shared axis)."""
    cols, pw, ph = 2, (W - 72) / 2, 112
    h = 84 + ((len(panels) + 1) // 2) * (ph + 14) + 10
    body = legend(80, [("Before", BEFORE), ("After", AFTER)])
    for k, (label, note, b, a, mx, fmt) in enumerate(panels):
        cx = 24 + (k % cols) * (pw + 24)
        cy = 100 + (k // cols) * (ph + 14)
        body += (f'<rect x="{cx}" y="{cy}" width="{pw}" height="{ph}" rx="6" fill="none" stroke="{GRID}"/>'
                 f'<text x="{cx + 14}" y="{cy + 24}" style="{FONT};font-size:13px;font-weight:600" fill="{INK}">'
                 f'{escape(label)}</text><text x="{cx + 14}" y="{cy + 42}" style="{FONT};font-size:11px" '
                 f'fill="{MUTED}">{escape(note)}</text>')
        bw = pw - 120
        for j, (v, color) in enumerate(((b, BEFORE), (a, AFTER))):
            y = cy + 56 + j * 24
            w = bw * v / mx if mx else 0
            body += bar(cx + 14, y, w, 18, color)
            body += (f'<text x="{cx + 20 + w}" y="{y + 14}" style="{FONT};font-size:12px" fill="{INK2}">'
                     f'{escape(fmt(v))}</text>')
    return svg(h, body, title, subtitle)


# ---------------- data ----------------
def load(name):
    return json.loads((B / name).read_text())


before, after = load("tools-report-before.json"), load("tools-report-after.json")
reader = load("web-step1.json")
GROUPS = [("Reading a page", ["simple page", "fact in long page", "gov page", "bare address", "docs question", "links",
                              "dead site"]),
          ("Research (follow links)", ["research: follow 1 link", "research: wikipedia hop", "research: author page"]),
          ("Web search", ["search: band formed", "search: mountain", "search: song year"]),
          ("JavaScript-only page", ["js-only page"]),
          ("Everyday chat", ["plain chat (no tools offered)", "web word, no address"])]
CLAIM = re.compile(r"\b(I'?m|I am|I'?ll|let me|I will)\s+(going to\s+)?(open|look|read|check|opening|looking|reading|"
                   r"checking|pull|go to|find)", re.I)


def rows_by(run):
    return {r["case"]: r for r in run["rows"]}


def ok(r):
    return r["tool_ok"] and r["facts_ok"] and not r["asked_owner_to_open"]


def med(xs):
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2) if n else 0


rb, ra = rows_by(before), rows_by(after)
false_before = sum(1 for r in before["rows"] if not r["tool_called"] and CLAIM.search(r["answer"])
                   and r["case"] not in GROUPS[-1][1])
false_after = sum(1 for r in after["rows"] if not r["tool_called"] and CLAIM.search(r["answer"])
                  and r["case"] not in GROUPS[-1][1])
cats = [g for g, _ in GROUPS]
pb = [sum(ok(rb[c]) for c in cs) / len(cs) * 100 for _, cs in GROUPS]
pa = [sum(ok(ra[c]) for c in cs) / len(cs) * 100 for _, cs in GROUPS]
counts = [len(cs) for _, cs in GROUPS]
OUT.mkdir(parents=True, exist_ok=True)

(OUT / "1-questions-answered.svg").write_text(grouped(
    "Web questions Echo answered correctly",
    f"Same 16 questions to Echo's own model (qwen3.5 9B): without vs with his new tools · "
    f"{before['summary']['passed']}/16 → {after['summary']['passed']}/16",
    cats, [("Before (no tools)", BEFORE, pb), ("After (tools on)", AFTER, pa)], 100,
    lambda j, i, v: f"{round(v * counts[i] / 100)}/{counts[i]}", [0, 25, 50, 75, 100], "%"))

rs = reader["old"]["summary"], reader["new"]["summary"]
(OUT / "2-page-reader.svg").write_text(small_multiples(
    "Page reader: old Learn fetcher vs new safe reader",
    "12 real web pages + attempts to reach Echo's own services on this Mac",
    [("Pages read", "out of 12", rs[0]["success"], rs[1]["success"], 12, lambda v: f"{v}/12"),
     ("Key facts found", "share of expected phrases in the text", rs[0]["phrase_recall"] * 100,
      rs[1]["phrase_recall"] * 100, 100, lambda v: f"{v:.0f}%"),
     ("Menu / footer text leaking in", "boilerplate phrases found, lower is better", rs[0]["boilerplate_hits"],
      rs[1]["boilerplate_hits"], 8, lambda v: f"{v}"),
     ("Attacks refused", "requests to this Mac's own services", rs[0]["unsafe_refused"], rs[1]["unsafe_refused"],
      2, lambda v: f"{v}/2")]))

tb = [med([rb[c]["total_s"] for c in cs]) for _, cs in GROUPS]
ta = [med([ra[c]["total_s"] for c in cs]) for _, cs in GROUPS]
(OUT / "3-answer-time.svg").write_text(grouped(
    "Time to a full answer (median seconds)",
    "Before = fast but no real answer (it can't read pages). After = reads, then answers. Mac mini M4, 16 GB.",
    cats, [("Before (no tools)", BEFORE, tb), ("After (tools on)", AFTER, ta)], 25,
    lambda j, i, v: f"{v:.1f} s", [0, 5, 10, 15, 20, 25], " s"))

cb = [max(rb[c]["prompt_tokens"] or 0 for c in cs) for _, cs in GROUPS]
ca = [max(ra[c]["prompt_tokens"] or 0 for c in cs) for _, cs in GROUPS]
(OUT / "4-context-used.svg").write_text(grouped(
    "How much of the model's 8,192-token window a question uses (largest case)",
    "Benchmark prompts carry no conversation or memories; live use adds ~2,000 (research ≈ 6,000, still inside)",
    cats, [("Before (no tools)", BEFORE, cb), ("After (tools on)", AFTER, ca)], 8192,
    lambda j, i, v: f"{v:,}", [0, 2000, 4000, 6000, 8000], "", ref=(8192, "window 8,192")))

MEM = [("Voice (Qwen3-TTS)", 2.65, 1.86), ("Speech recognition (Parakeet)", 2.51, 1.57), ("Swap (memory on disk)", 3.10, 0.88)]
(OUT / "5-memory.svg").write_text(grouped(
    "Memory before and after the 16 GB voice fix (GB)",
    "8-bit voice + half-precision speech recognition on Macs with 16 GB or less · free memory 26% → 46%",
    [m[0] for m in MEM], [("Before", BEFORE, [m[1] for m in MEM]), ("After", AFTER, [m[2] for m in MEM])], 3.5,
    lambda j, i, v: f"{v:.2f} GB", [0, 1, 2, 3], " GB"))

VOICE = [("Start of day (jitter buffer only)", 3.54), ("After freeing memory", 4.89), ("After 8-bit voice", 2.05)]
(OUT / "6-voice-gaps.svg").write_text(grouped(
    "Silence inside Echo's voice while the model is busy (seconds, 4 replies)",
    "Freeing memory stopped the swapping but not the gaps; the faster 8-bit voice reduced them (3 gaps → 2)",
    [v[0] for v in VOICE], [("Silence", AFTER, [v[1] for v in VOICE])], 6,
    lambda j, i, v: f"{v:.1f} s", [0, 2, 4, 6], " s"))

# ---------------- the page ----------------
table = "\n".join(f"| {c} | {round(pb[i] * counts[i] / 100)}/{counts[i]} | {round(pa[i] * counts[i] / 100)}/{counts[i]} | "
                  f"{tb[i]:.1f} s | {ta[i]:.1f} s | {cb[i]:,} | {ca[i]:,} |" for i, c in enumerate(cats))
calls = sum(len(r["calls"]) for r in after["rows"])
calls_ok = sum(c["ok"] for r in after["rows"] for c in r["calls"])
music = load("music-20261031.json")
mrows = "\n".join(f"| {r['say'].split(' It')[0].split(' looks like')[0].replace('I finished listening to ', '')[:60]} | "
                  f"{'listened' if r['ok'] else 'declined (not on the list)'} | {r['seconds']} s | "
                  f"{(r['analysis'] or {}).get('bpm', '–')} | {r['audio_files_left']} |" for r in music["rows"])
page = f"""# Echo: before / after report (2026-10-31)

Measured on a **Mac mini M4, 16 GB**, with Echo's own local model (qwen3.5 9B through Ollama). Nothing here used Claude
except the training test. Raw data: `docs/bench/*.json`; regenerate with `scripts/make_report.py`.

## Headline

- **Web questions answered correctly: {before['summary']['passed']}/16 → {after['summary']['passed']}/16.**
- **False "I'm opening that page now" claims (no tool behind them): {false_before} → {false_after}.**
- **Tool calls that worked: {calls_ok}/{calls}** (the one that didn't is a site that doesn't exist, which is supposed to fail; Echo said so).
- **Attacks on Echo's own services through a web page: blocked** (plain reader 2/2, invisible browser 3/3).
- **First spoken word on web questions: 15–24 s → 3.6–7.8 s** after the 16 GB voice fix (swap 3.1 → 0.9 GB).

![Web questions answered]( 1-questions-answered.svg)

![Page reader](2-page-reader.svg)

![Answer time](3-answer-time.svg)

![Context used](4-context-used.svg)

*The benchmark questions carry no conversation history or memories. In live use those add about 2,000 tokens, so a
research question uses about 6,000 of the 8,192: inside the window, with less room to spare than the chart suggests.*

![Memory](5-memory.svg)

![Voice gaps](6-voice-gaps.svg)

## The numbers behind the charts

| Kind of question | Correct before | Correct after | Time before | Time after | Context before (tokens) | Context after |
|---|---|---|---|---|---|---|
{table}

**Page reader** (12 real pages): pages read {rs[0]['success']}/12 → {rs[1]['success']}/12 · key facts found
{rs[0]['phrase_recall']:.0%} → {rs[1]['phrase_recall']:.0%} · menu/footer leaks {rs[0]['boilerplate_hits']} →
{rs[1]['boilerplate_hits']} · requests to Echo's own services refused {rs[0]['unsafe_refused']}/2 → {rs[1]['unsafe_refused']}/2 ·
median time {rs[0]['median_seconds']} s → {rs[1]['median_seconds']} s.

**Processor and memory while answering** (sampled every 2 s during the 16 questions):

| Part | Before (no tools) | After (tools on) |
|---|---|---|
| Language model (Ollama) | 14% CPU, 7.0 GB | 11% CPU, 7.5 GB (longer prompts) |
| Echo's Python services | peak 3.5 GB | peak 3.5 GB |
| Invisible Chrome (JavaScript pages only) | – | ~1.0 GB for ~2 s, then freed |

The heavy work runs on the graphics chip, so processor use stays low.

**Music listening** (real YouTube, scratch memory; audio deleted after each song):

| Song | Result | Time | Tempo (bpm) | Audio files left |
|---|---|---|---|---|
{mrows}

Lyrics were very good on "Simple Man". Musical key is reported only as a best guess (major/minor unreliable on full mixes).

**Train Echo** (real Claude, 2 calls): diagnosis 6 s, check of the chosen fix 7 s; both accurate. On a 9B model a new
rule doesn't always take on the first try; the 👎 → retry loop handles that.

## Honest limits

- Times vary with what else the Mac is doing; the first question after a restart is slower (the model reads its
  instructions once, ~18 s).
- Research and search answers take 15–20 s (reading 1–2 pages plus a longer prompt).
- Speaker recognition through the room microphone improves as Echo collects room samples ("Is that you, the owner?",
  "This is the owner"); not yet measured with real room samples.
"""
(OUT / "README.md").write_text(page.replace("]( 1-", "](1-"))
print("wrote", OUT, "| before", before["summary"]["passed"], "after", after["summary"]["passed"],
      "| false claims", false_before, "->", false_after)
