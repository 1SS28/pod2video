"""Build a fake two-host episode for trying pod2video without any API keys (macOS only).

It uses the `say` voices to synthesise a short conversation, writes a matching
transcript into the pod2video cache (word times are approximate), and draws two
placeholder avatars. Then render previews with still images, for free:

    python examples/synthetic_demo.py
    pod2video run --audio examples/demo/demo.mp3 --podcast-name "Side Project Radio" \
        --host "Sam=examples/demo/sam.png" --host "Dan=examples/demo/dan.png" \
        --speaker-map "A=Sam,B=Dan" --moments examples/demo/moments.json --preview

The avatars have no real face, so they only work with --preview, not Magic Hour.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import wave
from pathlib import Path

from core.compose import probe_duration, run_ffmpeg
from pod2video.config import Settings
from pod2video.models import Transcript, Word
from pod2video.transcribe import file_sha256

HERE = Path(__file__).parent / "demo"
GAP = 0.35
RATE = 24000

SCRIPT = [
    ("A", "Samantha", "Welcome back to Side Project Radio. Today we are talking about why most side projects never ship. Dan, you have abandoned more projects than anyone I know."),
    ("B", "Daniel", "That is fair. I counted last week, and I have forty one folders called final version. Not one of them is final, and not one of them shipped."),
    ("A", "Samantha", "So what actually kills them? Is it time? Everybody says they do not have time."),
    ("B", "Daniel", "It is never time. Here is the uncomfortable truth. You do not run out of time, you run out of reasons. The project stops being a question you are curious about and becomes a chore you owe yourself."),
    ("A", "Samantha", "Okay, that is a little brutal."),
    ("B", "Daniel", "It is brutal, but it is useful. Because if the problem is curiosity, the fix is not a better calendar. The fix is to ship something small while you still care. One week. One feature. One person who uses it."),
    ("A", "Samantha", "I love that. My rule is that if I cannot demo it to a friend in thirty seconds, it is not a project yet. It is a mood board."),
    ("B", "Daniel", "A mood board! Yes. Half my folders are mood boards with a package dot json file."),
    ("A", "Samantha", "So give people one thing they can do this weekend."),
    ("B", "Daniel", "Pick the oldest folder you still feel guilty about. Delete everything except the one idea that made you start it. Then ship that one idea to one person by Sunday night. If nobody cares, you just saved yourself a year. If somebody cares, you have a project."),
    ("A", "Samantha", "And that is the episode. Ship the small thing, while you still care. Thanks for listening."),
]


def synthesize(workdir: Path) -> tuple[Path, list[Word]]:
    frames = bytearray()
    words: list[Word] = []
    t = 0.4
    frames += bytes(int(t * RATE) * 2)
    for i, (speaker, voice, text) in enumerate(SCRIPT):
        aiff = workdir / f"line_{i}.aiff"
        wav = workdir / f"line_{i}.wav"
        subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
        run_ffmpeg(["-i", aiff, "-ac", "1", "-ar", RATE, "-c:a", "pcm_s16le", wav])
        with wave.open(str(wav), "rb") as w:
            frames += w.readframes(w.getnframes())
        duration = probe_duration(wav)
        # Approximate word times: spread the line's words over its length, weighted by word length.
        tokens = text.split()
        speech = duration * 0.94
        weights = [len(tok) + 2 for tok in tokens]
        cursor = t + duration * 0.03
        for tok, weight in zip(tokens, weights):
            span = speech * weight / sum(weights)
            words.append(Word(text=tok, start=round(cursor, 3), end=round(cursor + span * 0.9, 3), speaker=speaker))
            cursor += span
        t += duration + GAP
        frames += bytes(int(GAP * RATE) * 2)

    joined = workdir / "demo.wav"
    with wave.open(str(joined), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))
    return joined, words


def avatar(path: Path, initials: str, colour: tuple[int, int, int]) -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1024, 1024), colour)
    draw = ImageDraw.Draw(img)
    draw.ellipse((212, 140, 812, 740), fill=(255, 255, 255))
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 260)
    except OSError:
        font = ImageFont.load_default()
    draw.text((512, 440), initials, fill=colour, font=font, anchor="mm")
    img.save(path)


def main() -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        wav, words = synthesize(Path(tmp))
        mp3 = HERE / "demo.mp3"
        run_ffmpeg(["-i", wav, "-c:a", "libmp3lame", "-b:a", "96k", mp3])

    settings = Settings()
    transcript = Transcript(
        provider=settings.transcription_provider,
        audio_sha256=file_sha256(mp3),
        duration=probe_duration(mp3),
        words=words,
        language="en",
    )
    cache = settings.cache_dir / "transcripts" / f"{transcript.audio_sha256[:24]}.{transcript.provider}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(transcript.model_dump_json())

    sentences = transcript.sentences()
    def span(first_words: str, last_words: str) -> dict:
        a = next(s for s in sentences if s.text.startswith(first_words))
        b = next(s for s in sentences if s.text.startswith(last_words))
        return {"start": a.start, "end": b.end}

    moments = [
        {**span("Here is the uncomfortable", "One week."), "title": "You don't run out of time"},
        {**span("Pick the oldest folder", "And that is the episode."), "title": "Do this before Sunday night"},
    ]
    (HERE / "moments.json").write_text(json.dumps(moments, indent=2))

    avatar(HERE / "sam.png", "S", (231, 84, 128))
    avatar(HERE / "dan.png", "D", (64, 120, 220))
    print(f"Wrote {mp3} ({transcript.duration:.0f}s), avatars, moments.json")
    print(f"Seeded transcript cache: {cache}")


if __name__ == "__main__":
    main()
