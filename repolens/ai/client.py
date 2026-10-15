"""Thin wrapper around the Anthropic SDK that returns schema-validated JSON.

The rest of the app depends only on the :class:`LLMClient` protocol, so tests
substitute a fake and no network call is ever made in unit tests.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic

from repolens.config import get_model, get_secret

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept `fallbacks: "default"` on the Claude API. Haiku 5.5 has no
# server-side fallback, so it is called without one.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}


class AIError(Exception):
    """A user-facing AI failure. The message never contains credentials."""


@dataclass
class LLMResponse:
    data: dict[str, Any]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class LLMClient(Protocol):
    def complete_json(
        self,
        system: str,
        shared_context: str,
        task_context: str,
        task: str,
        schema: dict[str, Any],
    ) -> LLMResponse: ...


class AnthropicLLM:
    """Calls Claude with structured output. Credentials come from env / Streamlit secrets."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        effort: str = "high",
        max_tokens: int = 32_000,
        client: anthropic.Anthropic | None = None,
    ):
        key = api_key or get_secret("ANTHROPIC_API_KEY")
        if client is None and not key:
            raise AIError(
                "No Anthropic API key configured. Set ANTHROPIC_API_KEY in your environment "
                "or .streamlit/secrets.toml."
            )
        self._client = client or anthropic.Anthropic(api_key=key, max_retries=2, timeout=600.0)
        self.model = model or get_model()
        self.effort = effort
        self.max_tokens = max_tokens
        self._use_fallbacks = self.model in FALLBACK_MODELS

    def complete_json(
        self,
        system: str,
        shared_context: str,
        task_context: str,
        task: str,
        schema: dict[str, Any],
    ) -> LLMResponse:
        # The repository-wide context is stable across experiences, so it sits
        # first and carries the cache breakpoint; question-specific text follows.
        content: list[dict[str, Any]] = [
            {"type": "text", "text": shared_context, "cache_control": {"type": "ephemeral"}},
        ]
        if task_context:
            content.append({"type": "text", "text": task_context})
        content.append({"type": "text", "text": task})

        params: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        )
        try:
            message = self._send(params)
        except anthropic.AuthenticationError as exc:
            raise AIError("The Anthropic API key was rejected.") from exc
        except anthropic.PermissionDeniedError as exc:
            raise AIError("The Anthropic API key lacks permission for this model.") from exc
        except anthropic.RateLimitError as exc:
            raise AIError("Anthropic rate limit reached. Wait a moment and try again.") from exc
        except anthropic.BadRequestError as exc:
            log.warning("Anthropic rejected request: %s", exc.message)
            raise AIError(f"The AI request was rejected: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise AIError(f"Anthropic API error (HTTP {exc.status_code}). Try again later.") from exc
        except anthropic.APIConnectionError as exc:
            raise AIError("Could not reach the Anthropic API. Check your connection.") from exc

        if message.stop_reason == "refusal":
            raise AIError("The model declined to answer this request.")
        if message.stop_reason == "max_tokens":
            raise AIError("The AI response was cut off before it finished. Try a narrower question.")
        text = next((b.text for b in message.content if b.type == "text"), None)
        if not text:
            raise AIError("The AI returned an empty response.")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIError("The AI response was not valid JSON.") from exc

        usage = message.usage
        return LLMResponse(
            data=data,
            model=message.model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )

    def _send(self, params: dict[str, Any]):
        if self._use_fallbacks:
            try:
                # On a safety decline, the API re-runs the request on a recommended
                # fallback model instead of returning a refusal.
                with self._client.beta.messages.stream(
                    **params, betas=[FALLBACK_BETA], fallbacks="default"
                ) as stream:
                    return stream.get_final_message()
            except anthropic.BadRequestError as exc:
                if "fallback" not in str(exc.message).lower():
                    raise
                log.info("Server-side fallbacks unavailable for this model; retrying without them")
                self._use_fallbacks = False
        with self._client.messages.stream(**params) as stream:
            return stream.get_final_message()
