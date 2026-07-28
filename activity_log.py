"""Activity feed for the UI — in-memory buffer + append-only engagement log.

Log file (per engagement): ``activity.log`` at the engagement root
(and a copy under ``.dbt_agent/activity.log``).

Plain text in the same shape humans see in Activity History
(``Run · …`` sections + timestamped lines + detail blocks).
Live thinking tokens stay in memory while streaming; the final snapshot is
written when the brain step / request ends. The UI does not reload this file.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MAX = 800  # keep many runs in one engagement session (memory + reload window)
_DETAIL_MAX = 800
_THINKING_MAX = 12000
# Visible at engagement root (next to config.md) — same text as Activity History.
_LOG_NAME = "activity.log"
_LOG_REL = Path(_LOG_NAME)
# Also keep a copy under .dbt_agent for audit tooling.
_LOG_REL_DOT = Path(".dbt_agent") / _LOG_NAME

_lock = threading.Lock()
_buffers: dict[str, deque[dict[str, Any]]] = {}
_thinking: dict[str, dict[str, Any]] = {}
_current_run: dict[str, str] = {}
_run_meta: dict[str, dict[str, Any]] = {}  # run_id -> {label, started}

current_project_key: ContextVar[str | None] = ContextVar("activity_project_key", default=None)


def _normalize_project_dir(project_dir: str | None) -> str | None:
    raw = (project_dir or "").strip()
    if not raw:
        return None
    try:
        return str(Path(raw).expanduser().resolve())
    except OSError:
        return raw


def _key(project_dir: str | None) -> str:
    return _normalize_project_dir(project_dir) or "_global"


def _log_paths(project_dir: str | None) -> list[Path]:
    key = _key(project_dir)
    if key == "_global":
        return []
    root = Path(key)
    return [root / _LOG_REL, root / _LOG_REL_DOT]


def _log_path(project_dir: str | None) -> Path | None:
    paths = _log_paths(project_dir)
    return paths[0] if paths else None


def _clock(ts: str | None) -> str:
    """Match the UI: ISO timestamp → HH:MM:SS (chars 11–19)."""
    s = ts or ""
    return s[11:19] if len(s) >= 19 else ""


def _format_human_entry(entry: dict[str, Any]) -> str:
    """Render one activity entry the way Activity History shows it."""
    clock = _clock(entry.get("ts") if isinstance(entry.get("ts"), str) else None)
    kind = entry.get("kind") or "info"

    if kind == "run":
        label = entry.get("run_label") or entry.get("message") or "Run"
        head = f"Run · {label}"
        # UI puts time on the right of the run head
        line = f"{head}{' ' * max(2, 48 - len(head))}{clock}".rstrip()
        parts = ["", "-" * 56, line, "-" * 56]
        detail = entry.get("detail")
        if detail:
            parts.append(str(detail))
        parts.append("")
        return "\n".join(parts) + "\n"

    agent = entry.get("agent")
    prefix = f"[{agent}] " if agent else ""
    live = " (live)" if entry.get("live") else ""
    msg = entry.get("message") or ""
    head = f"{clock}  {prefix}{msg}{live}".rstrip()
    detail = entry.get("detail")
    if not detail:
        return head + "\n"
    indented = "\n".join(
        ("    " + ln) if ln else "    " for ln in str(detail).splitlines()
    )
    return head + "\n" + indented + "\n"


def _now_entry_base() -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "t": time.time(),
    }


def _persist_entry(project_dir: str | None, entry: dict[str, Any]) -> None:
    """Append one human-readable log block to engagement activity.log. Never raises."""
    paths = _log_paths(project_dir)
    if not paths:
        return
    row = {k: v for k, v in entry.items() if k != "live"}
    text = _format_human_entry(row)
    for path in paths:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(text)
                f.flush()
        except OSError as exc:
            try:
                from logging_util import log_event

                log_event(
                    "ERROR",
                    "activity_log_write_failed",
                    path=str(path),
                    error=str(exc),
                )
            except Exception:  # noqa: BLE001
                pass


def _commit(key: str, entry: dict[str, Any], *, persist: bool = True) -> None:
    """Append to in-memory buffer and optionally to the on-disk log."""
    buf = _buffers.get(key)
    if buf is None:
        buf = deque(maxlen=_MAX)
        _buffers[key] = buf
    buf.append(entry)
    if persist:
        _persist_entry(key if key != "_global" else None, entry)


def clear_activity(project_dir: str | None = None) -> None:
    """Wipe the in-memory UI buffer (e.g. on Open).

    Does **not** delete ``activity.log`` — that file is append-only
    audit storage only and is never loaded back into Activity History.
    """
    key = _key(project_dir or current_project_key.get())
    with _lock:
        _buffers[key] = deque(maxlen=_MAX)
        _thinking.pop(key, None)
        _current_run.pop(key, None)


def begin_run(
    label: str,
    *,
    project_dir: str | None = None,
    detail: str | None = None,
) -> str:
    """Start a new run section without wiping prior run history."""
    key = _key(project_dir or current_project_key.get())
    run_id = uuid.uuid4().hex[:10]
    label = (label or "Run").strip()[:120]
    with _lock:
        _thinking.pop(key, None)
        _current_run[key] = run_id
        meta = {
            "run_id": run_id,
            "label": label,
            "started": datetime.now(timezone.utc).isoformat(),
        }
        _run_meta[run_id] = meta
        entry = {
            **_now_entry_base(),
            "kind": "run",
            "message": label,
            "agent": None,
            "detail": (detail or "")[:_DETAIL_MAX] if detail else None,
            "run_id": run_id,
            "run_label": label,
        }
        _commit(key, entry, persist=True)
    return run_id


def push_activity(
    message: str,
    *,
    project_dir: str | None = None,
    kind: str = "info",
    agent: str | None = None,
    detail: str | None = None,
) -> None:
    """Append a human-readable activity line into the current run (and log)."""
    key = _key(project_dir or current_project_key.get())
    with _lock:
        run_id = _current_run.get(key)
        if not run_id:
            # Lazy-start a run if callers forgot begin_run
            run_id = uuid.uuid4().hex[:10]
            _current_run[key] = run_id
            _run_meta[run_id] = {
                "run_id": run_id,
                "label": "Activity",
                "started": datetime.now(timezone.utc).isoformat(),
            }
            _commit(
                key,
                {
                    **_now_entry_base(),
                    "kind": "run",
                    "message": "Activity",
                    "agent": None,
                    "detail": None,
                    "run_id": run_id,
                    "run_label": "Activity",
                },
                persist=True,
            )

        entry = {
            **_now_entry_base(),
            "kind": kind,
            "message": (message or "").strip()[:500],
            "agent": agent,
            "detail": (detail or "")[:_DETAIL_MAX] if detail else None,
            "run_id": run_id,
            "run_label": (_run_meta.get(run_id) or {}).get("label"),
        }
        _commit(key, entry, persist=True)


def set_thinking(
    text: str,
    *,
    project_dir: str | None = None,
    agent: str | None = None,
    topic: str | None = None,
    phase: str = "thinking",
) -> None:
    """Create/update the live brain-thinking line (model stream output).

    Live token updates stay in memory for the UI; the final snapshot is logged
    in ``clear_thinking`` (also called when ``activity_scope`` exits).
    """
    key = _key(project_dir or current_project_key.get())
    body = (text or "").strip()
    if len(body) > _THINKING_MAX:
        body = "…" + body[-(_THINKING_MAX - 1) :]
    label = "Model thinking" if phase == "thinking" else "Model writing"
    if topic:
        label = f"{label}: {topic[:80]}"
    with _lock:
        prev = _thinking.get(key) or {}
        run_id = _current_run.get(key)
        _thinking[key] = {
            **_now_entry_base(),
            "kind": "thinking",
            "message": label,
            "agent": agent if agent is not None else prev.get("agent"),
            "detail": body or None,
            "live": True,
            "phase": phase,
            "run_id": run_id,
            "run_label": (_run_meta.get(run_id) or {}).get("label") if run_id else None,
        }


def clear_thinking(project_dir: str | None = None) -> None:
    """Remove live thinking; snapshot last text into the run log + file."""
    key = _key(project_dir or current_project_key.get())
    with _lock:
        live = _thinking.pop(key, None)
        if not live:
            return
        detail = live.get("detail")
        if not detail:
            return
        run_id = live.get("run_id") or _current_run.get(key)
        _commit(
            key,
            {
                **_now_entry_base(),
                "kind": "thinking",
                "message": live.get("message") or "Model thinking",
                "agent": live.get("agent"),
                "detail": str(detail)[:_THINKING_MAX],
                "live": False,
                "phase": live.get("phase") or "thinking",
                "run_id": run_id,
                "run_label": live.get("run_label"),
            },
            persist=True,
        )


def get_activity(project_dir: str | None = None, *, since: float | None = None) -> list[dict[str, Any]]:
    key = _key(project_dir)
    with _lock:
        items = list(_buffers.get(key) or [])
        live = _thinking.get(key)
    if live:
        items = items + [live]
    if since is not None:
        items = [e for e in items if float(e.get("t") or 0) > since]
    return items


class activity_scope:
    """Bind activity pushes to a project for the duration of a request."""

    def __init__(self, project_dir: str):
        self.project_dir = _normalize_project_dir(str(project_dir)) or str(project_dir)
        self._token = None

    def __enter__(self) -> "activity_scope":
        self._token = current_project_key.set(self.project_dir)
        return self

    def __exit__(self, *exc: object) -> None:
        # Always snapshot any live thinking the UI showed during this request.
        try:
            clear_thinking(self.project_dir)
        except Exception:  # noqa: BLE001
            pass
        if self._token is not None:
            current_project_key.reset(self._token)
