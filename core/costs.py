"""Cost ledger: every paid API call gets an entry, and the report is built from it.

Entries are appended to a JSONL event log as they change, so a crashed run still
leaves a record of what was spent. Magic Hour costs are tracked in credits and
converted to dollars with a configurable rate; other providers record dollars
directly when the price is known.
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

DEFAULT_CREDITS_PER_USD = 400.0


@dataclass
class LedgerEntry:
    provider: str  # "magichour" | "transcription" | "llm"
    kind: str  # e.g. "talking_photo", "transcribe", "select_moments"
    label: str  # human-readable, e.g. "clip 2 / Alice"
    job_id: str | None = None
    status: str = "created"  # created | complete | error | canceled | cached | skipped
    credits: int | None = None  # Magic Hour credits; final once status is terminal
    usd: float | None = None  # direct dollar cost where known
    seconds: float | None = None  # billed media seconds
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class CostLedger:
    def __init__(self, path: Path | None = None, credits_per_usd: float = DEFAULT_CREDITS_PER_USD):
        self.path = Path(path) if path else None
        self.credits_per_usd = credits_per_usd
        self.entries: list[LedgerEntry] = []
        self.stage_seconds: dict[str, float] = defaultdict(float)
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, entry: LedgerEntry) -> LedgerEntry:
        with self._lock:
            self.entries.append(entry)
            self._append_event(entry)
        return entry

    def update(self, entry: LedgerEntry, **changes: Any) -> LedgerEntry:
        with self._lock:
            for key, value in changes.items():
                if key == "meta":
                    entry.meta.update(value)
                else:
                    setattr(entry, key, value)
            entry.updated_at = time.time()
            self._append_event(entry)
        return entry

    def _append_event(self, entry: LedgerEntry) -> None:
        if not self.path:
            return
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(entry), default=str) + "\n")

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            with self._lock:
                self.stage_seconds[name] += time.monotonic() - started

    def credits_to_usd(self, credits: float | None) -> float | None:
        if credits is None:
            return None
        return credits / self.credits_per_usd

    def magichour_credits(self, label_prefix: str | None = None) -> int:
        return sum(
            e.credits or 0
            for e in self.entries
            if e.provider == "magichour" and (label_prefix is None or e.label.startswith(label_prefix))
        )

    def usd_for(self, provider: str) -> float | None:
        values = [e.usd for e in self.entries if e.provider == provider and e.usd is not None]
        return sum(values) if values else None

    def failures(self) -> list[LedgerEntry]:
        return [e for e in self.entries if e.status in ("error", "canceled")]

    def totals(self) -> dict[str, Any]:
        mh_credits = self.magichour_credits()
        mh_usd = self.credits_to_usd(mh_credits) or 0.0
        llm_usd = self.usd_for("llm")
        stt_usd = self.usd_for("transcription")
        return {
            "magichour_credits": mh_credits,
            "magichour_usd": round(mh_usd, 4),
            "llm_usd": None if llm_usd is None else round(llm_usd, 4),
            "transcription_usd": None if stt_usd is None else round(stt_usd, 4),
            "total_usd": round(mh_usd + (llm_usd or 0.0) + (stt_usd or 0.0), 4),
            "credits_per_usd": self.credits_per_usd,
            "failures": len(self.failures()),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "totals": self.totals(),
            "stage_seconds": {k: round(v, 2) for k, v in self.stage_seconds.items()},
            "entries": [asdict(e) for e in self.entries],
        }
