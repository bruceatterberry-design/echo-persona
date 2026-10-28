"""Turn LivePortrait renders driven by the owner's own videos into seamless idle loops for the window.

Input:  derived/face/motion-v1/raw-<name>/source-working--<name>.mp4  (full frame, same geometry as the atlas)
Output: derived/face/motion-v1/idle-<name>.mp4  cropped to the atlas view (600×800), forward + reversed so it loops
        without a jump; motion.json lists the clips.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

VIEW = (80, 0, 800, 1066)     # must match face/render_face.py VIEW
SIZE = (600, 800)


def post(motion_dir: Path) -> dict:
    clips = []
    for raw in sorted(motion_dir.glob("raw-*/*.mp4")):
        if "concat" in raw.name:
            continue
        name = raw.parent.name.removeprefix("raw-")
        out = motion_dir / f"idle-{name}.mp4"
        x, y, w, h = VIEW
        vf = (f"crop={w}:{h}:{x}:{y},scale={SIZE[0]}:{SIZE[1]},split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1,"
              "format=yuv420p")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-filter_complex", vf, "-an", "-c:v", "libx264",
                        "-crf", "18", "-preset", "slow", "-movflags", "+faststart", str(out)], check=True)
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                                   capture_output=True, text=True).stdout)
        clips.append({"name": name, "file": out.name, "seconds": round(dur, 2), "driving": f"driving/{name}.mp4"})
    meta = {"version": 1, "clips": clips, "width": SIZE[0], "height": SIZE[1],
            "note": "Idle motion transferred from the owner's own phone videos onto their photo (LivePortrait)."}
    (motion_dir / "motion.json").write_text(json.dumps(meta, indent=2))
    return meta


if __name__ == "__main__":
    print(json.dumps(post(Path(sys.argv[1])), indent=2))
