"""JSONL operational logging."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parent
_LOG_PATH = _AGENT_ROOT / "logs" / "agent.log"


def log_event(level: str, event: str, **fields) -> None:
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level.upper(),
        "event": event,
        **fields,
    }
    with _LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")

    # Mirror a short line into the live activity feed when a project is in scope
    try:
        from activity_log import push_activity

        agent = fields.get("agent") or fields.get("specialist")
        detail = fields.get("error") or fields.get("path") or fields.get("phase")
        msg = event.replace("_", " ")
        if agent:
            msg = f"{agent}: {msg}"
        push_activity(
            msg,
            kind="error" if level.upper() == "ERROR" else "event",
            agent=str(agent) if agent else None,
            detail=str(detail) if detail is not None else None,
        )
    except Exception:  # noqa: BLE001
        pass
