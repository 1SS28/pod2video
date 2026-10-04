"""Make the README's before/after GIF: the clip's audio waveform, then the finished clip.

    python examples/make_demo_gif.py out/<episode>/clip_01_<title>.mp4 docs/demo.gif
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from core.compose import run_ffmpeg

W, H, FPS = 360, 640, 12


def main(clip: Path, dest: Path, before: float = 2.0, after: float = 6.0) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        wave_png = Path(tmp) / "wave.png"
        run_ffmpeg(["-i", clip, "-t", after, "-filter_complex", f"showwavespic=s={W - 40}x180:scale=sqrt:colors=0xFFD84D", "-frames:v", "1", wave_png])
        graph = (
            f"color=c=0x101014:s={W}x{H}:r={FPS}:d={before}[bg];"
            f"[bg][1:v]overlay=(W-w)/2:(H-h)/2[before];"
            f"[0:v]trim=duration={after},setpts=PTS-STARTPTS,fps={FPS},scale={W}:{H}[after];"
            f"[before][after]concat=n=2:v=1:a=0,split[a][b];"
            f"[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4"
        )
        run_ffmpeg(["-i", clip, "-i", wave_png, "-filter_complex", graph, "-loop", "0", dest])
    print(f"Wrote {dest}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
