"""Count gaps in the owner's streamed (cloned) voice: old playback (play each piece at once) vs jitter buffer (renderer/index.html).

Streams real replies from the voice service (127.0.0.1:8766), records when each piece arrives, then replays both
playback rules on those arrival times. --busy also has the LLM write an answer while each reply is spoken, like
Echo does. Usage: uv run python scripts/bench_voice_gaps.py [--busy] [replies]
"""
import http.client, json, sys, threading, time

REPLIES = [
    ["Well, good morning to you.", "I slept pretty good, thanks for asking, and the coffee is already on.",
     "What do you have going on at school today?"],
    ["Oh, I remember that trip.", "We drove all the way out there with the windows down, singing the whole way,",
     "and you laughed so hard at that sign we passed."],
    ["That sounds like a long day.", "Why don't you sit down for a minute and put your feet up,",
     "and tell me about your day, I always like hearing about it."],
    ["Sure thing.", "Let me think about that for a second, because I want to get it right for you."],
]
LEAD_MIN, LEAD_MAX, START = 0.7, 2.0, 0.06   # same numbers as renderer/index.html


def stream(text):
    """[(arrival seconds, piece seconds)] for one phrase, timed from the request."""
    c = http.client.HTTPConnection("127.0.0.1", 8766, timeout=60)
    t = time.perf_counter()
    c.request("POST", "/stream", json.dumps({"text": text}), {"Content-Type": "application/json"})
    r = c.getresponse(); rate = int(r.getheader("X-Sample-Rate") or 24000); out = []
    while chunk := r.read1(1 << 20):
        out.append((time.perf_counter() - t, len(chunk) / 2 / rate))
    c.close()
    return out


def busy_llm():
    c = http.client.HTTPConnection("127.0.0.1", 11434, timeout=120)
    c.request("POST", "/api/generate", json.dumps({"model": "llama3.1:8b", "stream": False, "keep_alive": "24h",
              "prompt": "Tell me about a nice afternoon on the porch.", "options": {"num_predict": 80}}))
    c.getresponse().read(); c.close()


def record(phrases, busy):
    """Phrases are requested one after another, like voicePump. Returns arrivals relative to the reply start."""
    th = threading.Thread(target=busy_llm) if busy else None
    if th: th.start()
    t0, pieces = time.perf_counter(), []
    for p in phrases:
        t = time.perf_counter() - t0
        pieces += [(t + a, d) for a, d in stream(p)]
    if th: th.join()
    return pieces


def old(pieces):
    nxt, first, gaps, gap_s = 0.0, None, 0, 0.0
    for arr, d in pieces:
        at = max(nxt, arr + START)
        if first is None: first = at
        elif at > nxt + 1e-3: gaps += 1; gap_s += at - nxt
        nxt = at + d
    return first, gaps, gap_s


def jitter(pieces, lead):
    nxt, first, gaps, gap_s, hold, started = 0.0, None, 0, 0.0, [], False
    def flush(now):
        nonlocal nxt, first, gap_s, hold, started
        for d in hold:
            after = nxt < now + START
            at = now + START if after else nxt
            if first is None: first = at
            elif after: gap_s += at - nxt
            nxt = at + d
        hold, started = [], True
    for arr, d in pieces:
        playing = nxt > arr + START
        if not playing and not hold and started: gaps += 1; lead = min(LEAD_MAX, lead + 0.3)
        hold.append(d)
        if playing or sum(hold) >= lead: flush(arr)
    if hold: flush(pieces[-1][0])
    return first, gaps, gap_s, (lead if gaps else max(LEAD_MIN, lead - 0.1))


busy = "--busy" in sys.argv
n = next((int(a) for a in sys.argv[1:] if a.isdigit()), len(REPLIES))
stream("Okay.")   # warm up
lead, tot = LEAD_MIN, {"old": [0, 0.0], "new": [0, 0.0]}
print(f"{'busy' if busy else 'quiet'} Mac, {n} replies\n reply  audio | old: first gaps gap_s | new: first gaps gap_s lead")
for i, phrases in enumerate((REPLIES * 3)[:n]):
    pieces = record(phrases, busy)
    f0, g0, s0 = old(pieces); f1, g1, s1, lead_next = jitter(pieces, lead)
    print(f" {i+1:>4} {sum(d for _, d in pieces):5.1f}s |  {f0:4.2f}s {g0:3d} {s0:5.2f}s | {f1:4.2f}s {g1:3d} {s1:5.2f}s {lead:.1f}")
    tot["old"][0] += g0; tot["old"][1] += s0; tot["new"][0] += g1; tot["new"][1] += s1; lead = lead_next
print(f"total gaps: old {tot['old'][0]} ({tot['old'][1]:.2f}s silence)  new {tot['new'][0]} ({tot['new'][1]:.2f}s silence)")
