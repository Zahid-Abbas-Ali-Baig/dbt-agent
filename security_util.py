"""Shared security helpers for path containment and secret handling."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parent

SECRET_CONFIG_KEYS = frozenset(
    {
        "DB_PASSWORD",
        "SOURCE_DB_PASSWORD",
        "TARGET_DB_PASSWORD",
        "API_KEY",
        "CURSOR_API_KEY",
        "PASSWORD",
    }
)

_WRITE_SQL_RE = re.compile(
    r"\b("
    r"insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|"
    r"call|execute|do|vacuum|reindex|cluster|comment|security|listen|notify|"
    r"refresh|merge|replace|attach|detach|load|unload"
    r")\b",
    re.IGNORECASE,
)


def engagements_root() -> Path:
    """Allowed parent for engagement project folders."""
    raw = (os.getenv("ENGAGEMENTS_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    # Default: sibling engagements under the same parent as this agent repo
    return _AGENT_ROOT.parent.resolve()


def is_under(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def assert_under_project(project_dir: Path, target: Path, *, label: str = "path") -> None:
    if not is_under(project_dir, target):
        raise ValueError(f"{label} escapes project dir: {target}")


def validate_engagement_dir(project_dir: Path) -> Path:
    """Resolve and ensure engagement path stays under ENGAGEMENTS_ROOT."""
    p = project_dir.expanduser().resolve()
    root = engagements_root()
    if not is_under(root, p) and p != root:
        raise ValueError(
            f"Project folder must be under {root}. "
            f"Got: {p}. Set ENGAGEMENTS_ROOT in .env to change the allowlist."
        )
    return p


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return "********"


def redact_config_dict(cfg: dict[str, str]) -> dict[str, str]:
    out = dict(cfg)
    for key in list(out):
        if key.upper() in SECRET_CONFIG_KEYS or key.upper().endswith("_PASSWORD"):
            if out[key] and "{{" not in str(out[key]):
                out[key] = mask_secret(str(out[key]))
    return out


def redact_config_text(text: str) -> str:
    """Mask password-like KEY: value lines in kit markdown for LLM context."""

    def _repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key.upper() in SECRET_CONFIG_KEYS or key.upper().endswith("_PASSWORD"):
            return f"{m.group(0).split(':', 1)[0]}: ********"
        return m.group(0)

    return re.sub(
        r"(?m)^(\s*[A-Za-z0-9_]*PASSWORD[A-Za-z0-9_]*)\s*:\s*.+$",
        _repl,
        text,
        flags=re.IGNORECASE,
    )


def redact_chat_message(message: str, *, intake_field: str | None = None) -> str:
    """Avoid storing plaintext passwords in browser session history."""
    if intake_field and "PASSWORD" in intake_field.upper():
        return "********"
    # KEY: secret patterns
    redacted = re.sub(
        r"(?i)\b([A-Za-z0-9_]*PASSWORD[A-Za-z0-9_]*)\s*[:=]\s*\S+",
        r"\1: ********",
        message,
    )
    return redacted


def secret_fingerprint(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]


def assert_readonly_sql(sql: str) -> None:
    """Reject mutating / multi-statement SQL for warehouse_query."""
    text = (sql or "").strip()
    if not text:
        raise ValueError("Empty SQL")
    low = text.lower()
    if not (low.startswith("select") or low.startswith("with") or low.startswith("explain")):
        raise ValueError("Only SELECT / WITH / EXPLAIN queries are allowed")
    # Reject additional statements
    body = text.rstrip().rstrip(";")
    if ";" in body:
        raise ValueError("Multiple SQL statements are not allowed")
    if _WRITE_SQL_RE.search(text):
        raise ValueError("Write/DDL keywords are not allowed in warehouse queries")


def yaml_quote(value: str) -> str:
    """Safe double-quoted YAML scalar."""
    escaped = (
        (value or "")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'
