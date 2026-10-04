"""First real run: check quality before trusting the defaults (needs your keys in .env).

    .venv/bin/python examples/quality_test.py

1. Checks your setup (ffmpeg, keys, plan, credit balance).
2. Transcribes a real two-person episode and picks moments, then renders free previews.
   Costs about $0.10 for transcription and $0.20 for moment selection, and no Magic Hour credits.
3. After you confirm, renders the same 15 s of the best moment twice, in `prompted` and
   `realistic` mode, with cartoon avatars generated from text descriptions. That's about
   1,150 Magic Hour credits in total (~$1.80 on Creator monthly), each step budget-capped.

The default episode is Hacker Public Radio 4735 (an interview, CC BY-SA 4.0). The avatars are
generic cartoons made from descriptions, not likenesses of the speakers, which is right for a
private quality check. For a public demo, use your own show and photos of people who agreed.
Pass --audio/--host to use your own material instead.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HPR_4735 = "https://hub.hackerpublicradio.org/ccdn.php?filename=/eps/hpr4735/hpr4735.mp3"
DEFAULT_HOSTS = [
    "Host=desc:friendly podcast host, short dark hair, over-ear headphones, dark green hoodie",
    "Guest=desc:podcast guest, round glasses, light brown curly hair, grey t-shirt with a gear logo",
]
OUT = Path("out/quality-test")


def pod2video(*args: str) -> int:
    print("\n$ pod2video " + " ".join(args), flush=True)
    return subprocess.call([sys.executable, "-m", "pod2video.cli", *args])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audio", default=HPR_4735, help="audio file or URL (default: HPR 4735)")
    parser.add_argument("--title", default="HPR 4735: friedcamp interviews, part 5")
    parser.add_argument("--podcast", default="Hacker Public Radio")
    parser.add_argument("--host", action="append", help='"Name=photo.jpg" or "Name=desc:..." (repeat; default: two cartoon hosts)')
    parser.add_argument("--seconds", type=float, default=15.0, help="length of the paid A/B segment")
    args = parser.parse_args()
    hosts = [arg for h in (args.host or DEFAULT_HOSTS) for arg in ("--host", h)]

    if pod2video("doctor") != 0:
        sys.exit("Fix the items marked ✗ above (keys go in .env), then run this again.")

    audio = args.audio
    if audio.startswith(("http://", "https://")):
        from pod2video.config import Settings
        from pod2video.feed import download

        audio = str(download(audio, Settings().cache_dir))

    common = ["--audio", audio, "--podcast-name", args.podcast, "--episode-title", args.title, *hosts]
    print("\nStep 2: transcribe, pick moments, free previews. Answer the 'which speaker is who' prompt.")
    if pod2video("run", *common, "--out", str(OUT / "previews"), "--preview") != 0:
        sys.exit("Preview failed; see the error above.")

    moments = next((OUT / "previews").glob("*/moments.json"))
    best = json.loads(moments.read_text())["moments"][0]
    start = round(best["start"], 2)
    segment = f"{start}-{round(start + args.seconds, 2)}"
    print(f"\nPreviews are in {moments.parent}. The best moment is \"{best['title']}\".")
    print(f"Step 3 renders {segment} s twice (prompted, then realistic), plus cartoon avatars made once.")
    if input("Spend about 1,150 Magic Hour credits on this test? [y/N] ").strip().lower() != "y":
        sys.exit("Stopped before spending anything.")

    budgets = {"prompted": 500, "realistic": 1000}  # avatars (~50 each) are made in the first run and reused
    for mode, budget in budgets.items():
        code = pod2video(
            "run", *common, "--segment", segment, "--generation-mode", mode,
            "--out", str(OUT / mode), "--yes", "--max-credits", str(budget),
        )
        if code != 0:
            sys.exit(f"The {mode} render failed; see the error above.")

    print("\nDone. Compare these side by side:")
    for mode in budgets:
        for clip in sorted((OUT / mode).glob("*/clip_*.mp4")):
            report = json.loads((clip.parent / "report.json").read_text())
            print(f"  {mode:9} {clip}  ({report['totals']['magichour_credits']:,} credits)")


if __name__ == "__main__":
    main()
