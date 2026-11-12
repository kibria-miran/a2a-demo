"""Thin NVIDIA NIM client (OpenAI-compatible), with token accounting.

``LLMError`` is a normal negotiation failure (E-50): the agents retry once,
then use the deterministic fallback. It is not a ``BaseException``, so the
server's turn guard does not turn it into ``failed``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src import config

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


def token_usage(role: str | None = None) -> TokenUsage | dict[str, TokenUsage]:
    if role is not None:
        return _USAGE.setdefault(role, TokenUsage())
    return {name: TokenUsage(value.input, value.output) for name, value in _USAGE.items()}


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
) -> str:
    """Call the chat completions endpoint and return the raw content string."""

    if not config.NIM_MODEL:
        raise LLMError("NIM_MODEL is not configured")
    try:
        response = _get_client().chat.completions.create(
            model=config.NIM_MODEL,
            messages=messages,
            temperature=temperature,
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


def default_chat() -> Any | None:
    """The real client only when both a model and a key are configured."""

    if config.NIM_MODEL and config.NVIDIA_API_KEY:
        return chat
    return None
