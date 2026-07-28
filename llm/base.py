"""LLM client protocol — all providers implement chat()."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

StreamCallback = Callable[[str, dict[str, Any]], None]


@runtime_checkable
class LLMClient(Protocol):
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        on_event: StreamCallback | None = None,
    ) -> str:
        """Return assistant text for a chat completion.

        Optional on_event(event_name, payload) receives live stream events
        such as thinking / assistant / tool.
        """
        ...


class LLMError(RuntimeError):
    """Raised when an LLM call fails."""
