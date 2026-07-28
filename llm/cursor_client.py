"""Cursor API adapter — prefers Node @cursor/sdk bridge (reliable on Windows)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from llm.base import LLMError

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_BRIDGE = _AGENT_ROOT / "scripts" / "cursor_chat.mjs"

StreamCallback = Callable[[str, dict[str, Any]], None]


class CursorClient:
    """Uses Cursor as a text brain. Tools stay in our orchestrator."""

    def __init__(self, *, api_key: str, model: str, cwd: str | None = None):
        self.api_key = api_key
        self.model = model
        self.cwd = cwd or os.getcwd()

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        on_event: StreamCallback | None = None,
    ) -> str:
        del temperature, max_tokens
        prompt = self._messages_to_prompt(messages)

        # Prefer Node bridge (avoids Python SDK WinError 10038 on local bridge)
        if _BRIDGE.exists():
            try:
                return self._chat_via_node(prompt, on_event=on_event)
            except LLMError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_node_err = exc
            else:
                last_node_err = None
        else:
            last_node_err = None

        try:
            return self._chat_via_python_sdk(prompt, on_event=on_event)
        except Exception as exc:  # noqa: BLE001
            detail = f"Python SDK: {exc}"
            if last_node_err:
                detail = f"Node bridge: {last_node_err}; {detail}"
            raise LLMError(f"Cursor chat failed. {detail}") from exc

    def _chat_via_node(self, prompt: str, *, on_event: StreamCallback | None = None) -> str:
        node = os.getenv("CURSOR_NODE_EXE") or "node"
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".txt", delete=False
        ) as fh:
            fh.write(prompt)
            prompt_path = fh.name
        result_text = ""
        result_error = ""
        got_result = False
        code = -1
        stderr = ""
        try:
            env = os.environ.copy()
            env["CURSOR_API_KEY"] = self.api_key
            # UTF-8 end-to-end: binary pipes + explicit decode (locale-independent).
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("PYTHONUTF8", "1")
            proc = subprocess.Popen(
                [
                    node,
                    str(_BRIDGE),
                    "--cwd",
                    str(self.cwd),
                    "--model",
                    self.model,
                    "--prompt-file",
                    prompt_path,
                ],
                cwd=str(_AGENT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                env=env,
            )
            assert proc.stdout is not None
            try:
                for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event = str(payload.get("event") or "")
                    if on_event and event and event != "result":
                        try:
                            on_event(event, payload)
                        except Exception:  # noqa: BLE001
                            pass
                    if event == "result":
                        got_result = True
                        if payload.get("ok"):
                            result_text = str(payload.get("result") or "").strip()
                            thinking = payload.get("thinking")
                            if on_event and thinking:
                                try:
                                    on_event("thinking", {"text": thinking})
                                except Exception:  # noqa: BLE001
                                    pass
                        else:
                            result_error = str(
                                payload.get("error") or "Cursor bridge failed"
                            )
                if proc.stderr is not None:
                    stderr = (proc.stderr.read() or b"").decode(
                        "utf-8", errors="replace"
                    )
                code = proc.wait(timeout=30)
            except Exception:
                # Keep a finished result if the pipe dies after the result line.
                if got_result and result_text and not result_error:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    return result_text
                raise
        finally:
            try:
                Path(prompt_path).unlink(missing_ok=True)
            except OSError:
                pass

        if result_error:
            raise LLMError(result_error)
        if not got_result:
            raise LLMError(
                f"Node Cursor bridge returned no result. "
                f"exit={code} stderr={stderr[:500]!r}"
            )
        if not result_text:
            raise LLMError("Cursor agent returned empty result")
        return result_text

    def _chat_via_python_sdk(
        self, prompt: str, *, on_event: StreamCallback | None = None
    ) -> str:
        try:
            from cursor_sdk import Agent, LocalAgentOptions
        except ImportError as exc:
            raise LLMError(
                "cursor-sdk is not installed and Node bridge failed. "
                "pip install cursor-sdk and npm install in dbt-agent/"
            ) from exc

        with Agent.create(
            model=self.model,
            api_key=self.api_key,
            local=LocalAgentOptions(cwd=str(Path(self.cwd))),
        ) as agent:
            run = agent.send(prompt)
            thinking = ""
            streamed = ""
            try:
                for message in run.stream():
                    mtype = getattr(message, "type", None) or (
                        message.get("type") if isinstance(message, dict) else None
                    )
                    if mtype == "thinking":
                        chunk = getattr(message, "text", None)
                        if chunk is None and isinstance(message, dict):
                            chunk = message.get("text")
                        chunk = str(chunk or "")
                        if chunk:
                            if chunk.startswith(thinking) or thinking.startswith(chunk):
                                thinking = chunk if len(chunk) >= len(thinking) else thinking
                            else:
                                thinking += chunk
                            if on_event:
                                on_event("thinking", {"text": thinking})
                    elif mtype == "assistant":
                        content = None
                        msg = getattr(message, "message", None)
                        if msg is not None:
                            content = getattr(msg, "content", None)
                        if content is None and isinstance(message, dict):
                            content = (message.get("message") or {}).get("content")
                        piece = ""
                        for block in content or []:
                            btype = getattr(block, "type", None) or (
                                block.get("type") if isinstance(block, dict) else None
                            )
                            if btype == "text":
                                piece += str(
                                    getattr(block, "text", None)
                                    or (block.get("text") if isinstance(block, dict) else "")
                                    or ""
                                )
                        if piece:
                            if piece.startswith(streamed) or streamed.startswith(piece):
                                streamed = piece if len(piece) >= len(streamed) else streamed
                            else:
                                streamed += piece
                            if on_event:
                                on_event("assistant", {"text": streamed})
                    elif mtype == "tool_call" and on_event:
                        on_event(
                            "tool",
                            {
                                "name": getattr(message, "name", None)
                                or (message.get("name") if isinstance(message, dict) else "tool"),
                                "status": getattr(message, "status", None)
                                or (message.get("status") if isinstance(message, dict) else None),
                            },
                        )
            except Exception:  # noqa: BLE001
                pass
            result = run.wait()
            text = streamed or getattr(result, "result", None) or str(result)
            if not str(text).strip():
                raise LLMError("Cursor agent returned empty result")
            return str(text).strip()

    @staticmethod
    def _messages_to_prompt(messages: list[dict[str, str]]) -> str:
        parts: list[str] = []
        for m in messages:
            role = m.get("role", "user").upper()
            parts.append(f"[{role}]\n{m.get('content', '')}")
        parts.append(
            "\nRespond as the assistant with the final answer only. "
            "Stay inside this dbt/Power BI engagement task. "
            "Refuse unrelated questions in one short line. "
            "Do not invent warehouse numbers not present in the messages."
        )
        return "\n\n".join(parts)
