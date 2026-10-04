"""Provider-agnostic "give me JSON matching this schema" calls (Anthropic or OpenAI).

The provider is inferred from the model id (`claude-*` -> Anthropic, everything else
-> OpenAI) unless set explicitly. Schemas must be strict-mode compatible: every
object lists all properties in `required` and sets `additionalProperties: false`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# USD per million tokens (input, output). Override with LLM_USD_PER_MTOK_IN/OUT.
KNOWN_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# Models that accept server-side refusal fallbacks (`fallbacks: "default"`).
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    data: Any
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    usd: float | None


def infer_provider(model: str) -> str:
    return "anthropic" if model.startswith("claude") else "openai"


class LLM:
    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        provider: str | None = None,
        effort: str | None = None,
        max_tokens: int = 16000,
        usd_per_mtok_in: float | None = None,
        usd_per_mtok_out: float | None = None,
        base_url: str | None = None,
    ):
        if not model:
            raise LLMError("LLM_MODEL is not set")
        if not api_key:
            raise LLMError("LLM_API_KEY is not set")
        self.model = model
        self.provider = provider or infer_provider(model)
        self.effort = effort
        self.max_tokens = max_tokens
        known = KNOWN_PRICES.get(model, (None, None))
        self.price_in = usd_per_mtok_in if usd_per_mtok_in is not None else known[0]
        self.price_out = usd_per_mtok_out if usd_per_mtok_out is not None else known[1]

        if self.provider == "anthropic":
            import anthropic

            self._client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
        elif self.provider == "openai":
            try:
                import openai
            except ImportError as exc:
                raise LLMError("OpenAI models need the openai extra: pip install 'pod2video[openai]'") from exc
            self._client = openai.OpenAI(api_key=api_key, base_url=base_url)
        else:
            raise LLMError(f"unknown LLM provider '{self.provider}' (use anthropic or openai)")

    def json(self, *, system: str, prompt: str, schema: dict[str, Any], name: str = "result") -> LLMResult:
        if self.provider == "anthropic":
            text, tokens_in, tokens_out = self._anthropic(system, prompt, schema)
        else:
            text, tokens_in, tokens_out = self._openai(system, prompt, schema, name)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned invalid JSON: {text[:300]}") from exc
        return LLMResult(
            data=data,
            provider=self.provider,
            model=self.model,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            usd=self._usd(tokens_in, tokens_out),
        )

    def _usd(self, tokens_in: int | None, tokens_out: int | None) -> float | None:
        if None in (self.price_in, self.price_out, tokens_in, tokens_out):
            return None
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000

    def _anthropic(self, system: str, prompt: str, schema: dict[str, Any]) -> tuple[str, int, int]:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": output_config,
        }
        if self.model in _FALLBACK_MODELS:
            # If a safety classifier declines, the API re-runs the request on a suitable fallback model.
            response = self._client.beta.messages.create(**params, betas=[_FALLBACK_BETA], fallbacks="default")
        else:
            response = self._client.messages.create(**params)

        if response.stop_reason == "refusal":
            raise LLMError(f"model declined the request: {getattr(response, 'stop_details', None)}")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"response hit max_tokens ({self.max_tokens}); raise LLM max_tokens")
        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise LLMError("model returned no text block")
        return text, response.usage.input_tokens, response.usage.output_tokens

    def _openai(self, system: str, prompt: str, schema: dict[str, Any], name: str) -> tuple[str, int | None, int | None]:
        kwargs: dict[str, Any] = {}
        if self.effort:
            kwargs["reasoning_effort"] = self.effort
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
            max_completion_tokens=self.max_tokens,
            **kwargs,
        )
        choice = response.choices[0]
        if getattr(choice.message, "refusal", None):
            raise LLMError(f"model declined the request: {choice.message.refusal}")
        if choice.finish_reason == "length":
            raise LLMError(f"response hit max tokens ({self.max_tokens})")
        usage = response.usage
        return (
            choice.message.content or "",
            usage.prompt_tokens if usage else None,
            usage.completion_tokens if usage else None,
        )
