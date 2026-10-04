"""Host images: the photo as given, or a cartoon avatar made once with Magic Hour and cached.

Avatars are cached by (source photo or description, prompt, model), so each host pays for
theirs once and every later episode reuses it. Every image that will be animated is also
checked with Magic Hour face detection (free) before any Talking Photo credits are spent.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from core.magichour import MagicHour
from pod2video.models import Host

AVATAR_MODEL = "nano-banana"  # identity-preserving edits; 50 credits per 1k image (docs, 2026-10)
AVATAR_CREDITS_ESTIMATE = 50

CARTOON_FROM_PHOTO = (
    "Turn this photo into a clean 2D cartoon portrait of the same person. Keep their face shape, "
    "hairstyle, hair colour, skin tone, facial hair, glasses and overall look clearly recognisable. "
    "Head and shoulders, facing the camera, eyes open, mouth closed, neutral friendly expression, "
    "plain soft-coloured background, bold clean outlines, flat colours, no text."
)
CARTOON_FROM_TEXT = (
    "A clean 2D cartoon portrait of a podcast host: {description}. Head and shoulders, facing the "
    "camera, eyes open, mouth closed, neutral friendly expression, plain soft-coloured background, "
    "bold clean outlines, flat colours, no text."
)


def _digest(*parts: str | bytes) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part if isinstance(part, bytes) else part.encode())
        h.update(b"\0")
    return h.hexdigest()[:24]


def avatar_key(host: Host, *, style: str, prompt: str | None, model: str) -> str | None:
    """Cache key for the image to animate, or None when the host's photo is used as is."""
    if host.description and not host.photo:
        return _digest("text", host.description, prompt or CARTOON_FROM_TEXT, model)
    if style == "cartoon":
        return _digest("photo", host.photo.read_bytes(), prompt or CARTOON_FROM_PHOTO, model)
    return None


class Avatars:
    def __init__(self, cache_dir: Path, *, style: str = "photo", prompt: str | None = None, model: str = AVATAR_MODEL):
        self.dir = cache_dir / "avatars"
        self.style = style
        self.prompt = prompt
        self.model = model

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.png"

    def cached(self, host: Host) -> Path | None:
        """The image to animate if no paid generation is needed (photo, or an avatar made earlier)."""
        key = avatar_key(host, style=self.style, prompt=self.prompt, model=self.model)
        if key is None:
            return host.photo
        path = self._path(key)
        return path if path.exists() else None

    def pending(self, hosts: list[Host]) -> list[Host]:
        return [h for h in hosts if self.cached(h) is None]

    def generate(self, host: Host, mh: MagicHour) -> Path:
        key = avatar_key(host, style=self.style, prompt=self.prompt, model=self.model)
        dest = self._path(key)
        if dest.exists():
            return dest
        label = f"avatar / {host.name}"
        if host.photo:
            job = mh.image_edit(images=[host.photo], prompt=self.prompt or CARTOON_FROM_PHOTO, model=self.model, label=label)
        else:
            prompt = (self.prompt or CARTOON_FROM_TEXT).format(description=host.description)
            job = mh.image_generate(prompt=prompt, model=self.model, label=label)
        self.dir.mkdir(parents=True, exist_ok=True)
        mh.download_result(job, "image", dest)
        dest.with_suffix(".json").write_text(json.dumps({"host": host.name, "job_id": job, "model": self.model}, indent=2))
        return dest


def check_face(image: Path, mh: MagicHour, cache_dir: Path) -> int:
    """Number of faces Magic Hour detects in `image` (cached by image hash)."""
    digest = hashlib.sha256(image.read_bytes()).hexdigest()[:24]
    cache = cache_dir / "faces" / f"{digest}.json"
    if cache.exists():
        return json.loads(cache.read_text())["faces"]
    faces = len(mh.detect_faces(image))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"faces": faces}))
    return faces


def placeholder(name: str, dest: Path) -> Path:
    """A free stand-in (initials on a colour) for previewing a host whose avatar is not made yet."""
    from PIL import Image, ImageDraw, ImageFont

    hue = int(hashlib.sha256(name.encode()).hexdigest()[:6], 16)
    colour = (80 + hue % 140, 80 + (hue >> 8) % 140, 80 + (hue >> 16) % 140)
    img = Image.new("RGB", (1024, 1024), colour)
    draw = ImageDraw.Draw(img)
    draw.ellipse((212, 140, 812, 740), fill=(255, 255, 255))
    initials = "".join(part[0] for part in name.split()[:2]).upper() or "?"
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 240)
    except OSError:
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 240)
        except OSError:
            font = ImageFont.load_default()
    draw.text((512, 440), initials, fill=colour, font=font, anchor="mm")
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest
