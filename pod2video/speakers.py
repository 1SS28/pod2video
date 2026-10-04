"""Map diarization labels (A, B, ...) to the host names given on the command line.

v1 is interactive: show each speaker's talk time and a sample line, ask the user to
confirm, and remember the answer per feed (or per audio file) for the next run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from pod2video.models import Host, Transcript


class SpeakerMapError(RuntimeError):
    pass


def sample_line(transcript: Transcript, speaker: str, max_words: int = 24) -> str:
    """The longest sentence the speaker says (trimmed), which is usually the most recognisable."""
    best = ""
    for sentence in transcript.sentences():
        if sentence.speaker == speaker and len(sentence.text) > len(best):
            best = sentence.text
    words = best.split()
    return " ".join(words[:max_words]) + (" …" if len(words) > max_words else "")


def parse_map(value: str, hosts: list[Host]) -> dict[str, str]:
    """`A=Alice,B=Bob` -> {"A": "Alice", "B": "Bob"}."""
    names = {h.name for h in hosts}
    mapping: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in value.split(","))):
        label, _, name = part.partition("=")
        if not name or name.strip() not in names:
            raise SpeakerMapError(f"bad --speaker-map entry '{part}' (hosts are: {', '.join(sorted(names))})")
        mapping[label.strip()] = name.strip()
    return mapping


def _store_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / "speaker_maps" / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.json"


def resolve(
    transcript: Transcript,
    hosts: list[Host],
    *,
    store_key: str,
    cache_dir: Path,
    console: Console,
    explicit: dict[str, str] | None = None,
    interactive: bool = True,
) -> dict[str, str]:
    """Return {speaker_label: host_name} covering every host."""
    ranked = transcript.speakers_by_talk_time()
    labels = [label for label, _ in ranked]
    if len(labels) < len(hosts):
        raise SpeakerMapError(
            f"transcript has {len(labels)} speaker(s) but {len(hosts)} hosts were given; "
            "use --layout single or check the audio"
        )

    store = _store_path(cache_dir, store_key)
    if explicit:
        mapping = explicit
    else:
        _show_speakers(transcript, ranked, console)
        saved = json.loads(store.read_text()) if store.exists() else None
        if saved and set(saved).issubset(labels) and set(saved.values()) == {h.name for h in hosts}:
            summary = ", ".join(f"{k} = {v}" for k, v in saved.items())
            if not interactive or Confirm.ask(f"Use the saved mapping ({summary})?", default=True, console=console):
                return saved
        if interactive:
            mapping = {}
            for i, host in enumerate(hosts):
                remaining = [label for label in labels if label not in mapping]
                default = labels[i] if labels[i] in remaining else remaining[0]
                choice = Prompt.ask(f"Which speaker is [bold]{host.name}[/]?", choices=remaining, default=default, console=console)
                mapping[choice] = host.name
        else:
            mapping = {label: host.name for label, host in zip(labels, hosts)}
            console.print(
                "[yellow]Non-interactive: mapped hosts by talk time "
                f"({', '.join(f'{k} = {v}' for k, v in mapping.items())}). Pass --speaker-map to override.[/]"
            )

    missing = {h.name for h in hosts} - set(mapping.values())
    if missing:
        raise SpeakerMapError(f"no speaker mapped to: {', '.join(sorted(missing))}")
    unknown = set(mapping) - set(labels)
    if unknown:
        raise SpeakerMapError(f"speaker label(s) not in transcript: {', '.join(sorted(unknown))} (found {', '.join(labels)})")
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(mapping, indent=2))
    return mapping


def _show_speakers(transcript: Transcript, ranked: list[tuple[str, float]], console: Console) -> None:
    table = Table(title="Speakers detected", show_lines=False)
    table.add_column("Label", style="bold")
    table.add_column("Talk time", justify="right")
    table.add_column("Sample line")
    for label, seconds in ranked:
        table.add_row(label, f"{seconds / 60:.1f} min", sample_line(transcript, label))
    console.print(table)
