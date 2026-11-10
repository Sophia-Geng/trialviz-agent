"""Thin OpenAI adapter: messages in, a schema-valid QueryPlanLLM out.

Structured Outputs (strict) guarantees the JSON matches QueryPlanLLM; business
rules are checked by the caller. Responses are cached by (model, messages) so
example runs are reproducible and cheap.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol

from app.config import settings
from app.schemas.plan import QueryPlanLLM


class PlannerError(RuntimeError):
    """The LLM call failed, timed out or refused."""


class LLMClient(Protocol):
    model: str

    def generate_plan(self, messages: list[dict]) -> QueryPlanLLM: ...


class OpenAIPlanner:
    def __init__(
        self,
        api_key: str | None = settings.openai_api_key,
        model: str = settings.openai_model,
        cache_dir: Path | None = settings.cache_dir / "llm" if settings.cache_enabled else None,
        timeout_s: float = 60.0,
    ) -> None:
        from openai import OpenAI

        if not api_key:
            raise PlannerError("OPENAI_API_KEY is not set")
        self._client = OpenAI(api_key=api_key, timeout=timeout_s, max_retries=2)
        self.model = model
        self._cache_dir = cache_dir

    def generate_plan(self, messages: list[dict]) -> QueryPlanLLM:
        cache_file = self._cache_path(messages)
        if cache_file and cache_file.exists():
            return QueryPlanLLM.model_validate_json(cache_file.read_text(encoding="utf-8"))

        kwargs: dict = {}
        if self.model.startswith(("gpt-5", "o")):
            kwargs["reasoning_effort"] = "low"  # reasoning models do not accept temperature
        else:
            kwargs["temperature"] = 0
        try:
            completion = self._client.chat.completions.parse(
                model=self.model, messages=messages, response_format=QueryPlanLLM, **kwargs
            )
        except Exception as exc:  # network errors, rate limits, invalid key, ...
            raise PlannerError(f"{type(exc).__name__}: {exc}") from exc

        message = completion.choices[0].message
        if message.refusal:
            raise PlannerError(f"model refused: {message.refusal}")
        if message.parsed is None:
            raise PlannerError("model returned no parsable plan")

        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(message.parsed.model_dump_json(), encoding="utf-8")
        return message.parsed

    def _cache_path(self, messages: list[dict]) -> Path | None:
        if self._cache_dir is None:
            return None
        key = json.dumps([self.model, messages], ensure_ascii=False, sort_keys=True)
        return self._cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"
