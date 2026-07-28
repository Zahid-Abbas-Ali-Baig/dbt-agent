"""OpenAI-compatible client (OpenAI, Ollama, vLLM, custom gateways)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from openai import OpenAI

from llm.base import LLMError

StreamCallback = Callable[[str, dict[str, Any]], None]


class OpenAICompatibleClient:
    def __init__(self, *, base_url: str | None, api_key: str, model: str):
        kwargs: dict = {"api_key": api_key or "not-needed"}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)
        self.model = model

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        on_event: StreamCallback | None = None,
    ) -> str:
        try:
            kwargs: dict = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "stream": True,
            }
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens
            stream = self._client.chat.completions.create(**kwargs)
            parts: list[str] = []
            reasoning: list[str] = []
            for chunk in stream:
                choice = (chunk.choices or [None])[0]
                if choice is None:
                    continue
                delta = choice.delta
                # Some reasoning models expose reasoning/thinking fields on delta
                for attr in ("reasoning_content", "reasoning", "thinking"):
                    piece = getattr(delta, attr, None)
                    if piece:
                        reasoning.append(str(piece))
                        if on_event:
                            on_event("thinking", {"text": "".join(reasoning)})
                content = getattr(delta, "content", None)
                if content:
                    parts.append(str(content))
                    if on_event:
                        on_event("assistant", {"text": "".join(parts)})
            text = "".join(parts).strip()
            if not text:
                # Fallback non-stream if provider ignored stream or returned empty
                return self._chat_once(messages, temperature=temperature, max_tokens=max_tokens)
            return text
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Providers that reject stream=True
            try:
                return self._chat_once(messages, temperature=temperature, max_tokens=max_tokens)
            except Exception:
                raise LLMError(f"OpenAI-compatible chat failed: {exc}") from exc

    def _chat_once(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int | None,
    ) -> str:
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        resp = self._client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content
        if not content or not str(content).strip():
            raise LLMError("LLM returned empty content")
        return str(content).strip()
