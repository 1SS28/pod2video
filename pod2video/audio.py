"""Audio prep: cut clip segments, build per-host isolated tracks, derive speaker timelines."""

from __future__ import annotations

import wave
from array import array
from pathlib import Path
from typing import Sequence

from core.compose import run_ffmpeg
from pod2video.models import Word

ANIMATION_RATE = 24000  # mono WAV sent to Magic Hour; small upload, plenty for lip sync
FADE = 0.01  # seconds of fade at each gate edge to avoid clicks

Interval = tuple[float, float]


def cut(src: Path, start: float, end: float, dest: Path, *, mono: bool = False, rate: int = 48000) -> Path:
    """Cut [start, end) from `src` into a 16-bit WAV (sample-accurate: decoded, not stream-copied)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-ss", f"{start:.3f}", "-i", src, "-t", f"{end - start:.3f}",
            "-vn", "-ac", "1" if mono else "2", "-ar", rate, "-c:a", "pcm_s16le", dest,
        ]
    )
    return dest


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def speaker_intervals(
    words: Sequence[Word],
    speaker: str,
    *,
    clip_start: float,
    clip_end: float,
    pad: float = 0.12,
    merge_gap: float = 0.35,
) -> list[Interval]:
    """Clip-relative intervals where `speaker` talks, padded and with small gaps merged."""
    spans: list[list[float]] = []
    for w in words:
        if w.speaker != speaker or w.end <= clip_start or w.start >= clip_end:
            continue
        a = max(0.0, w.start - clip_start - pad)
        b = min(clip_end - clip_start, w.end - clip_start + pad)
        if spans and a - spans[-1][1] <= merge_gap:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in spans]


def isolate(src_wav: Path, keep: Sequence[Interval], dest: Path) -> Path:
    """Silence everything outside `keep`, preserving timing. Expects a 16-bit WAV."""
    with wave.open(str(src_wav), "rb") as wav:
        params = wav.getparams()
        if params.sampwidth != 2:
            raise ValueError("isolate() expects 16-bit PCM")
        samples = array("h")
        samples.frombytes(wav.readframes(params.nframes))
    channels, rate = params.nchannels, params.framerate
    total = len(samples) // channels

    def frame(t: float) -> int:
        return max(0, min(total, round(t * rate)))

    out = array("h", bytes(len(samples) * 2))
    fade_frames = max(1, round(FADE * rate))
    for a, b in keep:
        fa, fb = frame(a), frame(b)
        if fb <= fa:
            continue
        out[fa * channels : fb * channels] = samples[fa * channels : fb * channels]
        ramp = min(fade_frames, (fb - fa) // 2)
        for k in range(ramp):
            g = k / ramp
            for c in range(channels):
                head = (fa + k) * channels + c
                tail = (fb - 1 - k) * channels + c
                out[head] = int(samples[head] * g)
                out[tail] = int(samples[tail] * g)

    dest.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dest), "wb") as wav:
        wav.setparams(params)
        wav.writeframes(out.tobytes())
    return dest


def condense(src_wav: Path, spans: Sequence[Interval], dest: Path, *, pad: float) -> Path:
    """Join `spans` of a WAV back to back with `pad` seconds of silence between them.

    Used to animate one host's on-screen moments in a single Talking Photo job: the render
    is cut apart again at the same offsets (see `span_offsets`).
    """
    with wave.open(str(src_wav), "rb") as wav:
        params = wav.getparams()
        frames = wav.readframes(params.nframes)
    width = params.sampwidth * params.nchannels
    rate = params.framerate
    silence = bytes(round(pad * rate) * width)
    out = bytearray()
    for i, (a, b) in enumerate(spans):
        if i:
            out += silence
        out += frames[round(a * rate) * width : round(b * rate) * width]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dest), "wb") as wav:
        wav.setparams(params)
        wav.writeframes(bytes(out))
    return dest


def span_offsets(spans: Sequence[Interval], pad: float) -> list[float]:
    """Where each span starts inside the condensed track built by `condense`."""
    offsets, cursor = [], 0.0
    for a, b in spans:
        offsets.append(round(cursor, 3))
        cursor += (b - a) + pad
    return offsets


def shot_list(per_host: Sequence[Sequence[Interval]], duration: float, *, min_shot: float = 1.2) -> list[list[Interval]]:
    """Assign every instant of the clip to one host for `speaker-cut`.

    The camera cuts to whoever starts talking, holds through silences and short
    interjections, and never shows a shot shorter than `min_shot` seconds.
    Returns per-host lists of on-screen intervals.
    """
    events = sorted((a, b, host) for host, spans in enumerate(per_host) for a, b in spans)
    shots: list[list[float]] = []  # [start, end, host]
    for a, b, host in events:
        if not shots:
            shots.append([0.0, b, host])
            continue
        current = shots[-1]
        if host == current[2]:
            current[1] = max(current[1], b)
            continue
        cut_at = max(a, current[0] + min_shot)  # never cut away before min_shot
        if b - cut_at >= min_shot * 0.5:
            current[1] = cut_at
            shots.append([cut_at, b, host])
        # else: a brief interjection; stay on the current shot.
    if not shots:
        return [[(0.0, duration)]] + [[] for _ in per_host[1:]]
    shots[-1][1] = duration
    for prev, nxt in zip(shots, shots[1:]):
        prev[1] = nxt[0]  # no gaps: each shot runs until the next one starts
    result: list[list[Interval]] = [[] for _ in per_host]
    for start, end, host in shots:
        if end > start:
            result[int(host)].append((round(start, 3), round(end, 3)))
    return result
