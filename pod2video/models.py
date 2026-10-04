"""Data models shared across the pod2video pipeline."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

SENTENCE_END = (".", "?", "!")


class Word(BaseModel):
    text: str
    start: float  # seconds
    end: float
    speaker: str | None = None
    confidence: float | None = None


class Sentence(BaseModel):
    idx: int
    start: float
    end: float
    speaker: str | None
    text: str
    first_word: int  # index into Transcript.words
    last_word: int


class Transcript(BaseModel):
    provider: str
    audio_sha256: str
    duration: float
    words: list[Word]
    language: str | None = None

    def speakers_by_talk_time(self) -> list[tuple[str, float]]:
        totals: dict[str, float] = defaultdict(float)
        for w in self.words:
            if w.speaker:
                totals[w.speaker] += w.end - w.start
        return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)

    def words_between(self, start: float, end: float) -> list[Word]:
        return [w for w in self.words if w.end > start and w.start < end]

    def sentences(self, max_words: int = 45, soft_max_words: int = 28, pause: float = 1.2) -> list[Sentence]:
        """Split words into sentences on punctuation, speaker changes and long pauses.

        Run-on sentences are split at a comma after `soft_max_words`, or hard at `max_words`,
        so every sentence is short enough to be a cut point.
        """
        sentences: list[Sentence] = []
        begin = 0
        words = self.words

        def close(last: int) -> None:
            nonlocal begin
            chunk = words[begin : last + 1]
            sentences.append(
                Sentence(
                    idx=len(sentences),
                    start=chunk[0].start,
                    end=chunk[-1].end,
                    speaker=chunk[0].speaker,
                    text=" ".join(w.text for w in chunk),
                    first_word=begin,
                    last_word=last,
                )
            )
            begin = last + 1

        for i, word in enumerate(words):
            nxt = words[i + 1] if i + 1 < len(words) else None
            count = i - begin + 1
            stripped = word.text.rstrip("\"')]")
            if (
                nxt is None
                or stripped.endswith(SENTENCE_END)
                or nxt.speaker != word.speaker
                or nxt.start - word.end > pause
                or (count >= soft_max_words and stripped.endswith((",", ";", ":")))
                or count >= max_words
            ):
                close(i)
        return sentences


class Host(BaseModel):
    name: str
    photo: Path | None = None
    description: str | None = None  # text instead of a photo: a cartoon avatar is generated from it

    @model_validator(mode="after")
    def _needs_photo_or_description(self) -> "Host":
        if not self.photo and not self.description:
            raise ValueError(f"host {self.name} needs a photo or a description")
        return self


class Moment(BaseModel):
    start: float  # seconds in the episode audio
    end: float
    title: str
    reason: str = ""
    hook: str = ""
    score_rank: int | None = Field(default=None, description="rank the model gave this moment (1 = best)")

    @property
    def duration(self) -> float:
        return self.end - self.start
