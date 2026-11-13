"""Thin NVIDIA NIM client (OpenAI-compatible), with token accounting.

``LLMError`` is a normal negotiation failure (E-50): the agents retry once,
then use the deterministic fallback. It is not a ``BaseException``, so the
server's turn guard does not turn it into ``failed``.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any

from src import config

_MAX_TOKENS = 2048  # the free-tier reasoning model thinks before it answers; don't starve it

REASON_LLM_ERROR = "LLM error"


class LLMError(Exception):
    """Network/timeout/non-2xx/malformed-envelope failure (E-50)."""


@dataclass
class TokenUsage:
    input: int = 0
    output: int = 0


_USAGE: dict[str, TokenUsage] = {}
_CLIENT: Any | None = None


def reset_token_usage() -> None:
    _USAGE.clear()


def token_usage(role: str) -> TokenUsage:
    return _USAGE.setdefault(role, TokenUsage())


def token_usage_all() -> dict[str, TokenUsage]:
    return {name: TokenUsage(value.input, value.output) for name, value in _USAGE.items()}


def usage_delta(before: dict[str, TokenUsage]) -> tuple[int, int]:
    """Return (tokens_in, tokens_out) accumulated since ``before`` was taken."""

    after = token_usage_all()
    roles = set(before) | set(after)
    tokens_in = sum(
        after.get(role, TokenUsage()).input - before.get(role, TokenUsage()).input
        for role in roles
    )
    tokens_out = sum(
        after.get(role, TokenUsage()).output - before.get(role, TokenUsage()).output
        for role in roles
    )
    return tokens_in, tokens_out


def reset_client() -> None:
    """Drop the cached OpenAI client (tests inject a fake instead)."""

    global _CLIENT
    _CLIENT = None


def _get_client() -> Any:
    global _CLIENT
    if _CLIENT is None:
        from openai import OpenAI

        _CLIENT = OpenAI(
            base_url=config.NIM_BASE_URL,
            api_key=config.NVIDIA_API_KEY or "not-configured",
            timeout=config.LLM_TIMEOUT,
        )
    return _CLIENT


def chat(
    messages: list[dict[str, Any]],
    role: str = "proposer",
    temperature: float = config.LLM_TEMPERATURE,
    model: str | None = None,
) -> str:
    """Call the chat completions endpoint and return the raw content string.

    ``model`` overrides ``config.NIM_MODEL`` for one call. X5 uses it to give
    each side a different model without mutating global config.
    """

    selected_model = model or config.NIM_MODEL
    if not selected_model:
        raise LLMError("NIM_MODEL is not configured")
    try:
        response = _get_client().chat.completions.create(
            model=selected_model,
            messages=messages,
            temperature=temperature,
            max_tokens=_MAX_TOKENS,
        )
    except Exception as exc:  # SDK/network/timeout/non-2xx all become LLMError
        raise LLMError(f"LLM request failed: {type(exc).__name__}") from exc

    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise LLMError("malformed LLM envelope") from exc
    if not isinstance(content, str) or not content.strip():
        raise LLMError("malformed LLM envelope")

    usage = getattr(response, "usage", None)
    entry = _USAGE.setdefault(role, TokenUsage())
    entry.input += int(getattr(usage, "prompt_tokens", 0) or 0)
    entry.output += int(getattr(usage, "completion_tokens", 0) or 0)
    return content


def default_chat(model: str | None = None) -> Any | None:
    """The real client only when both a model and a key are configured.

    With ``model`` set (X5), return a callable bound to that model. Without
    it, return ``chat`` itself so existing wiring (and its identity test) is
    unchanged.
    """

    selected_model = model or config.NIM_MODEL
    if selected_model and config.NVIDIA_API_KEY:
        if model is None:
            return chat
        return functools.partial(chat, model=model)
    return None
