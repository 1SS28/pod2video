"""Pick the best self-contained moments with an LLM, then snap them to sentence boundaries.

The model sees the transcript as numbered sentences with timestamps and answers with
sentence ids, so every cut lands on a sentence boundary. The window is then fitted to
the requested length (adding or dropping whole sentences) and padded slightly so the
first and last words are not clipped.
"""

from __future__ import annotations

from core.costs import CostLedger, LedgerEntry
from core.llm import LLM
from pod2video.models import Moment, Sentence, Transcript

SYSTEM = """You are a senior short-form video editor who cuts podcast episodes into vertical clips \
for TikTok, Reels and Shorts. You pick moments that work for a viewer who has never heard the show: \
they open with a hook in the first sentence, contain one complete thought or story with a payoff, \
and are quotable or surprising. Avoid moments that depend on earlier context, sponsor reads, \
intros/outros, and housekeeping."""

SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_sentence": {"type": "integer", "description": "id of the first sentence (S<id>)"},
                    "end_sentence": {"type": "integer", "description": "id of the last sentence, inclusive"},
                    "title": {"type": "string", "description": "punchy on-screen title, max 60 characters"},
                    "hook": {"type": "string", "description": "the opening line that grabs attention"},
                    "reason": {"type": "string", "description": "one sentence on why this works as a clip"},
                },
                "required": ["start_sentence", "end_sentence", "title", "hook", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}

LEAD_IN = 0.12  # seconds of air before the first word
TAIL = 0.35  # seconds after the last word


def render_transcript(sentences: list[Sentence], speaker_names: dict[str, str]) -> str:
    lines = []
    for s in sentences:
        who = speaker_names.get(s.speaker or "", s.speaker or "?")
        lines.append(f"[S{s.idx} {s.start:.1f}-{s.end:.1f}s {who}] {s.text}")
    return "\n".join(lines)


def build_prompt(
    sentences: list[Sentence],
    speaker_names: dict[str, str],
    *,
    count: int,
    min_len: float,
    max_len: float,
    podcast: str,
    episode: str,
) -> str:
    return f"""Podcast: {podcast}
Episode: {episode}

Below is the transcript as numbered sentences: [S<id> <start>-<end>s <speaker>] text.

<transcript>
{render_transcript(sentences, speaker_names)}
</transcript>

Pick the {count} best moments for standalone vertical clips, ranked best first.
Rules:
- Each moment is a contiguous run of sentences from start_sentence to end_sentence (inclusive).
- Length: between {min_len:.0f} and {max_len:.0f} seconds (end time of end_sentence minus start time of start_sentence).
- The first sentence must work as a hook without prior context; the last sentence must land the thought.
- Moments must not overlap each other.
- Titles are short, specific and in the speakers' own spirit; no clickbait the clip does not deliver."""


def fit_window(sentences: list[Sentence], first: int, last: int, min_len: float, max_len: float) -> tuple[int, int]:
    """Adjust a sentence range so its length is within [min_len, max_len] where possible."""
    n = len(sentences)
    first, last = max(0, min(first, n - 1)), max(0, min(last, n - 1))
    if last < first:
        first, last = last, first

    def length(a: int, b: int) -> float:
        return sentences[b].end - sentences[a].start

    while length(first, last) > max_len and last > first:
        last -= 1  # keep the hook; drop sentences from the tail
    while length(first, last) < min_len:
        if last + 1 < n and length(first, last + 1) <= max_len:
            last += 1
        elif first > 0 and length(first - 1, last) <= max_len:
            first -= 1
        else:
            break
    return first, last


def _padded_bounds(transcript: Transcript, sentences: list[Sentence], first: int, last: int, max_len: float) -> tuple[float, float]:
    words = transcript.words
    a, b = sentences[first], sentences[last]
    prev_end = words[a.first_word - 1].end if a.first_word > 0 else 0.0
    next_start = words[b.last_word + 1].start if b.last_word + 1 < len(words) else transcript.duration
    start = max(prev_end, a.start - LEAD_IN, 0.0)
    end = min(next_start, b.end + TAIL, transcript.duration or b.end + TAIL)
    if end - start > max_len:  # a single run-on sentence longer than max_len: cut at a word boundary
        limit = start + max_len
        inside = [w for w in words[a.first_word : b.last_word + 1] if w.end <= limit]
        end = inside[-1].end if inside else limit
    return round(start, 3), round(end, 3)


def select_moments(
    transcript: Transcript,
    llm: LLM,
    *,
    count: int,
    min_len: float,
    max_len: float,
    speaker_names: dict[str, str],
    podcast: str,
    episode: str,
    ledger: CostLedger,
) -> list[Moment]:
    sentences = transcript.sentences()
    if not sentences:
        return []
    prompt = build_prompt(
        sentences, speaker_names, count=count + 2, min_len=min_len, max_len=max_len, podcast=podcast, episode=episode
    )
    entry = ledger.add(LedgerEntry(provider="llm", kind="select_moments", label=llm.model))
    try:
        result = llm.json(system=SYSTEM, prompt=prompt, schema=SCHEMA, name="moments")
    except Exception as exc:
        ledger.update(entry, status="error", error=str(exc))
        raise
    ledger.update(
        entry,
        status="complete",
        usd=result.usd,
        meta={"input_tokens": result.input_tokens, "output_tokens": result.output_tokens},
    )
    return moments_from_ranges(transcript, sentences, result.data["moments"], count=count, min_len=min_len, max_len=max_len)


def moments_from_ranges(
    transcript: Transcript,
    sentences: list[Sentence],
    candidates: list[dict],
    *,
    count: int,
    min_len: float,
    max_len: float,
) -> list[Moment]:
    """Fit each candidate, drop overlaps, keep the best `count` in rank order."""
    chosen: list[Moment] = []
    for rank, cand in enumerate(candidates, start=1):
        first, last = fit_window(sentences, int(cand["start_sentence"]), int(cand["end_sentence"]), min_len, max_len)
        start, end = _padded_bounds(transcript, sentences, first, last, max_len)
        if end - start < min(min_len, 5.0):
            continue
        if any(start < m.end and end > m.start for m in chosen):
            continue
        chosen.append(
            Moment(
                start=start,
                end=end,
                title=cand.get("title", "").strip()[:80] or f"Clip {len(chosen) + 1}",
                hook=cand.get("hook", ""),
                reason=cand.get("reason", ""),
                score_rank=rank,
            )
        )
        if len(chosen) == count:
            break
    return chosen


def snap_to_words(transcript: Transcript, start: float, end: float) -> tuple[float, float]:
    """Snap a manual time range to word boundaries (used for --segment and edited moments files)."""
    words = transcript.words_between(start, end)
    if not words:
        return start, end
    first, last = words[0], words[-1]
    if first.start < start - 0.05:  # first word straddles the cut: start after it unless mostly inside
        first = next((w for w in words if w.start >= start - 0.05), first)
    if last.end > end + 0.05:
        inside = [w for w in words if w.end <= end + 0.05]
        last = inside[-1] if inside else last
    following = [w.start for w in transcript.words if w.start >= last.end]
    tail_end = min([last.end + TAIL, *following[:1]])
    return round(max(0.0, first.start - LEAD_IN), 3), round(tail_end, 3)
