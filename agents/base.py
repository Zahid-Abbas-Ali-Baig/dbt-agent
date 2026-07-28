"""Shared specialist base — playbook step runner with LLM assist."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llm.base import LLMClient
from logging_util import log_event
from tools.registry import ToolRegistry, ToolResult


@dataclass
class AgentResult:
    ok: bool
    reply: str
    needs_confirmation: bool = False
    confirmation_payload: dict[str, Any] | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    phase: int | None = None


def load_prompt(name: str) -> str:
    root = Path(__file__).resolve().parent.parent / "prompts"
    path = root / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


class BaseAgent:
    name = "base"
    allowlist: set[str] = set()

    def __init__(self, llm: LLMClient, registry: ToolRegistry, project_dir: Path):
        self.llm = llm
        self.registry = registry
        # Resolve so activity.log keys match Open/chat session paths
        try:
            self.project_dir = Path(project_dir).expanduser().resolve()
        except OSError:
            self.project_dir = Path(project_dir)


    def tool(self, name: str, **kwargs) -> ToolResult:
        from activity_log import push_activity

        proj = str(self.project_dir)
        detail_hint = None
        if name.startswith("mcp_") and "tool" in kwargs:
            detail_hint = f"→ {kwargs.get('tool')}"
        push_activity(
            f"Running tool `{name}`" + (f" {detail_hint}" if detail_hint else ""),
            kind="tool",
            agent=self.name,
            project_dir=proj,
        )
        result = self.registry.call(name, self.allowlist, **kwargs)
        status = "ok" if result.ok else "failed"
        push_activity(
            f"Tool `{name}` {status}" + (f" {detail_hint}" if detail_hint else ""),
            kind="tool" if result.ok else "error",
            agent=self.name,
            detail=(result.output or "")[:240],
            project_dir=proj,
        )
        return result

    def llm_fill(self, system: str, user: str, *, max_tokens: int | None = 4000) -> str:
        from activity_log import clear_thinking, push_activity, set_thinking
        from tools.skills_runtime import inject_skills_into_system

        # Cursor IDE attaches SKILL.md automatically; Cursor API does not — inject here.
        system = inject_skills_into_system(system, agent_name=self.name, user=user)

        proj = str(self.project_dir)
        topic = _brain_topic(system, user)
        push_activity(
            f"Brain started: {topic}",
            kind="brain",
            agent=self.name,
            project_dir=proj,
        )
        set_thinking(
            "Waiting for model stream…",
            agent=self.name,
            topic=topic,
            phase="thinking",
            project_dir=proj,
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        def on_event(event: str, payload: dict[str, Any]) -> None:
            if event == "thinking":
                set_thinking(
                    str(payload.get("text") or ""),
                    agent=self.name,
                    topic=topic,
                    phase="thinking",
                    project_dir=proj,
                )
            elif event == "assistant":
                set_thinking(
                    str(payload.get("text") or ""),
                    agent=self.name,
                    topic=topic,
                    phase="writing",
                    project_dir=proj,
                )
            elif event == "tool":
                push_activity(
                    f"Brain tool `{payload.get('name') or 'tool'}` "
                    f"{payload.get('status') or ''}".strip(),
                    kind="tool",
                    agent=self.name,
                    project_dir=proj,
                )
            elif event == "task":
                text = str(payload.get("text") or "").strip()
                if text:
                    set_thinking(
                        text,
                        agent=self.name,
                        topic=topic,
                        phase="thinking",
                        project_dir=proj,
                    )

        try:
            out = self.llm.chat(messages, max_tokens=max_tokens, on_event=on_event)
            clear_thinking(proj)
            preview = (out or "").strip()
            if len(preview) > 500:
                preview = preview[:497] + "…"
            push_activity(
                "Brain replied",
                kind="brain",
                agent=self.name,
                detail=preview or None,
                project_dir=proj,
            )
            return out
        except TypeError:
            # Older client without on_event
            try:
                out = self.llm.chat(messages, max_tokens=max_tokens)
                clear_thinking(proj)
                push_activity("Brain replied", kind="brain", agent=self.name, project_dir=proj)
                return out
            except Exception as exc:  # noqa: BLE001
                clear_thinking(proj)
                log_event("ERROR", "llm_fill_failed", agent=self.name, error=str(exc))
                push_activity(
                    f"Brain failed: {exc}",
                    kind="error",
                    agent=self.name,
                    project_dir=proj,
                )
                return f"[LLM unavailable: {exc}]"
        except Exception as exc:  # noqa: BLE001
            clear_thinking(proj)
            log_event("ERROR", "llm_fill_failed", agent=self.name, error=str(exc))
            push_activity(
                f"Brain failed: {exc}",
                kind="error",
                agent=self.name,
                project_dir=proj,
            )
            return f"[LLM unavailable: {exc}]"

    def run(self, phase: int, context: dict[str, Any]) -> AgentResult:
        raise NotImplementedError


def _brain_topic(system: str, user: str) -> str:
    """One short line for Live activity: what the brain is working on."""
    for blob in (user, system):
        for line in (blob or "").splitlines():
            line = " ".join(line.split()).strip()
            if not line:
                continue
            low = line.lower()
            if low.startswith("you are ") or low.startswith("respond as "):
                continue
            if line.startswith("#") or line.startswith("---"):
                continue
            if len(line) > 100:
                line = line[:97] + "…"
            return line
    return "working on a response"
