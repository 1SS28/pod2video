"""Settings loaded from the environment (and a local .env file)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    magic_hour_api_key: str = ""
    transcription_api_key: str = ""
    transcription_provider: str = "assemblyai"  # assemblyai | deepgram
    # Optional, for the report. Check your provider's current price per audio hour.
    transcription_usd_per_hour: float | None = None

    llm_api_key: str = ""
    llm_model: str = ""
    llm_provider: str | None = None  # inferred from the model id when unset
    llm_effort: str | None = None  # e.g. "high"; sent only when set
    llm_base_url: str | None = None
    llm_usd_per_mtok_in: float | None = None
    llm_usd_per_mtok_out: float | None = None

    # Pricing for the report: "auto" reads your plan from your Magic Hour account; or a key from
    # core/plans.py (e.g. creator-annual). MAGIC_HOUR_CREDITS_PER_USD overrides both.
    magic_hour_plan: str = "auto"
    magic_hour_credits_per_usd: float | None = None
    pod2video_cache_dir: Path = Field(default_factory=lambda: Path.home() / ".cache" / "pod2video")

    @property
    def cache_dir(self) -> Path:
        self.pod2video_cache_dir.mkdir(parents=True, exist_ok=True)
        return self.pod2video_cache_dir
