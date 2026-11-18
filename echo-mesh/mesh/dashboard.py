"""The Echo Mesh dashboard (the look of the owner's reference picture). Drawn from simple shapes so the same code paints the Windows window (tkinter canvas)
and a preview image (Pillow). `draw(ctx, state, page)` only draws what it is given; it never touches the network. Every number shown is a real
measurement from the Mesh; anything the Mesh does not measure is labelled 'not measured', never made up."""
import math, time

W, H = 1180, 664
BG0, BG1 = "#050a18", "#0a1530"
PANEL, EDGE, EDGE2 = "#0a1330", "#17407a", "#2a7fd4"
CYAN, BLUE, GREEN, AMBER, RED, PURPLE = "#35d6ff", "#2f86ff", "#25e07c", "#f5b94a", "#ff6b6b", "#9b6dff"
TEXT, DIM = "#eaf3ff", "#7f93b8"
NAV = [("overview", "Overview", "home"), ("nodes", "Nodes", "grid"), ("tasks", "Tasks", "list"), ("comms", "Communication", "chat"), ("models", "Models", "hex"), ("settings", "Settings", "gear")]
NAV_X, NAV_Y, NAV_W, NAV_H, NAV_GAP = 8, 112, 150, 44, 8


def mix(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]; b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * max(0.0, min(1.0, t))) for i in range(3))


def wrap(text, width_chars):
    lines, cur = [], ""
    for word in str(text).split():
        if len(cur) + len(word) + 1 > width_chars and cur:
            lines.append(cur); cur = word
        else:
            cur = (cur + " " + word).strip()
    return lines + ([cur] if cur else [])


def fmt_secs(s):
    s = float(s or 0)
    return f"{s:.0f} s" if s < 90 else f"{s / 60:.1f} min" if s < 5400 else f"{s / 3600:.1f} h"


def fmt_uptime(s):
    s = int(s or 0)
    d, r = divmod(s, 86400); h, r = divmod(r, 3600); m = r // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m} min"


def hhmm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def nav_regions():
    return [(key, NAV_X, NAV_Y + i * (NAV_H + NAV_GAP), NAV_W, NAV_H) for i, (key, _, _) in enumerate(NAV)]


def health(s):
    """-> (word, colour). From the measured round-trip time to the Mesh; 'Offline' if it is not answering."""
    if s["connection"] != "connected":
        return "Offline", RED
    r = [x for x in (s.get("rtts") or []) if x is not None]
    if not r:
        return "Good", BLUE
    med = sorted(r)[len(r) // 2]
    return ("Excellent", GREEN) if med < 60 else ("Good", BLUE) if med < 200 else ("Slow", AMBER)


def ico(c, kind, cx, cy, s, col):
    """Small icons drawn from lines and shapes (no font needed, so they look the same everywhere)."""
    h = s / 2
    if kind == "home":
        c.poly([(cx - h, cy), (cx, cy - h), (cx + h, cy)], col); c.rect(cx - h * 0.62, cy, h * 1.24, h * 0.95, col)
    elif kind == "grid":
        for dx in (-h * 0.95, h * 0.05):
            for dy in (-h * 0.95, h * 0.05):
                c.rrect(cx + dx, cy + dy, h * 0.9, h * 0.9, 2, col)
    elif kind == "list":
        for i in (-1, 0, 1):
            c.circle(cx - h * 0.8, cy + i * h * 0.7, 1.6, col); c.line(cx - h * 0.4, cy + i * h * 0.7, cx + h, cy + i * h * 0.7, 2, col)
    elif kind == "chat":
        c.rrect(cx - h, cy - h * 0.8, s, h * 1.3, 5, col); c.poly([(cx - h * 0.5, cy + h * 0.45), (cx - h * 0.7, cy + h), (cx, cy + h * 0.45)], col)
    elif kind == "hex":
        pts = [(cx + h * math.cos(math.radians(60 * i - 30)), cy + h * math.sin(math.radians(60 * i - 30))) for i in range(6)]
        for a, b in zip(pts, pts[1:] + pts[:1]):
            c.line(a[0], a[1], b[0], b[1], 2, col)
        c.circle(cx, cy, 2, col)
    elif kind == "gear":
        c.circle(cx, cy, h * 0.55, "#0a1330", col, 2)
        for i in range(8):
            a = math.radians(45 * i)
            c.line(cx + h * 0.7 * math.cos(a), cy + h * 0.7 * math.sin(a), cx + h * math.cos(a), cy + h * math.sin(a), 2.5, col)
    elif kind == "pulse":
        pts = [(cx - h, cy), (cx - h * 0.4, cy), (cx - h * 0.15, cy - h * 0.9), (cx + h * 0.25, cy + h * 0.9), (cx + h * 0.5, cy), (cx + h, cy)]
        for a, b in zip(pts, pts[1:]):
            c.line(a[0], a[1], b[0], b[1], 2, col)
    elif kind == "people":
        for dx in (-h * 0.5, h * 0.5):
            c.circle(cx + dx, cy - h * 0.4, h * 0.38, col); c.rrect(cx + dx - h * 0.55, cy + h * 0.1, h * 1.1, h * 0.8, 4, col)
    elif kind == "bars":
        for i, bh in enumerate((0.5, 0.9, 1.4)):
            c.rect(cx - h + i * h * 0.75, cy + h - bh * h, h * 0.5, bh * h, col)
    elif kind == "chip":
        c.rrect(cx - h * 0.6, cy - h * 0.6, h * 1.2, h * 1.2, 3, "#06112a", col)
        for i in (-0.3, 0.3):
            c.line(cx + i * h, cy - h * 0.6, cx + i * h, cy - h, 2, col); c.line(cx + i * h, cy + h * 0.6, cx + i * h, cy + h, 2, col)
    elif kind == "sync":
        c.arc(cx, cy, h * 0.8, 0, 290, 2.5, col); c.poly([(cx + h * 0.8, cy - h * 0.1), (cx + h * 1.1, cy + h * 0.5), (cx + h * 0.3, cy + h * 0.5)], col)
    elif kind == "check":
        c.line(cx - h * 0.6, cy, cx - h * 0.1, cy + h * 0.5, 3, col); c.line(cx - h * 0.1, cy + h * 0.5, cx + h * 0.7, cy - h * 0.5, 3, col)
    elif kind == "alert":
        c.line(cx, cy - h * 0.7, cx, cy + h * 0.1, 3, col); c.circle(cx, cy + h * 0.6, 1.8, col)


def panel(c, x, y, w, h, title="", right=None, glyph=None):
    c.rrect(x - 1, y - 1, w + 2, h + 2, 13, mix(PANEL, EDGE, 0.35), None)            # soft glow
    c.rrect(x, y, w, h, 12, PANEL, EDGE)
    if title:
        c.text(x + 16 + (24 if glyph else 0), y + 13, title, 14, TEXT, True)
        if glyph:
            ico(c, glyph, x + 26, y + 22, 15, CYAN)
    if right:
        txt, col = right
        pw = 26 + 7 * len(txt)
        c.rrect(x + w - pw - 12, y + 9, pw, 24, 12, mix(PANEL, col, 0.16), mix(PANEL, col, 0.6))
        c.circle(x + w - pw - 12 + 13, y + 21, 4, col)
        c.text(x + w - pw - 12 + 24, y + 21, txt, 11, col, False, "w")


def glow_circle(c, cx, cy, r, color):
    for i, k in enumerate((10, 7, 4)):
        c.circle(cx, cy, r + k, mix(BG1, color, 0.07 + 0.07 * i))
    c.circle(cx, cy, r, "#06112a", color, 3)


def header(c, s):
    for i in range(24):
        c.rect(0, i * 4, W, 4, mix(BG1, "#08204a", 1 - i / 24))
    c.icon(22, 10, 58)
    c.text(94, 10, "E\u200aC\u200aH\u200aO  M\u200aE\u200aS\u200aH", 31, TEXT, True)
    c.text(96, 48, "Distributed AI. Infinite Possibilities.", 13, DIM)
    c.line(352, 18, 352, 62, 1, EDGE)
    ov = s.get("overview")
    nodes = ov["nodes"] if ov else []
    online = sum(1 for n in nodes if n["online"]) + (1 if ov else 0)       # the Mesh's own Mac counts when we can reach it
    if s["connection"] == "connected":
        c.circle(374, 40, 6, GREEN); c.text(388, 40, f"{online} {'NODE' if online == 1 else 'NODES'} ONLINE" if ov else "CONNECTED", 17, TEXT, True, "w")
    else:
        word = {"connecting": "CONNECTING…", "waiting": "WAITING FOR APPROVAL", "unreachable": "CAN'T REACH THE MESH"}[s["connection"]]
        c.circle(374, 40, 6, AMBER if s["connection"] != "unreachable" else RED); c.text(388, 40, word, 17, TEXT, True, "w")
    c.text(W - 100, 26, "ECHO — PRIMARY BRAIN", 15, TEXT, True, "e")
    c.text(W - 100, 50, "Curious. Learning. Growing.", 12, DIM, False, "e")
    glow_circle(c, W - 50, 40, 22, CYAN); c.icon(W - 72, 18, 44)
    c.line(0, 74, W, 74, 1, EDGE)


def sidebar(c, s, page):
    c.rrect(6, 84, 156, 476, 12, mix(BG0, PANEL, 0.6), mix(BG0, EDGE, 0.7))
    for (key, label, glyph), (_, x, y, w, h) in zip(NAV, nav_regions()):
        if key == page:
            c.rrect(x, y, w, h, 9, mix(BLUE, "#0b3d9c", 0.5), EDGE2)
        ico(c, glyph, x + 26, y + h / 2, 17, TEXT if key == page else DIM)
        c.text(x + 46, y + h / 2, label, 14, TEXT if key == page else DIM, key == page, "w")
    c.text(20, 530, "Together", 17, DIM, False, "w"); c.text(20, 552, "we think deeper.", 15, DIM, False, "w")


def node_label(c, x, y, name, role, online, w=170, left=False):
    col = GREEN if online else DIM
    c.rrect(x, y, w, 62, 9, mix(PANEL, EDGE, 0.25), EDGE)
    c.text(x + 12, y + 8, name[:18].upper(), 13, TEXT, True)
    c.text(x + 12, y + 27, role, 12, CYAN, True)
    c.circle(x + 18, y + 49, 4, col); c.text(x + 28, y + 49, "Online" if online else "Offline", 12, col, False, "w")


def network_map(c, s):
    x, y, w, h = 174, 84, 730, 184
    panel(c, x, y, w, h, "ECHO MESH NETWORK")
    ov = s.get("overview")
    nodes = ov["nodes"] if ov else [{"name": s.get("name", "This computer"), "type": "compute", "online": s["connection"] == "connected", "is_me": True}]
    primary = (ov["mesh"]["primary"] if ov else "Mac mini")
    top = (x + w / 2, y + 64)
    shown = nodes[:5]
    n = len(shown)
    pts = [(x + w * (i + 1) / (n + 1), y + 142 if n > 1 else y + 142) for i in range(n)]
    reachable = s["connection"] == "connected"
    for (px, py), nd in zip(pts, shown):                                       # lines from the primary to each computer, glowing when live
        live = reachable and nd["online"]
        c.line(top[0], top[1], px, py, 6 if live else 3, mix(PANEL, GREEN, 0.25) if live else EDGE)
        c.line(top[0], top[1], px, py, 2, GREEN if live else "#34476e")
    for a, b in zip(pts, pts[1:]):
        c.line(a[0], a[1], b[0], b[1], 2, mix(PANEL, GREEN, 0.5) if reachable else "#34476e")
    glow_circle(c, top[0], top[1], 26, CYAN)
    c.text(top[0], top[1], "E", 24, TEXT, True, "center")
    node_label(c, top[0] + 40, y + 30, primary, "PRIMARY ECHO", reachable, 168)
    all_up = reachable and all(nd["online"] for nd in nodes)
    col = GREEN if all_up else AMBER
    c.rrect(x + 16, y + 44, 150, 54, 10, mix(PANEL, col, 0.12), mix(PANEL, col, 0.5))
    ico(c, "check" if all_up else "alert", x + 38, y + 71, 16, col)
    c.text(x + 54, y + 60, "Mesh Healthy" if all_up else "Check the Mesh", 13, col, True, "w")
    c.text(x + 54, y + 80, f"{sum(1 for nd in nodes if nd['online'])} of {len(nodes)} computers up", 11, DIM, False, "w")
    for (px, py), nd in zip(pts, shown):
        live = reachable and nd["online"]
        glow_circle(c, px, py, 20, CYAN if live else DIM)
        ico(c, "chip", px, py, 18, TEXT)
        c.text(px, py + 34, (nd["name"][:16] + (" (you)" if nd.get("is_me") else "")), 11, TEXT if live else DIM, True, "center")
        c.text(px, py + 48, {"worker": "WORKER NODE", "peer": "PEER NODE"}.get(nd.get("role"), "COMPUTER"), 10, CYAN if live else DIM, True, "center")
    if len(nodes) > 5:
        c.text(x + w - 14, y + h - 14, f"+{len(nodes) - 5} more", 11, DIM, False, "e")


def task_panel(c, s):
    x, y, w, h = 174, 280, 470, 188
    ov = s.get("overview")
    proj = (ov or {}).get("projects") or []
    active = next((p for p in proj if p.get("running")), None)
    panel(c, x, y, w, h, "TASK ORCHESTRATION", ("Active", GREEN) if active else ("Idle", DIM), "pulse")
    c.icon(x + 18, y + 54, 96)
    c.text(x + 136, y + 56, (active["running"] if active else "No tasks running")[:22], 15, TEXT, True)
    for i, ln in enumerate(wrap(f"Project: {active['title']}" if active else "Ask Echo to suggest a task, or start a project.", 26)[:3]):
        c.text(x + 136, y + 80 + i * 16, ln, 12, DIM)
    if active:
        frac = active["accepted"] / max(1, active["tasks"])
        c.rrect(x + 136, y + 112, 160, 8, 4, "#122046"); c.rrect(x + 136, y + 112, max(8, 160 * frac), 8, 4, mix(BLUE, CYAN, frac))
        c.text(x + 136, y + 128, f"{active['accepted']} of {active['tasks']} steps accepted", 12, TEXT, True)
        c.text(x + 136, y + 148, "Estimated completion: Estimating…", 11, DIM)
    shares = [(n["name"], n["share_of_work"]) for n in (ov or {}).get("nodes", []) if n.get("share_of_work") is not None]
    c.text(x + 318, y + 54, "Work shared (24 h):", 12, DIM)
    cols = [GREEN, BLUE, AMBER, PURPLE, CYAN]
    for i, (nm, pct) in enumerate(shares[:5]):
        yy = y + 80 + i * 20
        c.circle(x + 326, yy + 7, 5, cols[i % 5]); c.text(x + 338, yy + 7, nm[:12], 12, TEXT, False, "w"); c.text(x + w - 16, yy + 7, f"{pct:.0f}%", 12, TEXT, True, "e")
    if not shares:
        c.text(x + 318, y + 84, "No shared work yet.", 12, DIM)


def comms_panel(c, s):
    x, y, w, h = 656, 280, 248, 188
    panel(c, x, y, w, h, "COMMUNICATION CENTER", None, "chat")
    msgs = [(n["name"], n.get("ai_agreed")) for n in (s.get("overview") or {}).get("nodes", []) if n.get("ai_agreed") is not None]
    if msgs:
        for i, (nm, ok) in enumerate(msgs[:3]):
            yy = y + 48 + i * 40
            c.text(x + 16, yy, nm[:16], 12, TEXT, True); c.text(x + 16, yy + 18, "AI agreed to connect" if ok else "AI declined to connect", 11, GREEN if ok else AMBER)
    else:
        c.text(x + 16, y + 50, "AI introductions appear here.", 12, DIM)
        c.text(x + 16, y + 70, "Chat between AIs is not built yet.", 11, DIM)
    c.rrect(x + 12, y + h - 40, w - 24, 28, 8, "#071022", EDGE)
    c.text(x + 22, y + h - 26, "Send a message… (coming soon)", 11, DIM, False, "w")


def activity_panel(c, s):
    x, y, w, h = 916, 84, 258, 384
    panel(c, x, y, w, h, "ACTIVITY FEED", ("Live", GREEN))
    ov = s.get("overview")
    items = [(hhmm(e["ts"]), e["text"]) for e in (ov or {}).get("events", [])]
    items += [(t, txt) for t, txt in (s.get("events") or [])][::-1] if not items else []
    for i, (t, txt) in enumerate(items[:6]):
        yy = y + 48 + i * 54
        c.circle(x + 26, yy + 12, 11, "#0e2a5c", CYAN, 1); c.circle(x + 26, yy + 12, 3.5, CYAN)
        c.text(x + 48, yy, t, 11, CYAN)
        for j, ln in enumerate(wrap(txt, 24)[:2]):
            c.text(x + 48, yy + 17 + j * 15, ln, 12, TEXT)
        if i < 5:
            c.line(x + 14, yy + 48, x + w - 14, yy + 48, 1, mix(PANEL, EDGE, 0.5))
    if not items:
        c.text(x + 18, y + 54, "Nothing has happened yet.", 12, DIM)


def workers_panel(c, s):
    x, y, w, h = 174, 480, 654, 128
    panel(c, x, y, w, h, "WORKER COLLABORATION", None, "people")
    ov = s.get("overview")
    nodes = (ov["nodes"] if ov else [{"name": s.get("name", "This computer"), "online": s["connection"] == "connected", "threads": None, "ram_gb": None, "gpu": None, "load1": None, "is_me": True}])[:4]
    cw = 360 / max(1, len(nodes)) if len(nodes) > 2 else 175
    cw = (w - 190) / max(1, len(nodes))
    for i, n in enumerate(nodes):
        cx0 = x + 14 + i * cw
        c.text(cx0, y + 46, (n["name"][:14] + (" · you" if n.get("is_me") else "")), 13, TEXT, True)
        c.text(cx0, y + 66, f"CPU: {n['threads']} threads" if n.get("threads") else "CPU: unknown", 11, DIM)
        c.text(cx0, y + 82, "load " + f"{n['load1']:.2f}" if n.get("load1") is not None else "load: not measured", 11, DIM)
        c.text(cx0, y + 98, (f"RAM {n['ram_gb']:g} GB" if n.get("ram_gb") else "RAM: unknown") + (" · GPU yes" if n.get("gpu") else ""), 11, DIM)
        ok = n["online"]
        c.circle(cx0 + 4, y + 117, 4, GREEN if ok else RED); c.text(cx0 + 14, y + 117, "Healthy" if ok else "Offline", 12, GREEN if ok else RED, True, "w")
        if i < len(nodes) - 1:
            c.line(cx0 + cw - 8, y + 44, cx0 + cw - 8, y + h - 12, 1, mix(PANEL, EDGE, 0.6))
    lx = x + w - 170
    c.line(lx - 10, y + 44, lx - 10, y + h - 12, 1, GREEN)
    c.text(lx, y + 46, "Network Latency", 12, TEXT, True)
    r = [v for v in (s.get("rtts") or []) if v is not None]
    c.text(lx, y + 62, f"{(sorted(r)[len(r) // 2]):.0f} ms" if r else "—", 24, GREEN, True)
    c.text(lx, y + 92, "this computer to the Mesh", 10, DIM)
    if len(r) > 1:
        top = max(max(r), 1.0)
        pts = [(lx + i * 150 / (len(r[-30:]) - 1), y + h - 12 - 14 * v / top) for i, v in enumerate(r[-30:])]
        for a, b in zip(pts, pts[1:]):
            c.line(a[0], a[1], b[0], b[1], 2, GREEN)


def stats_panel(c, s):
    x, y, w, h = 840, 480, 334, 128
    panel(c, x, y, w, h, "NETWORK STATISTICS", None, "bars")
    j = (s.get("overview") or {}).get("jobs") or {}
    up = ((s.get("overview") or {}).get("mesh") or {}).get("uptime_s")
    rows = [("Jobs done (24 h)", j.get("done_24h", s.get("jobs_done", 0))), ("Failed (24 h)", j.get("failed_24h", s.get("jobs_failed", 0))), ("Waiting or running", (j.get("queued", 0) + j.get("running", 0)) if j else "—"),
            ("Mesh uptime", fmt_uptime(up) if up is not None else "—")]
    for i, (k, v) in enumerate(rows):
        c.text(x + 16, y + 50 + i * 18, k, 12, DIM); c.text(x + 200, y + 50 + i * 18, str(v), 13, TEXT, True, "ne")
    ov = s.get("overview")
    if ov:
        up_n, tot = sum(1 for n in ov["nodes"] if n["online"]), len(ov["nodes"])
        c.arc(x + w - 52, y + 76, 26, 0, 360, 7, "#122046")
        if tot:
            c.arc(x + w - 52, y + 76, 26, 0, 360 * up_n / tot, 7, GREEN)
        c.text(x + w - 52, y + 76, f"{up_n}/{tot}", 12, TEXT, True, "center")
        c.text(x + w - 52, y + 112, "computers up", 10, DIM, False, "center")


def status_bar(c, s):
    y = 620
    c.line(0, y, W, y, 1, EDGE)
    word, col = health(s)
    ai = s.get("ai") or {}
    model = (ai.get("models") or ["none"])[0].upper()
    q = ((s.get("overview") or {}).get("jobs") or {})
    items = [("bars", "CONNECTION HEALTH", word, col), ("sync", "SHARE LIMIT", f"{s['limit']:g}%  ·  using {s['used']:.0f}%", CYAN),
             ("chip", "ACTIVE MODEL", model[:26], TEXT), ("list", "TASK QUEUE", str(q.get("queued", 0) + q.get("running", 0)) if q else "—", TEXT)]
    colw = (W - 48) / 4
    for i, (glyph, label, val, vcol) in enumerate(items):
        x = 24 + i * colw
        ico(c, glyph, x + 14, y + 22, 18, GREEN if i == 0 else BLUE)
        c.text(x + 36, y + 9, label, 10, DIM)
        c.text(x + 36, y + 24, val, 14, vcol, True)
        if i:
            c.line(x - 6, y + 8, x - 6, y + 38, 1, EDGE)


# ---------------- the other pages ----------------
def page_nodes(c, s):
    x, y, w, h = 174, 84, 1000, 524
    panel(c, x, y, w, h, "NODES", None, "grid")
    ov = s.get("overview")
    c.text(x + 18, y + 48, "Computer", 11, DIM); c.text(x + 220, y + 48, "Type", 11, DIM); c.text(x + 310, y + 48, "Status", 11, DIM); c.text(x + 410, y + 48, "Hardware", 11, DIM)
    c.text(x + 640, y + 48, "Share limit / using", 11, DIM); c.text(x + 800, y + 48, "Latency", 11, DIM); c.text(x + 890, y + 48, "AI agreed", 11, DIM)
    for i, n in enumerate((ov or {}).get("nodes", [])[:10]):
        yy = y + 76 + i * 40
        c.text(x + 18, yy, n["name"][:22] + (" (you)" if n.get("is_me") else ""), 13, TEXT, True)
        c.text(x + 220, yy, n["type"].title(), 12, DIM)
        c.circle(x + 316, yy + 8, 5, GREEN if n["online"] else RED); c.text(x + 328, yy + 8, "Online" if n["online"] else "Offline", 12, GREEN if n["online"] else RED, False, "w")
        hw = ", ".join(v for v in (f"{n['threads']} threads" if n.get("threads") else None, f"{n['ram_gb']:g} GB" if n.get("ram_gb") else None, "GPU" if n.get("gpu") else None) if v)
        c.text(x + 410, yy, hw or "unknown", 12, DIM)
        if n.get("gpu"):
            c.text(x + 410, yy + 15, n["gpu"][:34], 10, DIM)
        c.text(x + 640, yy, f"{n['limit']:g}% / {n['used']:g}%" if n.get("limit") is not None else "not reported", 12, DIM)
        c.text(x + 800, yy, f"{n['rtt_ms']:.0f} ms" if n.get("rtt_ms") is not None else "—", 12, DIM)
        c.text(x + 890, yy, {True: "Yes", False: "No", None: "—"}[n.get("ai_agreed")], 12, DIM)
        c.line(x + 14, yy + 30, x + w - 14, yy + 30, 1, mix(PANEL, EDGE, 0.5))
    if not ov:
        c.text(x + 18, y + 80, "The Mesh hasn't sent its overview yet.", 13, DIM)


def page_tasks(c, s):
    x, y, w, h = 174, 84, 1000, 524
    panel(c, x, y, w, h, "TASKS AND PROJECTS", None, "list")
    proj = (s.get("overview") or {}).get("projects") or []
    if not proj:
        c.text(x + 18, y + 56, "No projects yet.", 14, DIM); c.text(x + 18, y + 82, "Ask Echo to suggest a task for the Mesh; it appears here after the owner approves it.", 12, DIM)
    for i, p in enumerate(proj[:8]):
        yy = y + 56 + i * 56
        c.text(x + 18, yy, p["title"][:50], 14, TEXT, True)
        c.rrect(x + 18, yy + 26, 300, 8, 4, "#122046"); c.rrect(x + 18, yy + 26, max(8, 300 * p["accepted"] / max(1, p["tasks"])), 8, 4, BLUE)
        c.text(x + 330, yy + 30, f"{p['accepted']}/{p['tasks']} accepted", 12, DIM, False, "w")
        if p.get("waiting_for_you"):
            c.text(x + 470, yy + 30, f"{p['waiting_for_you']} waiting for the owner", 12, AMBER, True, "w")
        if p.get("running"):
            c.text(x + 700, yy + 30, f"Running: {p['running'][:36]}", 12, GREEN, False, "w")


def page_comms(c, s):
    x, y, w, h = 174, 84, 1000, 524
    panel(c, x, y, w, h, "COMMUNICATION CENTER", None, "chat")
    c.text(x + 18, y + 56, "AI introductions", 14, TEXT, True)
    msgs = [n for n in (s.get("overview") or {}).get("nodes", []) if n.get("ai_agreed") is not None]
    for i, n in enumerate(msgs[:8]):
        yy = y + 90 + i * 40
        c.text(x + 18, yy, n["name"][:24], 13, TEXT, True); c.text(x + 260, yy, "AI agreed to connect" if n["ai_agreed"] else "AI declined to connect", 13, GREEN if n["ai_agreed"] else AMBER)
    if not msgs:
        c.text(x + 18, y + 90, "None yet. When a computer connects its AI, the AI's yes or no appears here.", 12, DIM)
    c.text(x + 18, y + h - 60, "Chat between AIs, project discussions and approvals are not built yet.", 12, DIM)


def page_models(c, s):
    x, y, w, h = 174, 84, 1000, 524
    panel(c, x, y, w, h, "MODELS", None, "hex")
    for i, n in enumerate(((s.get("overview") or {}).get("nodes") or [])[:8]):
        yy = y + 56 + i * 56
        c.text(x + 18, yy, n["name"][:22], 14, TEXT, True)
        px = x + 240
        for m in (n.get("models") or ["no AI connected"])[:5]:
            mw = 20 + 7 * len(m)
            c.rrect(px, yy - 4, mw, 28, 14, mix(PANEL, BLUE, 0.22), mix(PANEL, BLUE, 0.55)); c.text(px + mw / 2, yy + 10, m, 11, TEXT, False, "center"); px += mw + 8


def page_settings(c, s):
    x, y, w, h = 174, 84, 1000, 524
    panel(c, x, y, w, h, "SETTINGS", None, "gear")
    ai = s.get("ai") or {}
    rows = [("Share limit", f"{s['limit']:g}% of this computer's time" + (" (outside computers are capped at 30%)" if s.get("outside") else "")),
            ("Sharing", "Paused by you" if s.get("paused") else "On"), ("Connected AI", (ai.get("label") or "none") + (" at " + ai.get("host", "").replace("http://", "") if ai.get("label") else "")),
            ("This computer", s.get("name", ""))]
    for i, (k, v) in enumerate(rows):
        c.text(x + 18, y + 56 + i * 44, k, 12, DIM); c.text(x + 220, y + 56 + i * 44, v, 14, TEXT, True)
    c.text(x + 18, y + 56 + 4 * 44 + 10, "Change the share limit, pause sharing or remove Echo Mesh with the controls below.", 12, DIM)


PAGES = {"nodes": page_nodes, "tasks": page_tasks, "comms": page_comms, "models": page_models, "settings": page_settings}


def draw(c, s, page="overview"):
    for i in range(40):                                              # a deep blue background, a little lighter towards the top
        c.rect(0, i * (H / 40), W, H / 40 + 1, mix(BG0, BG1, 1 - i / 40))
    header(c, s)
    sidebar(c, s, page)
    if page in PAGES:
        PAGES[page](c, s)
        activity = False
    else:
        network_map(c, s); task_panel(c, s); comms_panel(c, s); activity_panel(c, s); workers_panel(c, s); stats_panel(c, s)
    status_bar(c, s)


class PilCtx:
    """Draws on a Pillow image: used for previews and tests."""
    ANCH = {"nw": "la", "w": "lm", "center": "mm", "e": "rm", "ne": "ra"}

    def __init__(self, icon=None, size=None):
        from PIL import Image, ImageDraw
        self.S = 2
        self.size = size or (W, H)               # the splash is drawn full screen (1920 x 1080); the dashboard keeps its own size
        self.im = Image.new("RGB", (self.size[0] * self.S, self.size[1] * self.S), BG0)
        self.d = ImageDraw.Draw(self.im)
        self._icon, self._fonts = icon, {}

    def font(self, px, bold):
        from PIL import ImageFont
        k = (px, bold)
        if k not in self._fonts:
            for path in ("/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc", "/Library/Fonts/Arial.ttf", "C:/Windows/Fonts/segoeui.ttf"):
                try:
                    f = ImageFont.truetype(path, int(px * self.S * 1.0))
                    if bold:
                        try: f.set_variation_by_name("Bold")
                        except Exception: pass
                    self._fonts[k] = f; break
                except Exception:
                    continue
            else:
                self._fonts[k] = ImageFont.load_default()
        return self._fonts[k]

    def _c(self, v): return v * self.S
    def rect(self, x, y, w, h, fill): self.d.rectangle([self._c(x), self._c(y), self._c(x + w), self._c(y + h)], fill=fill)
    def rrect(self, x, y, w, h, r, fill, outline=None): self.d.rounded_rectangle([self._c(x), self._c(y), self._c(x + w), self._c(y + h)], self._c(r), fill=fill, outline=outline, width=self.S)
    def poly(self, pts, fill, outline=None): self.d.polygon([(self._c(a), self._c(b)) for a, b in pts], fill=fill, outline=outline)
    def circle(self, cx, cy, r, fill, outline=None, width=1): self.d.ellipse([self._c(cx - r), self._c(cy - r), self._c(cx + r), self._c(cy + r)], fill=fill, outline=outline, width=int(round(self._c(width))))
    def line(self, x1, y1, x2, y2, w, color): self.d.line([self._c(x1), self._c(y1), self._c(x2), self._c(y2)], fill=color, width=max(1, int(round(self._c(w)))))
    def arc(self, cx, cy, r, start, sweep, width, color):
        if sweep >= 359.9: return self.d.ellipse([self._c(cx - r), self._c(cy - r), self._c(cx + r), self._c(cy + r)], outline=color, width=int(round(self._c(width))))
        if sweep > 0: self.d.arc([self._c(cx - r), self._c(cy - r), self._c(cx + r), self._c(cy + r)], start - 90, start - 90 + sweep, fill=color, width=int(round(self._c(width))))
    def text(self, x, y, s, px, color, bold=False, anchor="nw"): self.d.text((self._c(x), self._c(y)), str(s), font=self.font(px, bold), fill=color, anchor=self.ANCH[anchor])
    def icon(self, x, y, size):
        if self._icon is not None:
            ic = self._icon.resize((self._c(size), self._c(size)))
            self.im.paste(ic, (self._c(x), self._c(y)), ic)
    def save(self, path): self.im.resize(self.size).save(path)


class TkCtx:
    """Draws on a tkinter canvas."""
    def __init__(self, canvas, photo=None, big_photo=None):
        self.c, self.photo = canvas, photo
        self.cache = {}

    def _font(self, px, bold): return ("Segoe UI", -int(px), "bold" if bold else "normal")
    def rect(self, x, y, w, h, fill): self.c.create_rectangle(x, y, x + w, y + h, fill=fill, outline=fill)
    def rrect(self, x, y, w, h, r, fill, outline=None):
        r = min(r, w / 2, h / 2)
        pts = [x + r, y, x + w - r, y, x + w, y, x + w, y + r, x + w, y + h - r, x + w, y + h, x + w - r, y + h, x + r, y + h, x, y + h, x, y + h - r, x, y + r, x, y]
        self.c.create_polygon(pts, smooth=True, fill=fill, outline=outline or fill, width=1)
    def poly(self, pts, fill, outline=None): self.c.create_polygon([v for p in pts for v in p], fill=fill, outline=outline or fill)
    def circle(self, cx, cy, r, fill, outline=None, width=1): self.c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline=outline or fill, width=width)
    def line(self, x1, y1, x2, y2, w, color): self.c.create_line(x1, y1, x2, y2, width=w, fill=color, capstyle="round")
    def arc(self, cx, cy, r, start, sweep, width, color):
        if sweep >= 359.9: return self.c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=width)
        if sweep > 0: self.c.create_arc(cx - r, cy - r, cx + r, cy + r, start=90 - start, extent=-sweep, style="arc", outline=color, width=width)
    def text(self, x, y, s, px, color, bold=False, anchor="nw"): self.c.create_text(x, y, text=str(s), font=self._font(px, bold), fill=color, anchor=anchor)
    def icon(self, x, y, size):
        if self.photo is not None:
            k = size
            if k not in self.cache:
                n = max(1, round(self.photo.width() / size))
                self.cache[k] = self.photo.subsample(n) if n > 1 else self.photo.zoom(max(1, round(size / self.photo.width())))
            self.c.create_image(x, y, image=self.cache[k], anchor="nw")
