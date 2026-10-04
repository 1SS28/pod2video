"""Transcription with word timestamps and speaker labels, cached to JSON per audio file.

Providers are swappable: set TRANSCRIPTION_PROVIDER to `assemblyai` (default) or `deepgram`.
Each provider returns words normalised to `Word(text, start, end, speaker)` with times in
seconds and speaker labels as letters (A, B, ...).
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Protocol

import httpx

from core.costs import CostLedger, LedgerEntry
from pod2video.models import Transcript, Word


# USD per audio hour with speaker labels (pricing pages, 2026-10): AssemblyAI Universal-3.5 Pro
# $0.21 + diarization $0.02; Deepgram Nova-3 $0.0043/min with diarization included.
DEFAULT_USD_PER_HOUR = {"assemblyai": 0.23, "deepgram": 0.258}


class TranscriptionError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Provider(Protocol):
    name: str

    def transcribe(self, audio: Path) -> tuple[list[Word], float, str | None]:
        """Return (words, duration_seconds, language)."""
        ...


class AssemblyAI:
    name = "assemblyai"
    base_url = "https://api.assemblyai.com"

    def __init__(
        self,
        api_key: str,
        *,
        speakers_expected: int | None = None,
        poll_interval: float = 3.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"authorization": api_key},
            timeout=httpx.Timeout(60.0, write=600.0),
            transport=transport,
        )
        self.speakers_expected = speakers_expected
        self.poll_interval = poll_interval

    def transcribe(self, audio: Path) -> tuple[list[Word], float, str | None]:
        with audio.open("rb") as fh:
            upload = self._http.post("/v2/upload", content=fh, headers={"content-type": "application/octet-stream"})
        _raise_for(upload, "AssemblyAI upload")
        body: dict = {"audio_url": upload.json()["upload_url"], "speaker_labels": True, "language_detection": True}
        if self.speakers_expected:
            body["speakers_expected"] = self.speakers_expected
        submitted = self._http.post("/v2/transcript", json=body)
        _raise_for(submitted, "AssemblyAI submit")
        transcript_id = submitted.json()["id"]

        while True:
            result = self._http.get(f"/v2/transcript/{transcript_id}")
            _raise_for(result, "AssemblyAI poll")
            data = result.json()
            if data["status"] == "completed":
                break
            if data["status"] == "error":
                raise TranscriptionError(f"AssemblyAI failed: {data.get('error')}")
            time.sleep(self.poll_interval)

        words = [
            Word(
                text=w["text"],
                start=w["start"] / 1000,
                end=w["end"] / 1000,
                speaker=w.get("speaker"),
                confidence=w.get("confidence"),
            )
            for w in data.get("words") or []
        ]
        duration = float(data.get("audio_duration") or (words[-1].end if words else 0.0))
        return words, duration, data.get("language_code")


class Deepgram:
    name = "deepgram"
    url = "https://api.deepgram.com/v1/listen"

    def __init__(self, api_key: str, *, model: str = "nova-3", transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            headers={"Authorization": f"Token {api_key}"},
            timeout=httpx.Timeout(60.0, read=900.0, write=600.0),
            transport=transport,
        )
        self.model = model

    def transcribe(self, audio: Path) -> tuple[list[Word], float, str | None]:
        params = {"model": self.model, "diarize": "true", "punctuate": "true", "smart_format": "true", "detect_language": "true"}
        with audio.open("rb") as fh:
            response = self._http.post(self.url, params=params, content=fh, headers={"Content-Type": "application/octet-stream"})
        _raise_for(response, "Deepgram")
        data = response.json()
        channel = data["results"]["channels"][0]
        alternative = channel["alternatives"][0]
        words = [
            Word(
                text=w.get("punctuated_word") or w["word"],
                start=float(w["start"]),
                end=float(w["end"]),
                speaker=_letter(w.get("speaker")),
                confidence=w.get("confidence"),
            )
            for w in alternative.get("words", [])
        ]
        duration = float(data.get("metadata", {}).get("duration") or (words[-1].end if words else 0.0))
        return words, duration, channel.get("detected_language")


def _letter(index: int | None) -> str | None:
    if index is None:
        return None
    return chr(ord("A") + index) if index < 26 else f"S{index}"


def _raise_for(response: httpx.Response, what: str) -> None:
    if response.is_error:
        raise TranscriptionError(f"{what} failed: HTTP {response.status_code} {response.text[:300]}")


def make_provider(name: str, api_key: str, *, speakers_expected: int | None = None) -> Provider:
    if not api_key:
        raise TranscriptionError("TRANSCRIPTION_API_KEY is not set")
    if name == "assemblyai":
        return AssemblyAI(api_key, speakers_expected=speakers_expected)
    if name == "deepgram":
        return Deepgram(api_key)
    raise TranscriptionError(f"unknown transcription provider '{name}' (use assemblyai or deepgram)")


def transcribe(
    audio: Path,
    *,
    provider_name: str,
    api_key: str,
    cache_dir: Path,
    ledger: CostLedger,
    usd_per_hour: float | None = None,
    speakers_expected: int | None = None,
) -> Transcript:
    """Transcribe `audio`, reusing a cached transcript for the same file and provider."""
    digest = file_sha256(audio)
    cache_path = cache_dir / "transcripts" / f"{digest[:24]}.{provider_name}.json"
    if cache_path.exists():
        transcript = Transcript.model_validate_json(cache_path.read_text())
        ledger.add(LedgerEntry(provider="transcription", kind="transcribe", label=provider_name, status="cached", usd=0.0, seconds=transcript.duration))
        return transcript

    provider = make_provider(provider_name, api_key, speakers_expected=speakers_expected)
    entry = ledger.add(LedgerEntry(provider="transcription", kind="transcribe", label=provider_name))
    try:
        words, duration, language = provider.transcribe(audio)
    except Exception as exc:
        ledger.update(entry, status="error", error=str(exc))
        raise
    if not words:
        ledger.update(entry, status="error", error="no words returned")
        raise TranscriptionError("transcription returned no words (is the file silent?)")

    transcript = Transcript(provider=provider_name, audio_sha256=digest, duration=duration, words=words, language=language)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(transcript.model_dump_json())
    rate = usd_per_hour if usd_per_hour is not None else DEFAULT_USD_PER_HOUR.get(provider_name)
    usd = duration / 3600 * rate if rate is not None else None
    ledger.update(entry, status="complete", seconds=round(duration, 1), usd=usd)
    return transcript
