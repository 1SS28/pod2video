"""ffmpeg helpers: binary discovery, photo prep, 9:16 layouts, ASS captions, overlays.

Frame (1080x1920). In the one-face layouts (`speaker-cut`, `single`) the speaker sits on a
rounded "card" over a blurred, darkened copy of their own image, with the podcast name and
episode title above, captions and a faint audio waveform below, and a progress bar at the
bottom. Every shot slowly pushes in, and alternate shots of the same host are punched in
tighter, so even still previews feel edited. `split` stacks the hosts under a header band.
Text, tags and the progress bar are one ASS file, so the only special filter ffmpeg needs is
libass (`ass`).
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

WIDTH = 1080
HEIGHT = 1920
HEADER = 200
FPS = 30
HOOK_SECONDS = 3.0
CARD_LAYOUTS = ("speaker-cut", "single")
CARD_RADIUS = 40
WAVE_Y, WAVE_H = 1630, 110
PROGRESS_Y = 1852

_FFMPEG_CANDIDATES = (
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",  # macOS: `brew install ffmpeg-full` (keg-only, has libass)
    "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
)


class FFmpegError(RuntimeError):
    pass


# -- ffmpeg discovery & execution ------------------------------------------------


def _has_libass(binary: str) -> bool:
    try:
        out = subprocess.run([binary, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.split()[1:2] == ["ass"] for line in out.splitlines() if line.strip())


@functools.lru_cache(maxsize=1)
def ffmpeg_bin() -> str:
    """FFMPEG_BIN, else the first ffmpeg with libass, else any ffmpeg on PATH."""
    explicit = os.environ.get("FFMPEG_BIN")
    if explicit:
        return explicit
    found = [p for p in (shutil.which("ffmpeg"), *_FFMPEG_CANDIDATES) if p and Path(p).exists()]
    for binary in found:
        if _has_libass(binary):
            return binary
    if found:
        return found[0]
    raise FFmpegError("ffmpeg not found. Install it (macOS: `brew install ffmpeg-full`) or set FFMPEG_BIN.")


@functools.lru_cache(maxsize=1)
def ffprobe_bin() -> str:
    explicit = os.environ.get("FFPROBE_BIN")
    if explicit:
        return explicit
    sibling = Path(ffmpeg_bin()).with_name("ffprobe")
    if sibling.exists():
        return str(sibling)
    found = shutil.which("ffprobe")
    if not found:
        raise FFmpegError("ffprobe not found next to ffmpeg or on PATH; set FFPROBE_BIN")
    return found


def supports_captions() -> bool:
    return _has_libass(ffmpeg_bin())


def run_ffmpeg(args: Sequence[str], *, cwd: Path | None = None) -> None:
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", *map(str, args)]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-25:])
        raise FFmpegError(f"ffmpeg failed ({proc.returncode}):\n{tail}")


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed on {path}: {proc.stderr.strip()}")
    return float(json.loads(proc.stdout)["format"]["duration"])


# -- Layout -------------------------------------------------------------------------


@dataclass(frozen=True)
class Pane:
    x: int
    y: int
    w: int
    h: int

    @property
    def aspect(self) -> float:
        return self.w / self.h


def layout_panes(layout: str, n_hosts: int) -> list[Pane]:
    """One pane per host video.

    `speaker-cut` and `single` show one face on a 960x960 card. Talking Photo outputs at most
    720p, so a square card needs a 1.33x upscale where a full-height 9:16 frame would need 2.4x
    and look soft.
    """
    if layout == "split":
        h = (HEIGHT - HEADER) // n_hosts
        return [Pane(0, HEADER + i * h, WIDTH, h) for i in range(n_hosts)]
    if layout in CARD_LAYOUTS:
        return [Pane(60, 250, 960, 960) for _ in range(n_hosts)]
    raise ValueError(f"unknown layout: {layout}")


@dataclass(frozen=True)
class Placement:
    """Show `render` (an index into the host videos) in `pane` from clip time `start` to `end`,
    starting `offset` seconds into that video."""

    render: int
    pane: Pane
    start: float
    end: float
    offset: float = 0.0
    zoom: float = 1.0  # starting zoom (punch-in)
    push: float = 0.04  # extra zoom by the end of the shot (slow push-in)


def crop_photo(src: Path, dest: Path, aspect: float, *, focus_y: float = 0.35, max_edge: int = 1440) -> Path:
    """Crop a host photo to the pane aspect (biased upward, where faces usually are)."""
    from PIL import Image, ImageOps

    with Image.open(src) as opened:
        img = ImageOps.exif_transpose(opened).convert("RGB")
    w, h = img.size
    if w / h > aspect:
        new_w = round(h * aspect)
        x0 = (w - new_w) // 2
        box = (x0, 0, x0 + new_w, h)
    else:
        new_h = round(w / aspect)
        y0 = round((h - new_h) * focus_y)
        box = (0, y0, w, y0 + new_h)
    img = img.crop(box)
    img.thumbnail((max_edge, max_edge))
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, quality=92)
    return dest


def make_backdrop(image: Path, dest: Path) -> Path:
    """A full-frame blurred, darkened copy of the host image to sit behind the card."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    with Image.open(image) as opened:
        img = ImageOps.exif_transpose(opened).convert("RGB")
    small = ImageOps.fit(img, (WIDTH // 8, HEIGHT // 8))  # blur small, then upscale: smooth and fast
    small = small.filter(ImageFilter.GaussianBlur(12))  # ~100 px at full size: a colour wash, not a picture
    small = ImageEnhance.Color(small).enhance(1.3)
    big = ImageEnhance.Brightness(small.resize((WIDTH, HEIGHT), Image.BICUBIC)).enhance(0.38)
    # Darker top and bottom so the header text and progress bar stay readable on any photo.
    shade = Image.new("L", (1, HEIGHT))
    for y in range(HEIGHT):
        top = max(0.0, 1 - y / 380) * 170
        bottom = max(0.0, (y - 1450) / 470) * 150
        shade.putpixel((0, y), int(max(top, bottom)))
    black = Image.new("RGB", (WIDTH, HEIGHT))
    big = Image.composite(black, big, shade.resize((WIDTH, HEIGHT)))
    dest.parent.mkdir(parents=True, exist_ok=True)
    big.save(dest, quality=90)
    return dest


def _card_assets(pane: Pane, workdir: Path) -> tuple[Path, Path]:
    """Rounded-corner alpha mask for the card, and a soft drop shadow under it."""
    from PIL import Image, ImageDraw, ImageFilter

    mask_path, shadow_path = workdir / "card_mask.png", workdir / "card_shadow.png"
    if not mask_path.exists():
        big = Image.new("L", (pane.w * 4, pane.h * 4), 0)  # draw 4x and downsample for smooth corners
        ImageDraw.Draw(big).rounded_rectangle((0, 0, pane.w * 4 - 1, pane.h * 4 - 1), radius=CARD_RADIUS * 4, fill=255)
        big.resize((pane.w, pane.h), Image.LANCZOS).save(mask_path)
    if not shadow_path.exists():
        alpha = Image.new("L", (WIDTH, HEIGHT), 0)
        ImageDraw.Draw(alpha).rounded_rectangle(
            (pane.x, pane.y + 18, pane.x + pane.w, pane.y + pane.h + 18), radius=CARD_RADIUS, fill=170
        )
        shadow = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        shadow.putalpha(alpha.filter(ImageFilter.GaussianBlur(26)))
        shadow.save(shadow_path)
    return mask_path, shadow_path


def still_video(image: Path, duration: float, dest: Path) -> Path:
    """A static video from a photo: the free stand-in for Talking Photo in --preview mode."""
    run_ffmpeg(
        [
            "-loop", "1", "-i", image, "-t", f"{duration:.3f}", "-r", FPS,
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", dest,
        ]
    )
    return dest


# -- Captions & overlays (ASS) ---------------------------------------------------------


class TimedWord(Protocol):
    text: str
    start: float
    end: float
    speaker: str | None


@dataclass(frozen=True)
class CaptionStyle:
    name: str
    font: str = "Arial"
    size: int = 78
    bold: bool = True
    uppercase: bool = True
    primary: str = "FFFFFF"  # RGB hex
    highlight: str | None = "FFD84D"  # active word colour; None disables karaoke highlight
    outline: str = "000000"
    outline_width: float = 6.0
    box: bool = False  # opaque box behind captions instead of an outline
    box_alpha: int = 0x60  # 0 = opaque, 255 = invisible
    max_words: int = 3
    max_chars: int = 20
    accent: str = "FFD84D"  # header rule and active-speaker bar
    header_bg: str = "101014"


CAPTION_STYLES: dict[str, CaptionStyle] = {
    "bold": CaptionStyle(name="bold", size=80, max_chars=16),
    "clean": CaptionStyle(
        name="clean", size=60, uppercase=False, highlight="7FE3FF", outline_width=14, box=True,
        max_words=6, max_chars=32, accent="7FE3FF",
    ),
    "minimal": CaptionStyle(
        name="minimal", size=54, bold=False, uppercase=False, highlight=None, outline_width=3,
        max_words=7, max_chars=36, accent="FFFFFF", header_bg="000000",
    ),
}


@dataclass
class Overlay:
    """Everything drawn on top of the video for one clip."""

    duration: float
    layout: str
    panes: list[Pane]
    host_names: list[str]
    words: list[TimedWord] = field(default_factory=list)  # clip-relative times
    # Per host (pane order): clip-relative intervals when that host is on screen/speaking.
    active: list[list[tuple[float, float]]] = field(default_factory=list)
    podcast_name: str = ""
    episode_title: str = ""
    hook: str = ""  # the clip title, shown big for the first seconds
    has_logo: bool = False


def _ass_colour(rgb: str, alpha: int = 0) -> str:
    rgb = rgb.lstrip("#")
    return f"&H{alpha:02X}{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}".upper()


def _ass_time(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_text(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def caption_chunks(words: Sequence[TimedWord], max_words: int, max_chars: int, gap: float = 0.6) -> list[list[TimedWord]]:
    """Group words into short on-screen phrases, breaking at pauses, punctuation and speaker changes."""
    chunks: list[list[TimedWord]] = []
    current: list[TimedWord] = []
    for word in words:
        if current:
            prev = current[-1]
            too_long = len(" ".join(w.text for w in current)) + 1 + len(word.text) > max_chars
            if (
                len(current) >= max_words
                or too_long
                or word.start - prev.end > gap
                or (word.speaker and prev.speaker and word.speaker != prev.speaker)
                or (prev.text.endswith(",") and len(current) >= 2)
            ):
                chunks.append(current)
                current = []
        current.append(word)
        if word.text.rstrip("\"')]").endswith((".", "?", "!")):
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def build_ass(overlay: Overlay, style: CaptionStyle) -> str:
    bold = -1 if style.bold else 0
    fmt = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding"
    )
    # With a box, the box is its own layer (one box per phrase); per-word colour runs would
    # otherwise each get a box and the overlaps show as dark seams.
    text_outline = 0 if style.box else style.outline_width
    box_colour = _ass_colour("000000", style.box_alpha)
    styles = [
        f"Style: Caption,{style.font},{style.size},{_ass_colour(style.primary)},&H000000FF,{_ass_colour(style.outline)},"
        f"&H00000000,{bold},0,0,0,100,100,0,0,1,{text_outline},0,5,80,80,0,1",
        f"Style: CaptionBox,{style.font},{style.size},&HFF000000,&HFF000000,{box_colour},"
        f"{box_colour},{bold},0,0,0,100,100,0,0,3,{style.outline_width},0,5,80,80,0,1",
        f"Style: Header,{style.font},52,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        f"Style: Sub,{style.font},34,&H00B4B4B4,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        f"Style: Tag,{style.font},40,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,1,0,1,3,0,1,0,0,0,1",
        f"Style: TagBox,{style.font},34,&H00FFFFFF,&H000000FF,&H50000000,&H50000000,-1,0,0,0,100,100,2,0,3,12,0,1,0,0,0,1",
        f"Style: Hook,{style.font},60,{_ass_colour(style.header_bg)},&H000000FF,{_ass_colour(style.accent)},"
        f"{_ass_colour(style.accent)},-1,0,0,0,100,100,0,0,3,18,0,8,60,60,0,1",
        "Style: Shape,Arial,10,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
    ]
    end = _ass_time(overlay.duration)
    events: list[str] = []

    def dialogue(layer: int, start: float | str, stop: float | str, style_name: str, text: str) -> None:
        a = start if isinstance(start, str) else _ass_time(start)
        b = stop if isinstance(stop, str) else _ass_time(stop)
        if a != b:
            events.append(f"Dialogue: {layer},{a},{b},{style_name},,0,0,0,,{text}")

    def rect(x: int, y: int, w: int, h: int, colour: str, alpha: int = 0, extra: str = "") -> str:
        return (
            f"{{\\an7\\pos({x},{y})\\p1\\1c{_ass_colour(colour)}\\1a&H{alpha:02X}&{extra}}}"
            f"m 0 0 l {w} 0 {w} {h} 0 {h}{{\\p0}}"
        )

    card = overlay.layout in CARD_LAYOUTS

    # Header band text (the band itself is the ffmpeg background colour).
    text_x = 210 if overlay.has_logo else 48
    title_chars = 44 if overlay.has_logo else 52
    headline, subline = overlay.podcast_name, overlay.episode_title
    if not headline:  # no show name: promote the episode title to the headline slot
        headline, subline = subline, ""
    if headline:
        y = 40 if subline else 68
        dialogue(2, 0.0, end, "Header", f"{{\\pos({text_x},{y})}}{_ass_text(_truncate(headline, 32))}")
    if subline:
        dialogue(2, 0.0, end, "Sub", f"{{\\pos({text_x},112)}}{_ass_text(_truncate(subline, title_chars))}")
    if not card:
        dialogue(1, 0.0, end, "Shape", rect(0, HEADER - 6, WIDTH, 6, style.accent))

    # Name tags and active-speaker marker.
    for i, (pane, name) in enumerate(zip(overlay.panes, overlay.host_names)):
        intervals = overlay.active[i] if i < len(overlay.active) else []
        tag = f"{{\\pos({pane.x + 40},{pane.y + pane.h - 36})}}{_ass_text(name.upper())}"
        if card:
            pill = f"{{\\pos({pane.x + 34},{pane.y + pane.h - 30})}}{_ass_text(name.upper())}"
            for a, b in (intervals if overlay.layout == "speaker-cut" else [(0.0, overlay.duration)]):
                dialogue(3, a, b, "TagBox", pill)
        elif overlay.layout == "split":
            dialogue(3, 0.0, end, "Tag", tag)
            for a, b in intervals:
                dialogue(1, a, b, "Shape", rect(pane.x, pane.y, 14, pane.h, style.accent))
        elif overlay.layout == "speaker-cut":
            for a, b in intervals:
                dialogue(3, a, b, "Tag", tag)
        else:
            dialogue(3, 0.0, end, "Tag", tag)

    # Hook: the clip title over the first seconds, where viewers decide whether to keep watching.
    if overlay.hook:
        top = overlay.panes[0]
        hook_y = top.y + top.h + 56 if card else top.y + 48
        hook_end = min(HOOK_SECONDS, overlay.duration)
        dialogue(6, 0.0, hook_end, "Hook", f"{{\\pos({WIDTH // 2},{hook_y})\\fad(150,300)}}{_ass_text(_truncate(overlay.hook, 60))}")

    # Word-timed captions.
    if card:
        cap_y = overlay.panes[0].y + overlay.panes[0].h + 240  # under the card, above the waveform
    elif len(overlay.panes) == 2:
        cap_y = overlay.panes[1].y  # on the seam between the two hosts
    else:
        cap_y = HEADER + round((HEIGHT - HEADER) * 0.72)
    pop = "\\fscx84\\fscy84\\t(0,120,\\fscx100\\fscy100)"  # each phrase pops in
    base = _ass_colour(style.primary)
    chunks = caption_chunks(overlay.words, style.max_words, style.max_chars)
    for n, chunk in enumerate(chunks):
        tokens = [_ass_text(w.text.upper() if style.uppercase else w.text) for w in chunk]
        chunk_start = chunk[0].start
        next_start = chunks[n + 1][0].start if n + 1 < len(chunks) else overlay.duration
        chunk_end = min(chunk[-1].end + 0.4, next_start, overlay.duration)
        if style.box:
            dialogue(4, chunk_start, chunk_end, "CaptionBox", f"{{\\pos({WIDTH // 2},{cap_y}){pop}}}{' '.join(tokens)}")
        if style.highlight is None:
            dialogue(5, chunk_start, chunk_end, "Caption", f"{{\\pos({WIDTH // 2},{cap_y}){pop}}}{' '.join(tokens)}")
            continue
        hi = _ass_colour(style.highlight)
        for i, word in enumerate(chunk):
            seg_start = chunk_start if i == 0 else word.start
            seg_end = chunk[i + 1].start if i + 1 < len(chunk) else chunk_end
            if seg_end <= seg_start:
                continue
            parts = [f"{{\\1c{hi}}}{t}{{\\1c{base}}}" if j == i else t for j, t in enumerate(tokens)]
            start_tags = pop if i == 0 else ""
            dialogue(5, seg_start, seg_end, "Caption", f"{{\\pos({WIDTH // 2},{cap_y}){start_tags}}}{' '.join(parts)}")

    # Progress bar: a faint track and an accent fill that grows with the clip.
    x0, bar_w = (60, WIDTH - 120) if card else (0, WIDTH)
    y0 = PROGRESS_Y if card else HEIGHT - 10
    dialogue(1, 0.0, end, "Shape", rect(x0, y0, bar_w, 8, "FFFFFF", alpha=0xB0))
    grow = f"\\clip({x0},{y0},{x0},{y0 + 8})\\t(0,{round(overlay.duration * 1000)},\\clip({x0},{y0},{x0 + bar_w},{y0 + 8}))"
    dialogue(2, 0.0, end, "Shape", rect(x0, y0, bar_w, 8, style.accent, extra=grow))

    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {WIDTH}",
            f"PlayResY: {HEIGHT}",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            fmt,
            *styles,
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events,
            "",
        ]
    )


# -- Final render --------------------------------------------------------------------


def _enable(windows: Sequence[tuple[float, float]]) -> str:
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in windows) or "0"


def render_clip(
    *,
    renders: Sequence[Path],
    placements: Sequence[Placement],
    mix_audio: Path,
    overlay: Overlay,
    style: CaptionStyle,
    dest: Path,
    logo: Path | None = None,
    backdrops: Sequence[Path | None] | None = None,
) -> Path:
    """Composite host videos into the 9:16 frame, burn in the overlay, and restore the original audio.

    Each placement shows part of one render from `start` to `end` in clip time. In the card
    layouts, shots are cut onto a rounded card (with a slow push-in and their punch-in zoom)
    over `backdrops[render]`, a blurred copy of that host's image. Later placements draw on top,
    so a shared boundary resolves to the newer shot.
    """
    workdir = dest.parent
    workdir.mkdir(parents=True, exist_ok=True)
    ass_path = workdir / f"{dest.stem}.ass"
    ass_path.write_text(build_ass(overlay, style), encoding="utf-8")
    card = overlay.layout in CARD_LAYOUTS
    d = f"{overlay.duration:.3f}"
    ordered = sorted(placements, key=lambda p: p.start)

    inputs: list[str] = []

    def add(path: Path, still: bool = False) -> int:
        if still:
            inputs.extend(["-loop", "1", "-framerate", str(FPS), "-t", d])
        inputs.extend(["-i", str(Path(path).resolve())])
        return inputs.count("-i") - 1

    for video in renders:
        add(video)
    audio_index = add(mix_audio)
    logo_index = add(logo, still=True) if logo else None

    graph = [f"color=c=0x{style.header_bg}:s={WIDTH}x{HEIGHT}:r={FPS}:d={d}[bg]"]
    last = "bg"
    audio_out = f"{audio_index}:a"
    if card:
        pane = overlay.panes[0]
        mask, shadow = _card_assets(pane, workdir)
        mask_index, shadow_index = add(mask, still=True), add(shadow, still=True)
        unique = {}
        for p in ordered:
            path = backdrops[p.render] if backdrops else None
            if path:
                unique.setdefault(path, []).append((p.start, p.end))
        for n, (path, windows) in enumerate(unique.items()):
            index = add(path, still=True)
            graph.append(f"[{index}:v]scale={WIDTH}:{HEIGHT},setsar=1[bd{n}]")
            graph.append(f"[{last}][bd{n}]overlay=0:0:enable='{_enable(windows)}'[bdo{n}]")
            last = f"bdo{n}"
        graph.append(f"[{last}][{shadow_index}:v]overlay=0:0[shadowed]")
        last = "shadowed"
        graph.append(f"color=c=black@0.0:s={pane.w}x{pane.h}:r={FPS}:d={d},format=rgba[c]")
        target = "c"
        graph.append(f"[{audio_index}:a]asplit=2[aout][awave]")
        audio_out = "[aout]"

    for i in range(len(renders)):
        uses = [n for n, p in enumerate(ordered) if p.render == i]
        if not uses:
            continue
        pane = ordered[uses[0]].pane
        # Card shots are zoomed with zoompan, so give it 1.5x the pixels to keep the motion smooth.
        w, h = (pane.w * 3 // 2, pane.h * 3 // 2) if card else (pane.w, pane.h)
        outs = "".join(f"[r{n}]" for n in uses)
        graph.append(
            f"[{i}:v]fps={FPS},scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,tpad=stop_mode=clone:stop_duration=30,split={len(uses)}{outs}"
        )
    for n, p in enumerate(ordered):
        # Take a little extra at the end so a shot never runs a frame short before the next one starts.
        chain = f"[r{n}]trim=start={p.offset:.3f}:end={p.offset + (p.end - p.start) + 0.1:.3f},setpts=PTS-STARTPTS"
        if card:
            frames = max(1, round((p.end - p.start + 0.1) * FPS))
            chain += (
                f",zoompan=z='{p.zoom:.3f}*(1+{p.push:.3f}*on/{frames})':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)*0.42'"
                f":d=1:s={p.pane.w}x{p.pane.h}:fps={FPS}"
            )
        graph.append(f"{chain},setpts=PTS-STARTPTS+{p.start:.3f}/TB[p{n}]")
        window = f"enable='between(t,{p.start:.3f},{p.end:.3f})'"
        if card:
            graph.append(f"[{target}][p{n}]overlay=0:0:format=auto:eof_action=pass:{window}[c{n}]")
            target = f"c{n}"
        else:
            graph.append(f"[{last}][p{n}]overlay={p.pane.x}:{p.pane.y}:eof_action=pass:{window}[o{n}]")
            last = f"o{n}"

    if card:
        pane = overlay.panes[0]
        graph.append(f"[{mask_index}:v]format=gray[mask]")
        graph.append(f"[{target}][mask]alphamerge[card]")
        graph.append(f"[{last}][card]overlay={pane.x}:{pane.y}[carded]")
        graph.append(
            f"[awave]showwaves=s={pane.w}x{WAVE_H}:mode=cline:scale=sqrt:colors=0xFFFFFF:rate={FPS},"
            f"format=rgba,colorchannelmixer=aa=0.45[wave]"
        )
        graph.append(f"[carded][wave]overlay={pane.x}:{WAVE_Y}[waved]")
        last = "waved"
    graph.append(f"[{last}]ass={ass_path.name}[sub]")
    last = "sub"
    if logo_index is not None:
        graph.append(f"[{logo_index}:v]scale=-1:140,format=rgba[logo]")
        graph.append(f"[{last}][logo]overlay=40:28[branded]")
        last = "branded"
    graph.append(f"[{last}]format=yuv420p[out]")

    run_ffmpeg(
        [
            *inputs,
            "-filter_complex", ";".join(graph),
            "-map", "[out]", "-map", audio_out,
            "-t", d, "-r", FPS,
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            dest.name,
        ],
        cwd=workdir,
    )
    return dest
