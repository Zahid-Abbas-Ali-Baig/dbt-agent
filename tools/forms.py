"""Configurable UI form schemas for brain + warehouse connection.

Warehouse types and brain providers are declared here so new backends can be
added without changing the one-question-at-a-time chat flow.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from tools.files import (
    SUPPORTED_WAREHOUSE_TYPES,
    apply_intake_defaults,
    missing_intake_keys,
    parse_config,
    set_config_value,
)
from tools.registry import ToolResult

# --- Warehouse connection forms (keyed by warehouse type) -------------------

_POSTGRES_SOURCE_FIELDS = [
    {"key": "SOURCE_DATABASE_NAME", "label": "Database name", "type": "text", "required": True},
    {"key": "SOURCE_SCHEMA_NAME", "label": "Schema (landed tables)", "type": "text", "required": True},
    {"key": "SOURCE_DB_HOST", "label": "Host", "type": "text", "required": True, "default": "localhost"},
    {"key": "SOURCE_DB_PORT", "label": "Port", "type": "text", "required": True, "default": "5432"},
    {"key": "SOURCE_DB_USER", "label": "Username", "type": "text", "required": True},
    {"key": "SOURCE_DB_PASSWORD", "label": "Password", "type": "password", "required": True},
]

_POSTGRES_TARGET_FIELDS = [
    {"key": "TARGET_DATABASE_NAME", "label": "Database name", "type": "text", "required": True},
    {"key": "TARGET_DB_HOST", "label": "Host", "type": "text", "required": True, "default": "localhost"},
    {"key": "TARGET_DB_PORT", "label": "Port", "type": "text", "required": True, "default": "5432"},
    {"key": "TARGET_DB_USER", "label": "Username", "type": "text", "required": True},
    {"key": "TARGET_DB_PASSWORD", "label": "Password", "type": "password", "required": True},
]

# Expand later: snowflake, redshift, bigquery, databricks, duckdb
WAREHOUSE_TYPE_FORMS: dict[str, dict[str, Any]] = {
    "postgres": {
        "label": "Postgres",
        "enabled": True,
        "source_fields": _POSTGRES_SOURCE_FIELDS,
        "target_fields": _POSTGRES_TARGET_FIELDS,
    },
    # Placeholders for future types (shown disabled in UI)
    "snowflake": {"label": "Snowflake", "enabled": False, "source_fields": [], "target_fields": []},
    "redshift": {"label": "Redshift", "enabled": False, "source_fields": [], "target_fields": []},
    "bigquery": {"label": "BigQuery", "enabled": False, "source_fields": [], "target_fields": []},
}


def connection_form_schema(*, values: dict[str, str] | None = None) -> dict[str, Any]:
    """Schema for the engagement connection menu."""
    vals = dict(values or {})
    types = []
    for tid in list(SUPPORTED_WAREHOUSE_TYPES) + [
        t for t in WAREHOUSE_TYPE_FORMS if t not in SUPPORTED_WAREHOUSE_TYPES
    ]:
        meta = WAREHOUSE_TYPE_FORMS.get(tid, {"label": tid, "enabled": False})
        types.append(
            {
                "id": tid,
                "label": meta.get("label", tid),
                "enabled": bool(meta.get("enabled")) and tid in SUPPORTED_WAREHOUSE_TYPES,
            }
        )
    source_type = (vals.get("SOURCE_WAREHOUSE_TYPE") or "").lower()
    if source_type not in WAREHOUSE_TYPE_FORMS or not WAREHOUSE_TYPE_FORMS[source_type].get("enabled"):
        # Prefer first enabled type for field layout only (type was chosen in prior step)
        source_type = next(
            (t for t in SUPPORTED_WAREHOUSE_TYPES if WAREHOUSE_TYPE_FORMS.get(t, {}).get("enabled")),
            "postgres",
        )
    target_type = (vals.get("TARGET_WAREHOUSE_TYPE") or source_type).lower()
    if target_type not in WAREHOUSE_TYPE_FORMS or not WAREHOUSE_TYPE_FORMS[target_type].get("enabled"):
        target_type = source_type

    src_meta = WAREHOUSE_TYPE_FORMS[source_type]
    tgt_meta = WAREHOUSE_TYPE_FORMS[target_type]
    return {
        "warehouse_types": types,
        "common_fields": [
            {"key": "PROJECT_NAME", "label": "Project name", "type": "text", "required": True},
            {"key": "SOURCE_NAME", "label": "Source label (dbt)", "type": "text", "required": True, "default": "crm"},
        ],
        "source_type": source_type,
        "target_type": target_type,
        "source_fields": src_meta.get("source_fields") or [],
        "target_fields": tgt_meta.get("target_fields") or [],
        "target_same_as_source": (vals.get("TARGET_SAME_AS_SOURCE") or "true").lower()
        in {"true", "yes", "y", "1"},
        "values": {
            k: v
            for k, v in vals.items()
            if v and "{{" not in str(v) and not (k.upper().endswith("PASSWORD") and v)
        },
        # Passwords never prefilled into the browser
    }


def connection_form_values(project_dir: Path) -> dict[str, str]:
    cfg = parse_config(project_dir).data or {}
    return {k: str(v) for k, v in cfg.items() if v is not None}


def apply_connection_form(project_dir: Path, payload: dict[str, Any]) -> ToolResult:
    """Write connection menu fields into config.md."""
    values = dict(payload.get("values") or payload or {})
    source_type = str(values.get("SOURCE_WAREHOUSE_TYPE") or "postgres").strip().lower()
    target_same = str(values.get("TARGET_SAME_AS_SOURCE") or "true").strip().lower()
    if target_same in {"true", "yes", "y", "1", "same"}:
        target_same = "true"
    else:
        target_same = "false"
    target_type = str(
        values.get("TARGET_WAREHOUSE_TYPE") or (source_type if target_same == "true" else source_type)
    ).strip().lower()

    if source_type not in SUPPORTED_WAREHOUSE_TYPES:
        return ToolResult(
            ok=False,
            output=f"Warehouse type `{source_type}` is not enabled yet. Supported: {', '.join(SUPPORTED_WAREHOUSE_TYPES)}",
        )
    if target_type not in SUPPORTED_WAREHOUSE_TYPES:
        return ToolResult(
            ok=False,
            output=f"Target warehouse type `{target_type}` is not enabled yet.",
        )

    values["SOURCE_WAREHOUSE_TYPE"] = source_type
    values["TARGET_WAREHOUSE_TYPE"] = target_type
    values["TARGET_SAME_AS_SOURCE"] = target_same
    values["WAREHOUSE_TYPE"] = source_type

    required = ["PROJECT_NAME", "SOURCE_NAME"]
    schema = WAREHOUSE_TYPE_FORMS[source_type]
    for f in schema.get("source_fields") or []:
        if f.get("required"):
            required.append(f["key"])
    if target_same == "false":
        for f in (WAREHOUSE_TYPE_FORMS[target_type].get("target_fields") or []):
            if f.get("required"):
                required.append(f["key"])

    missing = [k for k in required if not str(values.get(k) or "").strip()]
    if missing:
        return ToolResult(ok=False, output=f"Missing required fields: {', '.join(missing)}")

    written: list[str] = []
    for key, val in values.items():
        if not str(val).strip():
            continue
        res = set_config_value(project_dir, key, str(val).strip())
        if not res.ok:
            return res
        written.append(key)

    apply_intake_defaults(project_dir)
    still = missing_intake_keys(project_dir)
    if still:
        return ToolResult(
            ok=False,
            output=f"Still missing after save: {', '.join(still)}",
            data={"written": written, "missing": still},
        )
    return ToolResult(
        ok=True,
        output=f"Saved connection settings ({len(written)} fields).",
        data={"written": written},
    )


# --- Brain / LLM provider forms --------------------------------------------

BRAIN_PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "cursor",
        "label": "Cursor",
        "enabled": True,
        "fields": [
            {"key": "MODEL", "label": "Model", "type": "text", "required": True, "default": "composer-2.5"},
            {"key": "CURSOR_API_KEY", "label": "Cursor API key", "type": "password", "required": True},
        ],
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "enabled": False,
        "fields": [
            {"key": "MODEL", "label": "Model", "type": "text", "required": True, "default": "gpt-4.1"},
            {"key": "API_KEY", "label": "API key", "type": "password", "required": True},
            {"key": "BASE_URL", "label": "Base URL (optional)", "type": "text", "required": False},
        ],
    },
    {
        "id": "ollama",
        "label": "Ollama",
        "enabled": False,
        "fields": [
            {"key": "MODEL", "label": "Model", "type": "text", "required": True, "default": "llama3.2"},
            {"key": "BASE_URL", "label": "Base URL", "type": "text", "required": False, "default": "http://localhost:11434/v1"},
        ],
    },
]


def warehouse_type_form_schema(*, values: dict[str, str] | None = None) -> dict[str, Any]:
    """Dropdown-only step: pick SOURCE (and optional TARGET) warehouse type."""
    vals = dict(values or {})
    types = []
    for tid, meta in WAREHOUSE_TYPE_FORMS.items():
        enabled = bool(meta.get("enabled")) and tid in SUPPORTED_WAREHOUSE_TYPES
        types.append({"id": tid, "label": meta.get("label", tid), "enabled": enabled})
    # Ensure enabled types listed first
    types.sort(key=lambda t: (not t["enabled"], t["label"]))
    source = (vals.get("SOURCE_WAREHOUSE_TYPE") or "").lower()
    if source and "{{" in source:
        source = ""
    target_same = (vals.get("TARGET_SAME_AS_SOURCE") or "true").lower() in {"true", "yes", "y", "1"}
    target = (vals.get("TARGET_WAREHOUSE_TYPE") or source or "").lower()
    return {
        "warehouse_types": types,
        "source_type": source if source in WAREHOUSE_TYPE_FORMS else "",
        "target_type": target if target in WAREHOUSE_TYPE_FORMS else "",
        "target_same_as_source": target_same,
    }


def apply_warehouse_type_form(project_dir: Path, payload: dict[str, Any]) -> ToolResult:
    source_type = str(payload.get("source_type") or payload.get("SOURCE_WAREHOUSE_TYPE") or "").strip().lower()
    if source_type not in SUPPORTED_WAREHOUSE_TYPES:
        enabled = ", ".join(SUPPORTED_WAREHOUSE_TYPES) or "(none)"
        return ToolResult(
            ok=False,
            output=f"Pick an enabled warehouse type. Available now: {enabled}.",
        )
    target_same_raw = str(payload.get("TARGET_SAME_AS_SOURCE") or payload.get("target_same_as_source") or "true")
    target_same = "true" if target_same_raw.strip().lower() in {"true", "yes", "y", "1", "same"} else "false"
    target_type = str(
        payload.get("target_type") or payload.get("TARGET_WAREHOUSE_TYPE") or source_type
    ).strip().lower()
    if target_same == "true":
        target_type = source_type
    elif target_type not in SUPPORTED_WAREHOUSE_TYPES:
        return ToolResult(ok=False, output=f"Target type `{target_type}` is not enabled yet.")

    for key, val in (
        ("SOURCE_WAREHOUSE_TYPE", source_type),
        ("TARGET_WAREHOUSE_TYPE", target_type),
        ("TARGET_SAME_AS_SOURCE", target_same),
        ("WAREHOUSE_TYPE", source_type),
    ):
        res = set_config_value(project_dir, key, val)
        if not res.ok:
            return res
    return ToolResult(
        ok=True,
        output=f"Source warehouse set to {source_type}"
        + ("" if target_same == "true" else f"; target set to {target_type}"),
        data={"source_type": source_type, "target_type": target_type, "target_same": target_same},
    )


def brain_form_schema() -> dict[str, Any]:
    provider = (os.getenv("LLM_PROVIDER") or "cursor").strip().lower()
    enabled_ids = {p["id"] for p in BRAIN_PROVIDERS if p.get("enabled")}
    if provider not in enabled_ids:
        provider = "cursor"
    values: dict[str, str] = {
        "LLM_PROVIDER": provider,
        "MODEL": (os.getenv("MODEL") or "").strip(),
    }
    # Never send existing secrets to the browser — only whether set
    secrets_set = {
        "CURSOR_API_KEY": bool((os.getenv("CURSOR_API_KEY") or os.getenv("API_KEY") or "").strip()),
        "API_KEY": bool((os.getenv("API_KEY") or "").strip()),
    }
    return {
        "providers": [
            {
                "id": p["id"],
                "label": p["label"],
                "enabled": bool(p.get("enabled")),
                "fields": p.get("fields") or [],
            }
            for p in BRAIN_PROVIDERS
        ],
        "provider": provider,
        "values": {k: v for k, v in values.items() if v},
        "secrets_set": secrets_set,
    }


def _upsert_env_file(env_path: Path, updates: dict[str, str]) -> None:
    """Update or append KEY=value lines in .env without wiping unrelated keys."""
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = text.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def apply_brain_form(agent_root: Path, payload: dict[str, Any]) -> ToolResult:
    """Persist brain settings into agent .env and refresh process env."""
    provider = str(payload.get("provider") or payload.get("LLM_PROVIDER") or "cursor").strip().lower()
    meta = next((p for p in BRAIN_PROVIDERS if p["id"] == provider), None)
    if not meta or not meta.get("enabled"):
        return ToolResult(
            ok=False,
            output=f"Brain provider `{provider}` is not enabled yet. Use Cursor for now.",
        )
    values = dict(payload.get("values") or {})
    updates: dict[str, str] = {"LLM_PROVIDER": provider}
    for field in meta.get("fields") or []:
        key = field["key"]
        raw = values.get(key)
        if raw is None or str(raw).strip() == "":
            if field.get("type") == "password" and field.get("required"):
                # Keep existing secret if already set
                existing = (os.getenv(key) or "").strip()
                if key == "CURSOR_API_KEY" and not existing:
                    existing = (os.getenv("API_KEY") or "").strip()
                if existing:
                    continue
                if field.get("required"):
                    return ToolResult(ok=False, output=f"Missing required field: {field.get('label') or key}")
            elif field.get("required"):
                default = field.get("default")
                if default:
                    updates[key] = str(default)
                else:
                    return ToolResult(ok=False, output=f"Missing required field: {field.get('label') or key}")
            continue
        updates[key] = str(raw).strip()

    env_path = agent_root / ".env"
    _upsert_env_file(env_path, updates)
    for k, v in updates.items():
        os.environ[k] = v
    return ToolResult(
        ok=True,
        output=f"Brain set to {provider}" + (f" / {updates.get('MODEL')}" if updates.get("MODEL") else ""),
        data={"provider": provider, "model": updates.get("MODEL")},
    )


# --- Path confidence threshold (Phase 1 clarify gate) --------------------------

DEFAULT_PATH_CONFIDENCE_THRESHOLD = 70

CONFIDENCE_PRESETS: list[dict[str, Any]] = [
    {"id": "50", "label": "50% — ask often"},
    {"id": "60", "label": "60%"},
    {"id": "70", "label": "70% — recommended"},
    {"id": "80", "label": "80%"},
    {"id": "90", "label": "90% — ask rarely"},
]


def confidence_form_schema(*, current: int | None = None) -> dict[str, Any]:
    value = current if current is not None else DEFAULT_PATH_CONFIDENCE_THRESHOLD
    try:
        value = max(1, min(100, int(value)))
    except (TypeError, ValueError):
        value = DEFAULT_PATH_CONFIDENCE_THRESHOLD
    preset_ids = {p["id"] for p in CONFIDENCE_PRESETS}
    return {
        "presets": list(CONFIDENCE_PRESETS),
        "threshold": value,
        "preset": str(value) if str(value) in preset_ids else "custom",
        "custom_allowed": True,
        "default": DEFAULT_PATH_CONFIDENCE_THRESHOLD,
        "help": (
            "During discovery I map each business need to tables and score confidence. "
            "Below this %, I pause and ask you one question at a time before drafting the brief."
        ),
    }


def apply_confidence_form(project_dir: Path, payload: dict[str, Any]) -> ToolResult:
    raw = payload.get("threshold", payload.get("PATH_CONFIDENCE_THRESHOLD", payload.get("value")))
    if raw is None or str(raw).strip() == "":
        return ToolResult(ok=False, output="Pick a confidence threshold (1–100).")
    try:
        threshold = int(str(raw).strip().rstrip("%"))
    except (TypeError, ValueError):
        return ToolResult(ok=False, output="Confidence must be a whole number from 1 to 100.")
    if threshold < 1 or threshold > 100:
        return ToolResult(ok=False, output="Confidence must be between 1 and 100.")
    res = set_config_value(project_dir, "PATH_CONFIDENCE_THRESHOLD", str(threshold))
    if not res.ok:
        return res
    return ToolResult(
        ok=True,
        output=f"Path confidence threshold set to {threshold}%.",
        data={"threshold": threshold},
    )
