#!/usr/bin/env python3
"""Fan a list of small jobs out across the Mesh so they run in parallel on every online computer.

Run on the Mac. Standard library only. Sends ordinary `generate` jobs (the same kind the Mesh already runs), NOT pinned to a computer:
whichever computer is free and has the model takes the next one, so fast machines do more. Each computer's policy and share limit still apply.

  python3 tools/fanout.py --prompt "Write a one-line docstring for this function:\n{item}" --items-file funcs.txt
  python3 tools/fanout.py --prompt "Summarise this file in two sentences:\n{item}" --files a.py b.py c.py --out /tmp/summaries.json
  python3 tools/fanout.py --prompt "Give 3 edge-case test inputs for: {item}" --item "parse_date(s)" --item "split_name(s)"

Items: --item (repeat), --items-file (one per line, blank lines skipped), or --files (each file's text is the item; its path is the label).
{item} in the prompt is replaced by the item. The model answers are DRAFTS from small models: check every one before using it.
"""
import argparse, json, os, sys, time, urllib.request, urllib.error

ADMIN = os.environ.get("ECHO_MESH_ADMIN", "http://127.0.0.1:47830")
TOKEN_FILE = os.path.expanduser(os.environ.get("ECHO_MESH_TOKEN", "~/.echo-mesh/admin.token"))
DEFAULT_MODEL = "qwen2.5-coder:3b"
MAX_PROMPT = 19000        # the Mesh refuses prompts over 20,000 characters


def api(method, path, body=None):
    token = open(TOKEN_FILE).read().strip()
    req = urllib.request.Request(ADMIN + path, method=method, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"null")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompt", required=True, help="prompt template; {item} is replaced by each item (\\n is a line break)")
    ap.add_argument("--item", action="append", default=[], help="one item (repeatable)")
    ap.add_argument("--items-file", help="a text file with one item per line")
    ap.add_argument("--files", nargs="*", default=[], help="files whose whole text is one item each")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=float, default=600, help="seconds to wait for everything (default 600)")
    ap.add_argument("--out", help="also save the results as JSON to this path")
    a = ap.parse_args()

    template = a.prompt.replace("\\n", "\n")
    items = [(s, s) for s in a.item]
    if a.items_file:
        items += [(ln.strip(), ln.strip()) for ln in open(a.items_file) if ln.strip()]
    items += [(open(p, errors="replace").read(), p) for p in a.files]
    if not items:
        ap.error("give at least one --item, --items-file or --files")

    try:
        nodes = {n["id"]: n for n in api("GET", "/status")["nodes"]}
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"Can't reach the Mesh coordinator ({e}).")
    able = [n["name"] for n in nodes.values() if n["online"] and a.model in ((n.get("inventory", {}).get("ollama") or {}).get("models") or [])]
    if not able:
        sys.exit(f"No online computer has {a.model}.")

    jobs, skipped = {}, []
    for text, label in items:
        prompt = template.replace("{item}", text)
        if len(prompt) > MAX_PROMPT:
            skipped.append(label[:60]); continue
        jid = api("POST", "/jobs", {"type": "generate", "payload": {"model": a.model, "prompt": prompt}, "project": "fanout", "requires": {"model": a.model}})["id"]
        jobs[jid] = label
    print(f"Sent {len(jobs)} job(s) to the Mesh ({', '.join(sorted(able))} can take them)." + (f" Skipped {len(skipped)} too long: {', '.join(skipped)}" if skipped else ""), file=sys.stderr)

    t0, pending, out = time.time(), set(jobs), []
    while pending and time.time() - t0 < a.timeout:
        for jid in list(pending):
            j = api("GET", f"/jobs/{jid}")
            if j and j["status"] in ("done", "failed"):
                pending.discard(jid)
                r = json.loads(j["result"] or "{}") if j["status"] == "done" else {}
                out.append({"item": jobs[jid], "ok": j["status"] == "done", "node": nodes.get(j["node"], {}).get("name", j["node"]),
                            "seconds": r.get("seconds"), "response": (r.get("response") or "").strip(), "error": j.get("error")})
        if pending:
            time.sleep(1)
    order = list(jobs.values())
    out.sort(key=lambda r: order.index(r["item"]))
    for jid in pending:
        out.append({"item": jobs[jid], "ok": False, "node": None, "seconds": None, "response": "", "error": f"no answer in {a.timeout:.0f}s (job {jid} may still run later)"})

    took = time.time() - t0
    by = {}
    for r in out:
        by[r["node"]] = by.get(r["node"], 0) + 1
    print(f"Done in {took:.0f}s: {sum(r['ok'] for r in out)}/{len(out)} answered. Per computer: {by}", file=sys.stderr)
    if a.out:
        json.dump(out, open(a.out, "w"), indent=1)
    json.dump(out, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
