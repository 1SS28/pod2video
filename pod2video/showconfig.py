"""`pod2video.yml`: one show's settings, for unattended runs (GitHub Actions)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from pod2video.config import Settings
from pod2video.models import Host


class HostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    photo: Path | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _photo_or_description(self) -> "HostConfig":
        if bool(self.photo) == bool(self.description):
            raise ValueError(f"host {self.name}: give exactly one of photo or description")
        return self


class ShowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feed: str
    hosts: list[HostConfig] = Field(min_length=1)
    podcast_name: str | None = None
    logo: str | None = None  # a path in the repo, or "feed" for the podcast artwork
    avatar: Literal["photo", "cartoon"] = "photo"
    clips: int = Field(default=3, ge=1, le=20)  # suggested picks per episode
    candidates: int | None = Field(default=None, ge=1, le=20)  # previews per episode (default 2 x clips)
    min_len: float = 25
    max_len: float = 45
    layout: Literal["auto", "split", "speaker-cut", "single"] = "auto"
    style: str = "bold"
    generation_mode: Literal["prompted", "realistic"] = "prompted"
    plan: str = "auto"
    max_credits_per_run: int | None = Field(default=5000, ge=0)
    speaker_map: dict[str, str] | None = None  # fixed diarization label -> host name, if your labels are stable
    transcription_provider: Literal["assemblyai", "deepgram"] | None = None
    llm_model: str | None = None
    llm_effort: str | None = None

    _base_dir: Path = PrivateAttr(default=Path("."))  # the config file's directory

    def resolve(self, path: Path) -> Path:
        return path if path.is_absolute() else self._base_dir / path

    def host_models(self) -> list[Host]:
        return [
            Host(name=h.name, photo=self.resolve(h.photo) if h.photo else None, description=h.description)
            for h in self.hosts
        ]

    def logo_path(self) -> str | None:
        if not self.logo or self.logo == "feed":
            return self.logo
        return str(self.resolve(Path(self.logo)))

    def speaker_map_arg(self) -> str | None:
        return ",".join(f"{label}={name}" for label, name in self.speaker_map.items()) if self.speaker_map else None

    def settings(self, base: Settings | None = None) -> Settings:
        """Environment settings (API keys) with this show's choices layered on top."""
        base = base or Settings()
        updates = {
            "transcription_provider": self.transcription_provider,
            "llm_model": self.llm_model,
            "llm_effort": self.llm_effort,
        }
        return base.model_copy(update={k: v for k, v in updates.items() if v is not None})


def load(path: Path) -> ShowConfig:
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    config = ShowConfig.model_validate(data)
    config._base_dir = path.resolve().parent
    return config
