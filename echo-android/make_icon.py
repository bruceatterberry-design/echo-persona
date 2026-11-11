import math, os
from PIL import Image, ImageDraw, ImageFont, ImageFilter
RES = "app/src/main/res"
def ring_layer(S, bg):
    img = Image.new("RGBA", (S, S), (13, 11, 9, 255) if bg else (0, 0, 0, 0))
    c = S / 2; r = S * 0.285; w = S * 0.034
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0)); g = ImageDraw.Draw(glow)
    g.ellipse([c - r * 1.15, c - r * 1.15, c + r * 1.15, c + r * 1.15], outline=(216, 178, 90, 120), width=max(1, int(w * 3)))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(S * 0.03)))
    ring = Image.new("RGBA", (S, S), (0, 0, 0, 0)); px = ring.load()
    for y in range(S):
        for x in range(S):
            d = math.hypot(x + .5 - c, y + .5 - c)
            a = max(0.0, 1 - abs(d - r) / (w / 2))
            if a <= 0: continue
            t = (math.atan2(y - c, x - c) + math.pi) / (2 * math.pi)
            k = 0.5 + 0.5 * math.cos(2 * math.pi * (t * 2 - 0.15))
            col = tuple(int(lo + (hi - lo) * k) for lo, hi in zip((154, 116, 40), (243, 212, 138)))
            px[x, y] = col + (int(255 * min(1, a * 1.6)),)
    img.alpha_composite(ring)
    f = ImageFont.truetype("/System/Library/Fonts/Optima.ttc", int(S * 0.13))
    d = ImageDraw.Draw(img); txt = "echo"
    bb = d.textbbox((0, 0), txt, font=f)
    d.text((c - (bb[0] + bb[2]) / 2, c - (bb[1] + bb[3]) / 2), txt, font=f, fill=(243, 212, 138, 255))
    return img
for name, dp in (("mdpi", 108), ("hdpi", 162), ("xhdpi", 216), ("xxhdpi", 324), ("xxxhdpi", 432)):
    os.makedirs(f"{RES}/mipmap-{name}", exist_ok=True)
    ring_layer(dp, False).save(f"{RES}/mipmap-{name}/ic_launcher_foreground.png")
os.makedirs(f"{RES}/mipmap-anydpi-v26", exist_ok=True)
for n in ("ic_launcher", "ic_launcher_round"):
    open(f"{RES}/mipmap-anydpi-v26/{n}.xml", "w").write('<?xml version="1.0" encoding="utf-8"?>\n<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n    <background android:drawable="@color/icon_bg" />\n    <foreground android:drawable="@mipmap/ic_launcher_foreground" />\n</adaptive-icon>\n')
open(f"{RES}/values/colors.xml", "w").write('<?xml version="1.0" encoding="utf-8"?>\n<resources><color name="icon_bg">#0D0B09</color></resources>\n')
ring_layer(432, True).save(os.environ["PREVIEW"])
