"""LLM factory — select provider from environment."""

from __future__ import annotations

import os

from llm.base import LLMClient, LLMError
from llm.cursor_client import CursorClient
from llm.openai_compatible import OpenAICompatibleClient

SUPPORTED = ("openai", "ollama", "vllm", "openai_compatible", "cursor")


def get_llm_client(*, cwd: str | None = None) -> LLMClient:
    provider = (os.getenv("LLM_PROVIDER") or "openai_compatible").strip().lower()
    model = (os.getenv("MODEL") or "").strip()
    if not model:
        raise LLMError("MODEL env var is required")

    if provider == "cursor":
        api_key = (os.getenv("CURSOR_API_KEY") or os.getenv("API_KEY") or "").strip()
        if not api_key:
            raise LLMError("CURSOR_API_KEY (or API_KEY) required for LLM_PROVIDER=cursor")
        return CursorClient(api_key=api_key, model=model, cwd=cwd)

    api_key = (os.getenv("API_KEY") or "not-needed").strip()
    base_url = (os.getenv("BASE_URL") or "").strip() or None

    if provider == "openai":
        # Official OpenAI — base_url optional
        return OpenAICompatibleClient(base_url=base_url, api_key=api_key, model=model)

    if provider == "ollama":
        return OpenAICompatibleClient(
            base_url=base_url or "http://localhost:11434/v1",
            api_key=api_key or "ollama",
            model=model,
        )

    if provider == "vllm":
        return OpenAICompatibleClient(
            base_url=base_url or "http://localhost:8000/v1",
            api_key=api_key or "not-needed",
            model=model,
        )

    if provider == "openai_compatible":
        if not base_url:
            raise LLMError("BASE_URL required for LLM_PROVIDER=openai_compatible")
        return OpenAICompatibleClient(base_url=base_url, api_key=api_key, model=model)

    raise LLMError(
        f"Unknown LLM_PROVIDER={provider!r}. Supported: {', '.join(SUPPORTED)}"
    )
