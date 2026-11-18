"""Builds the Echo Mesh icon: a blue-to-purple brain with a recycle symbol (three chasing arrows) in the middle.
Writes icon.svg. (EchoMesh.ico and the tray PNG are made from it by build_icon_files.py.)"""
import math

CX = CY = 32.0
R = 9.6


def pt(angle_deg, r=R):
    a = math.radians(angle_deg)
    return CX + r * math.cos(a), CY + r * math.sin(a)


def arrow(start_deg):
    """One clockwise arc with an arrowhead at its end."""
    a0, a1 = start_deg, start_deg + 78
    x0, y0 = pt(a0); x1, y1 = pt(a1)
    arc = f"M{x0:.2f} {y0:.2f} A{R} {R} 0 0 1 {x1:.2f} {y1:.2f}"
    # arrowhead: a triangle at the end, pointing along the direction of travel
    tang = math.radians(a1 + 90)
    tip = (x1 + 4.6 * math.cos(tang), y1 + 4.6 * math.sin(tang))
    nx, ny = math.cos(math.radians(a1)), math.sin(math.radians(a1))
    b1 = (x1 + 3.6 * nx, y1 + 3.6 * ny); b2 = (x1 - 3.6 * nx, y1 - 3.6 * ny)
    head = f"M{tip[0]:.2f} {tip[1]:.2f} L{b1[0]:.2f} {b1[1]:.2f} L{b2[0]:.2f} {b2[1]:.2f} Z"
    return arc, head


arcs, heads = zip(*(arrow(a) for a in (-100, 20, 140)))
ARCS = "".join(f'<path d="{d}"/>' for d in arcs)
HEADS = "".join(f'<path d="{d}"/>' for d in heads)

SVG = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="Echo Mesh"><defs><linearGradient id="bp" gradientUnits="userSpaceOnUse" x1="4" y1="0" x2="60" y2="0"><stop offset="0" stop-color="#2563eb"/><stop offset=".48" stop-color="#6366f1"/><stop offset="1" stop-color="#9333ea"/></linearGradient></defs><path d="M30 8 C25 4 16 6 14 13 C8 13 4 20 7 26 C2 31 4 40 10 42 C10 49 17 55 24 53 C27 57 31 57 30 52 Z" fill="url(#bp)"/><path d="M 34 8 C 39 4 48 6 50 13 C 56 13 60 20 57 26 C 62 31 60 40 54 42 C 54 49 47 55 40 53 C 37 57 33 57 34 52 Z" fill="url(#bp)"/><g fill="none" stroke="#dbeafe" stroke-opacity=".55" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 13 C17 18 22 17 24 12"/><path d="M7 26 C12 28 16 25 15 20"/><path d="M10 42 C15 41 16 36 12 33"/><path d="M24 53 C21 48 24 44 29 45"/><path d="M 50 13 C 47 18 42 17 40 12"/><path d="M 57 26 C 52 28 48 25 49 20"/><path d="M 54 42 C 49 41 48 36 52 33"/><path d="M 40 53 C 43 48 40 44 35 45"/></g><g fill="none" stroke="#1e1b4b" stroke-opacity=".9" stroke-width="6.6" stroke-linecap="round" stroke-linejoin="round">{ARCS}</g><g fill="#1e1b4b" stroke="#1e1b4b" stroke-opacity=".9" stroke-width="3" stroke-linejoin="round">{HEADS}</g><g fill="none" stroke="#ffffff" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round">{ARCS}</g><g fill="#ffffff" stroke="#ffffff" stroke-width=".6" stroke-linejoin="round">{HEADS}</g></svg>
"""

if __name__ == "__main__":
    import os
    open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.svg"), "w").write(SVG)
    print("wrote icon.svg")
