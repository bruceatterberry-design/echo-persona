#!/usr/bin/env python3
"""Ask every online Mesh computer the same question and show the answers side by side.

Run on the Mac (it talks to the coordinator's admin port on 127.0.0.1 with ~/.echo-mesh/admin.token).
Standard library only. Nothing is installed or downloaded: it only sends ordinary `generate` jobs,
the same kind the Mesh already runs, so each computer's own policy and share limit still apply.

  python3 tools/ask_all.py "Why is the sky blue? One sentence."
  python3 tools/ask_all.py --model qwen2.5-coder:7b "Explain a hash map."   # only computers that have that model
  python3 tools/ask_all.py --only laptop,Desk-PC "Say hi."
  python3 tools/ask_all.py --compare "Explain a hash map."                  # then one computer judges the answers
  python3 tools/ask_all.py --compare --judge Desk-PC --judge-model qwen2.5-coder:7b "..."
  python3 tools/ask_all.py --list                                           # who is online and what models they have
  echo "my question" | python3 tools/ask_all.py
"""
import argparse, json, os, random, re, string, sys, time, urllib.request, urllib.error

ADMIN = os.environ.get("ECHO_MESH_ADMIN", "http://127.0.0.1:47830")
TOKEN_FILE = os.path.expanduser(os.environ.get("ECHO_MESH_TOKEN", "~/.echo-mesh/admin.token"))
DEFAULT_MODEL = "qwen2.5-coder:3b"       # the small model the whole Mesh shares


def api(method, path, body=None):
    token = open(TOKEN_FILE).read().strip()
    req = urllib.request.Request(ADMIN + path, method=method, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"null")


def models_of(node):
    return (node.get("inventory", {}).get("ollama", {}) or {}).get("models", [])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="*", help="the question (or pipe it in)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"model name each computer must have (default {DEFAULT_MODEL})")
    ap.add_argument("--only", help="comma-separated computer names to ask (default: every online one)")
    ap.add_argument("--timeout", type=float, default=300, help="seconds to wait for answers (default 300)")
    ap.add_argument("--echo", action="store_true", help="apply Echo's awareness/dialogue/style rules to the question")
    ap.add_argument("--compare", action="store_true", help="after the answers arrive, one computer judges them (answers are shown to it unnamed, in random order)")
    ap.add_argument("--judge", help="computer that judges (default: the one that answered fastest)")
    ap.add_argument("--judge-model", help="model the judge uses (default: same as --model)")
    ap.add_argument("--list", action="store_true", help="just list the computers and their models")
    a = ap.parse_args()

    try:
        status = api("GET", "/status")
    except (urllib.error.URLError, OSError, FileNotFoundError) as e:
        sys.exit(f"Can't reach the Mesh coordinator ({e}). Is com.echomesh.coordinator running on this Mac?")
    nodes = status["nodes"]

    if a.list:
        for n in nodes:
            print(f"{'ONLINE ' if n['online'] else 'offline'}  {n['name']:<14} {n['id']}  {', '.join(models_of(n)) or '(no models)'}")
        return

    question = " ".join(a.question).strip() or ("" if sys.stdin.isatty() else sys.stdin.read().strip())
    if not question:
        ap.error("give a question, or pipe one in")

    wanted = {s.strip().lower() for s in a.only.split(",")} if a.only else None
    targets, skipped = [], []
    for n in nodes:
        if wanted and n["name"].lower() not in wanted:
            continue
        if not n["online"]:
            skipped.append(f"{n['name']} (offline)")
        elif a.model not in models_of(n):
            skipped.append(f"{n['name']} (no {a.model})")
        else:
            targets.append(n)
    if skipped:
        print("Skipping: " + ", ".join(skipped))
    if not targets:
        sys.exit("No online computer can answer that. Try --list.")

    # one job per computer, pinned to it with requires.node, so each answers exactly once
    jobs = {}
    for n in targets:
        payload = {"model": a.model, "prompt": question}
        if a.echo:
            payload["cognition"] = True
        jid = api("POST", "/jobs", {"type": "generate", "payload": payload, "project": "ask-all",
                                    "requires": {"node": n["id"], "model": a.model}})["id"]
        jobs[jid] = n["name"]
    print(f"Asked {len(jobs)} computer(s) with {a.model}: {', '.join(jobs.values())}\n")

    t0, pending, results = time.time(), dict(jobs), {}
    while pending and time.time() - t0 < a.timeout:
        for jid in list(pending):
            j = api("GET", f"/jobs/{jid}")
            if j and j["status"] in ("done", "failed"):
                results[jid] = j
                del pending[jid]
                show(jobs[jid], j, time.time() - t0)
        if pending:
            time.sleep(1)

    for jid, name in pending.items():
        print(f"--- {name}: no answer in {a.timeout:.0f}s (job {jid} may still run later)\n")
    print(f"Done: {len(results)} answered, {len(pending)} waiting.")

    if a.compare:
        compare(a, question, nodes, jobs, results)


def answer_text(j):
    if j["status"] != "done":
        return None
    return (json.loads(j["result"] or "{}").get("response") or "").strip() or None


def compare(a, question, nodes, jobs, results):
    """Send the answers, unnamed and shuffled, to one computer and ask it to pick the best. Same kind of job as the questions."""
    good = {jobs[jid]: (answer_text(j), json.loads(j["result"]).get("seconds", 1e9)) for jid, j in results.items() if answer_text(j)}
    if len(good) < 2:
        print("\nNeed at least two answers to compare.")
        return
    by_name = {n["name"]: n for n in nodes}
    judge = next((n for n in by_name if n.lower() == (a.judge or "").lower()), None) if a.judge else min(good, key=lambda k: good[k][1])
    if not judge or not by_name[judge]["online"]:
        print(f"\nJudge '{a.judge}' is not an online computer. Try --list.")
        return
    jmodel = a.judge_model or a.model
    if jmodel not in models_of(by_name[judge]):
        print(f"\n{judge} does not have {jmodel}. Try --list.")
        return

    names = list(good)
    random.shuffle(names)                                    # the judge must not know who wrote what, or in which order
    labels = dict(zip(string.ascii_uppercase, names))
    budget = max(500, (19000 - len(question) - 1200) // len(names))
    blocks = "\n\n".join(f"ANSWER {k}:\n{good[v][0][:budget]}" for k, v in labels.items())
    prompt = (f"You are a fair judge. Several answers were given to the same question.\n\nQUESTION:\n{question}\n\n{blocks}\n\n"
              f"Pick the answer that is the most correct, clear and useful. Reply in exactly this format:\n"
              f"BEST: <one letter>\nRANKING: <letters from best to worst, e.g. B > A > C>\nREASON: <two short sentences>")
    print(f"\nComparing: {judge} is judging {len(names)} unnamed answers with {jmodel}...")
    jid = api("POST", "/jobs", {"type": "generate", "payload": {"model": jmodel, "prompt": prompt}, "project": "ask-all",
                               "requires": {"node": by_name[judge]["id"], "model": jmodel}})["id"]
    t0, j = time.time(), None
    while time.time() - t0 < a.timeout:
        j = api("GET", f"/jobs/{jid}")
        if j and j["status"] in ("done", "failed"):
            break
        time.sleep(1)
    if not j or j["status"] != "done":
        print(f"The judge did not answer ({(j or {}).get('error') or 'timed out'}).")
        return
    verdict = answer_text(j) or ""
    m = re.search(r"BEST:\s*\**\s*([A-Z])", verdict)
    print("\n=== Judge (" + judge + ") ===\n" + verdict + "\n")
    if m and m.group(1) in labels:
        print("Who was who: " + ", ".join(f"{k} = {v}" for k, v in labels.items()))
        print(f"Winner: {labels[m.group(1)]}")
    else:
        print("The judge did not follow the format, so no winner was picked.")
    print("(A small model is a rough judge: treat this as a hint, not a ruling.)")


def show(name, j, waited):
    print(f"--- {name} (after {waited:.0f}s)")
    if j["status"] == "failed":
        print(f"    FAILED: {j.get('error')}\n")
        return
    r = json.loads(j["result"] or "{}")
    text = (r.get("response") or "").strip() or "(empty answer)"
    tps = ""
    if r.get("eval_count") and r.get("eval_duration_ns"):
        tps = f", {r['eval_count'] / (r['eval_duration_ns'] / 1e9):.1f} tokens/s"
    print(f"    [{r.get('seconds', '?')}s{tps}]")
    print("    " + text.replace("\n", "\n    ") + "\n")


if __name__ == "__main__":
    main()
