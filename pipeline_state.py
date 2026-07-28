"""Typed pipeline state persisted on disk (engagement + agent memory)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class QualityState:
    last_checkpoint: str | None = None
    status: str | None = None  # pass | needs_rework | needs_human_waiver
    rework_count: int = 0
    findings_path: str | None = None
    rework_target: str | None = None  # modeling | bi


@dataclass
class PipelineState:
    current_phase: int = 0
    brief_status: str = "unknown"  # pending approval | approved | unknown
    desktop_import_ok: bool = False
    wireframe_approved: bool = False
    phase8_ok: bool = False
    phase8_relationships_done: bool = False
    phase8_visuals_done: bool = False
    last_specialist: str | None = None
    last_error: str | None = None
    quality: QualityState = field(default_factory=QualityState)
    last_artifacts: dict[str, Any] = field(default_factory=dict)
    # Conversational intake + yes/no run gates
    intake_field: str | None = None  # e.g. PROJECT_NAME or REQUIREMENTS
    pending_approval: dict[str, Any] | None = None  # {command, label}
    warehouse_confirmed: bool = False
    warehouse_fingerprint: str | None = None
    brain_confirmed: bool = False
    env_precheck_ok: bool = False
    # Ask human when path confidence is below this % (set during intake)
    path_confidence_threshold: int | None = None
    path_confidence_confirmed: bool = False
    # Phase 1: proposed / confirmed analytics table scope
    table_scope: dict[str, Any] | None = None
    # Phase 1: low-confidence path clarifications (one question at a time)
    path_clarify: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PipelineState":
        q = data.get("quality") or {}
        quality = QualityState(
            last_checkpoint=q.get("last_checkpoint"),
            status=q.get("status"),
            rework_count=int(q.get("rework_count") or 0),
            findings_path=q.get("findings_path"),
            rework_target=q.get("rework_target"),
        )
        pending = data.get("pending_approval")
        path_clarify = data.get("path_clarify")
        thresh_raw = data.get("path_confidence_threshold")
        try:
            thresh = int(thresh_raw) if thresh_raw is not None else None
        except (TypeError, ValueError):
            thresh = None
        if thresh is not None:
            thresh = max(1, min(100, thresh))
        return cls(
            current_phase=int(data.get("current_phase") or 0),
            brief_status=str(data.get("brief_status") or "unknown"),
            desktop_import_ok=bool(data.get("desktop_import_ok")),
            wireframe_approved=bool(data.get("wireframe_approved")),
            phase8_ok=bool(data.get("phase8_ok")),
            phase8_relationships_done=bool(data.get("phase8_relationships_done")),
            phase8_visuals_done=bool(data.get("phase8_visuals_done")),
            last_specialist=data.get("last_specialist"),
            last_error=data.get("last_error"),
            quality=quality,
            last_artifacts=dict(data.get("last_artifacts") or {}),
            intake_field=data.get("intake_field"),
            pending_approval=dict(pending) if isinstance(pending, dict) else None,
            warehouse_confirmed=bool(data.get("warehouse_confirmed")),
            warehouse_fingerprint=data.get("warehouse_fingerprint"),
            brain_confirmed=bool(data.get("brain_confirmed")),
            env_precheck_ok=bool(data.get("env_precheck_ok")),
            path_confidence_threshold=thresh,
            path_confidence_confirmed=bool(data.get("path_confidence_confirmed")),
            table_scope=dict(data["table_scope"])
            if isinstance(data.get("table_scope"), dict)
            else None,
            path_clarify=dict(path_clarify) if isinstance(path_clarify, dict) else None,
        )


def state_path(project_dir: Path) -> Path:
    return project_dir / ".dbt_agent_state.json"


def load_state(project_dir: Path) -> PipelineState:
    path = state_path(project_dir)
    if not path.exists():
        return PipelineState()
    data = json.loads(path.read_text(encoding="utf-8"))
    return PipelineState.from_dict(data)


def save_state(project_dir: Path, state: PipelineState) -> None:
    path = state_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")


def sync_brief_status(project_dir: Path, state: PipelineState) -> PipelineState:
    """Read design_brief.md Status from disk (disk truth)."""
    brief = project_dir / "design_brief.md"
    if not brief.exists():
        state.brief_status = "unknown"
        return state
    text = brief.read_text(encoding="utf-8")
    lower = text.lower()
    if "status:" in lower:
        for line in text.splitlines():
            if line.lower().strip().startswith("**status:**") or line.lower().strip().startswith("status:"):
                val = line.split(":", 1)[-1].strip().strip("*").strip().lower()
                if "approved" in val and "pending" not in val:
                    state.brief_status = "approved"
                elif "pending" in val:
                    state.brief_status = "pending approval"
                else:
                    state.brief_status = val
                break
    return state
