"""The Echo Mesh splash screen (the ring, the name and the tagline from the owner's dashboard picture). Drawn with the same small canvas interface as the
dashboard, so the preview picture and the real full-screen window are the same drawing. `draw_splash(c, w, h, sweep=360)` only draws."""
import math, random
from .dashboard import BG0, BG1, CYAN, BLUE, TEXT, DIM, GREEN, mix

TAGLINE = "Distributed AI. Infinite Possibilities."


def ring(c, cx, cy, r, sweep=360.0):
    """The glowing ring. `sweep` (degrees) lets the real window animate it drawing itself; 360 = complete."""
    for k in range(46, 0, -1):                                              # soft glow outside the ring
        c.arc(cx, cy, r + 6 + k, 0, sweep, 1, mix(BG1, CYAN, 0.018 * (47 - k) / 4.0))
    for k in range(-7, 8):                                                  # the ring itself: bright cyan centre, blue edges
        t = 1 - abs(k) / 8.0
        c.arc(cx, cy, r + k, 0, sweep, 1, mix(BLUE, "#aef3ff", t * t))
    for k in range(1, 22):                                                  # the faint inner shade
        c.arc(cx, cy, r - 8 - k, 0, sweep, 1, mix(BG1, BLUE, 0.16 * (1 - k / 22.0)))
    c.arc(cx, cy, r - 40, 20, min(sweep, 70), 2, mix(BG1, CYAN, 0.55))      # small highlights, like the picture's ring
    c.arc(cx, cy, r - 56, 200, min(sweep, 40), 2, mix(BG1, CYAN, 0.35))


def draw_splash(c, w, h, sweep=360.0, status="Starting Echo Mesh setup"):
    for i in range(48):                                                     # deep blue background, lighter toward the middle
        t = 1 - abs(i - 24) / 24.0
        c.rect(0, i * h / 48.0, w, h / 48.0 + 1, mix(BG0, "#0b1f48", 0.15 + 0.85 * t * t))
    rnd = random.Random(7)                                                  # a faint network of dots and lines, always the same
    pts = [(rnd.uniform(0.04, 0.96) * w, rnd.uniform(0.05, 0.95) * h) for _ in range(34)]
    for i, (x1, y1) in enumerate(pts):
        for x2, y2 in pts[i + 1:]:
            d = math.hypot(x1 - x2, y1 - y2)
            if d < 0.17 * w:
                c.line(x1, y1, x2, y2, 1, mix(BG0, BLUE, 0.16 * (1 - d / (0.17 * w))))
    for x, y in pts:
        c.circle(x, y, 2.4, mix(BG0, CYAN, 0.45))
    for k in range(9):                                                      # light swoosh in the lower left, like the picture
        c.arc(-0.08 * w, 1.05 * h, 0.30 * w + k * 16, 8, 62, 1, mix(BG0, BLUE, 0.10 + 0.05 * k))
    cx, cy, r = w / 2, h * 0.36, h * 0.155
    ring(c, cx, cy, r, sweep)
    c.text(cx, cy + r + h * 0.115, "E C H O   M E S H", int(h * 0.105), TEXT, True, "center")
    c.text(cx, cy + r + h * 0.215, TAGLINE, int(h * 0.031), DIM, False, "center")
    bw = w * 0.16                                                            # a thin line that fills while the installer wakes up
    c.line(cx - bw / 2, h * 0.86, cx + bw / 2, h * 0.86, 3, mix(BG0, BLUE, 0.28))
    c.line(cx - bw / 2, h * 0.86, cx - bw / 2 + bw * min(1.0, sweep / 360.0), h * 0.86, 3, CYAN)
    c.text(cx, h * 0.90, status, int(h * 0.018), DIM, False, "center")
    c.text(w - 28, h - 24, "Together we think deeper.", int(h * 0.016), mix(BG0, DIM, 0.8), False, "e")
