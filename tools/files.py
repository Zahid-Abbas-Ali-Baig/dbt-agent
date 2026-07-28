"""Engagement file I/O scoped to DBT_PROJECT_DIR."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from tools.registry import ToolResult

_AGENT_ROOT = Path(__file__).resolve().parent.parent
KIT_DIR = _AGENT_ROOT / "kit"

CONFIG_KEYS = [
    "PROJECT_NAME",
    # Legacy aliases (mirrored from SOURCE_*)
    "WAREHOUSE_TYPE",
    "DATABASE_NAME",
    "SCHEMA_NAME",
    "DB_HOST",
    "DB_PORT",
    "DB_USER",
    "DB_PASSWORD",
    "DB_THREADS",
    # Source (landed) warehouse — dynamic type for future releases
    "SOURCE_WAREHOUSE_TYPE",
    "SOURCE_DATABASE_NAME",
    "SOURCE_SCHEMA_NAME",
    "SOURCE_DB_HOST",
    "SOURCE_DB_PORT",
    "SOURCE_DB_USER",
    "SOURCE_DB_PASSWORD",
    "SOURCE_NAME",
    # Target (dbt write) warehouse — dynamic type; v1 = postgres only
    "TARGET_WAREHOUSE_TYPE",
    "TARGET_SAME_AS_SOURCE",
    "TARGET_DATABASE_NAME",
    "TARGET_DB_HOST",
    "TARGET_DB_PORT",
    "TARGET_DB_USER",
    "TARGET_DB_PASSWORD",
    "TARGET_DB_THREADS",
    "STAGING_SCHEMA",
    "INTERMEDIATE_SCHEMA",
    "MARTS_SCHEMA",
    "ENABLE_SEMANTIC_LAYER",
    "BI_TOOL",
    "BI_PBIP_DIR",
    "REQUIREMENTS_DOC",
    "DESIGN_BRIEF_DOC",
    "KPI_BREAKDOWN_MIN_POPULATED_PCT",
    "KPI_BREAKDOWN_MIN_DISTINCT",
    "PATH_CONFIDENCE_THRESHOLD",
]

# v1 supported types — expand later without changing intake shape
SUPPORTED_WAREHOUSE_TYPES = ("postgres",)
# Future: "snowflake", "redshift", "bigquery", "databricks", "duckdb"

# Asked one-by-one in chat intake (rest get safe defaults).
INTAKE_CONFIG_KEYS = [
    "PROJECT_NAME",
    "SOURCE_WAREHOUSE_TYPE",
    "SOURCE_DATABASE_NAME",
    "SOURCE_SCHEMA_NAME",
    "SOURCE_DB_HOST",
    "SOURCE_DB_PORT",
    "SOURCE_DB_USER",
    "SOURCE_DB_PASSWORD",
    "SOURCE_NAME",
    "TARGET_WAREHOUSE_TYPE",
    "TARGET_SAME_AS_SOURCE",
    # TARGET_* connection fields only required when TARGET_SAME_AS_SOURCE is false
    "TARGET_DATABASE_NAME",
    "TARGET_DB_HOST",
    "TARGET_DB_PORT",
    "TARGET_DB_USER",
    "TARGET_DB_PASSWORD",
]

INTAKE_DEFAULTS = {
    "TARGET_SAME_AS_SOURCE": "true",
    # Warehouse types are chosen via the warehouse-type menu — do not auto-fill
    "DB_THREADS": "4",
    "TARGET_DB_THREADS": "4",
    "SOURCE_DB_PORT": "5432",
    "TARGET_DB_PORT": "5432",
    "STAGING_SCHEMA": "staging",
    "INTERMEDIATE_SCHEMA": "intermediate",
    "MARTS_SCHEMA": "marts",
    "ENABLE_SEMANTIC_LAYER": "true",
    "BI_TOOL": "powerbi",
    "BI_PBIP_DIR": "powerbi-project",
    "KPI_BREAKDOWN_MIN_POPULATED_PCT": "5",
    "KPI_BREAKDOWN_MIN_DISTINCT": "2",
}

INTAKE_PROMPTS = {
    "PROJECT_NAME": "What should we call this project? (short name, no spaces, e.g. shopsphere)",
    "SOURCE_WAREHOUSE_TYPE": (
        "What is the SOURCE warehouse type? (v1: postgres only — type postgres)"
    ),
    "SOURCE_DATABASE_NAME": "What is the SOURCE database name? (where landed tables live)",
    "SOURCE_SCHEMA_NAME": "Which SOURCE schema already has the landed tables?",
    "SOURCE_DB_HOST": "What is the SOURCE database host? (e.g. localhost)",
    "SOURCE_DB_PORT": "What is the SOURCE database port? (usually 5432 for postgres)",
    "SOURCE_DB_USER": "What is the SOURCE database username?",
    "SOURCE_DB_PASSWORD": "What is the SOURCE database password?",
    "SOURCE_NAME": "What short label should we use for this source in dbt? (e.g. crm)",
    "TARGET_WAREHOUSE_TYPE": (
        "What is the TARGET warehouse type for dbt models? (v1: postgres only — type postgres)"
    ),
    "TARGET_SAME_AS_SOURCE": (
        "Should the TARGET warehouse use the same connection as SOURCE? Reply yes or no."
    ),
    "TARGET_DATABASE_NAME": "What is the TARGET database name?",
    "TARGET_DB_HOST": "What is the TARGET database host?",
    "TARGET_DB_PORT": "What is the TARGET database port?",
    "TARGET_DB_USER": "What is the TARGET database username?",
    "TARGET_DB_PASSWORD": "What is the TARGET database password?",
    # Legacy prompts kept for free-form KEY answers
    "DATABASE_NAME": "What is the SOURCE database name?",
    "SCHEMA_NAME": "Which SOURCE schema has the landed tables?",
    "DB_HOST": "What is the SOURCE database host?",
    "DB_PORT": "What is the SOURCE database port?",
    "DB_USER": "What is the SOURCE database username?",
    "DB_PASSWORD": "What is the SOURCE database password?",
    "REQUIREMENTS": (
        "Paste the business requirements — domain, goals, pain points, "
        "and the questions the report must answer. Skip SQL and table lists."
    ),
}


def _safe_join(project_dir: Path, rel: str) -> Path:
    from security_util import assert_under_project

    rel_path = Path(rel)
    if rel_path.is_absolute():
        raise ValueError("Absolute paths not allowed")
    # Disallow parent escapes in the relative path itself
    if ".." in rel_path.parts:
        raise ValueError(f"Path escapes project dir: {rel}")
    target = (project_dir / rel_path).resolve()
    assert_under_project(project_dir, target, label="path")
    return target


def init_engagement(project_dir: Path, *, force: bool = False) -> ToolResult:
    project_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in ("config.md", "requirements.md", "design_brief.md", "client_feedback.md"):
        src = KIT_DIR / name
        dest = project_dir / name
        if dest.exists() and not force:
            continue
        shutil.copy2(src, dest)
        copied.append(name)
    # Empty model folders
    for sub in (
        "models/staging",
        "models/intermediate",
        "models/marts",
        "models/semantic",
    ):
        (project_dir / sub).mkdir(parents=True, exist_ok=True)
    return ToolResult(
        ok=True,
        output=f"Initialized engagement at {project_dir}. Copied: {copied or '(already present)'}",
        data={"copied": copied, "project_dir": str(project_dir)},
    )


def _is_placeholder(val: str) -> bool:
    v = (val or "").strip()
    return not v or "{{" in v or v.startswith("<") and v.endswith(">")


def _first(*vals: str) -> str:
    for v in vals:
        if v and not _is_placeholder(v):
            return v
    return ""


def normalize_config(cfg: dict[str, str]) -> dict[str, str]:
    """Fill SOURCE_/TARGET_/legacy aliases so all call sites share one shape."""
    out = dict(cfg)

    # Source prefers SOURCE_*, falls back to legacy DB_*
    # Do not invent a warehouse type — empty until the type menu (or config) sets it
    out["SOURCE_WAREHOUSE_TYPE"] = _first(
        out.get("SOURCE_WAREHOUSE_TYPE", ""), out.get("WAREHOUSE_TYPE", "")
    ).lower()
    out["SOURCE_DATABASE_NAME"] = _first(
        out.get("SOURCE_DATABASE_NAME", ""), out.get("DATABASE_NAME", "")
    )
    out["SOURCE_SCHEMA_NAME"] = _first(
        out.get("SOURCE_SCHEMA_NAME", ""), out.get("SCHEMA_NAME", "")
    )
    out["SOURCE_DB_HOST"] = _first(out.get("SOURCE_DB_HOST", ""), out.get("DB_HOST", ""))
    out["SOURCE_DB_PORT"] = _first(out.get("SOURCE_DB_PORT", ""), out.get("DB_PORT", ""), "5432")
    out["SOURCE_DB_USER"] = _first(out.get("SOURCE_DB_USER", ""), out.get("DB_USER", ""))
    out["SOURCE_DB_PASSWORD"] = _first(
        out.get("SOURCE_DB_PASSWORD", ""), out.get("DB_PASSWORD", "")
    )

    # Mirror legacy from source (backward compatible for phase0/profiles)
    out["WAREHOUSE_TYPE"] = out["SOURCE_WAREHOUSE_TYPE"]
    out["DATABASE_NAME"] = out["SOURCE_DATABASE_NAME"]
    out["SCHEMA_NAME"] = out["SOURCE_SCHEMA_NAME"]
    out["DB_HOST"] = out["SOURCE_DB_HOST"]
    out["DB_PORT"] = out["SOURCE_DB_PORT"]
    out["DB_USER"] = out["SOURCE_DB_USER"]
    out["DB_PASSWORD"] = out["SOURCE_DB_PASSWORD"]

    same_raw = (out.get("TARGET_SAME_AS_SOURCE") or "true").strip().lower()
    same = same_raw in {"true", "yes", "y", "1"}
    out["TARGET_SAME_AS_SOURCE"] = "true" if same else "false"
    out["TARGET_WAREHOUSE_TYPE"] = _first(
        out.get("TARGET_WAREHOUSE_TYPE", ""), out["SOURCE_WAREHOUSE_TYPE"]
    ).lower()

    if same:
        out["TARGET_DATABASE_NAME"] = out["SOURCE_DATABASE_NAME"]
        out["TARGET_DB_HOST"] = out["SOURCE_DB_HOST"]
        out["TARGET_DB_PORT"] = out["SOURCE_DB_PORT"]
        out["TARGET_DB_USER"] = out["SOURCE_DB_USER"]
        out["TARGET_DB_PASSWORD"] = out["SOURCE_DB_PASSWORD"]
    else:
        out["TARGET_DATABASE_NAME"] = _first(out.get("TARGET_DATABASE_NAME", ""))
        out["TARGET_DB_HOST"] = _first(out.get("TARGET_DB_HOST", ""))
        out["TARGET_DB_PORT"] = _first(out.get("TARGET_DB_PORT", ""), "5432")
        out["TARGET_DB_USER"] = _first(out.get("TARGET_DB_USER", ""))
        out["TARGET_DB_PASSWORD"] = _first(out.get("TARGET_DB_PASSWORD", ""))

    out.setdefault("TARGET_DB_THREADS", out.get("DB_THREADS", "4"))
    return out


def _strip_inline_comment(key: str, val: str) -> str:
    """Strip trailing markdown comments without mangling passwords that contain '#'."""
    if key.upper().endswith("PASSWORD") or "PASSWORD" in key.upper():
        return val
    if " #" in val:
        return val.split(" #", 1)[0].strip()
    return val


def parse_config(project_dir: Path) -> ToolResult:
    path = project_dir / "config.md"
    if not path.exists():
        return ToolResult(ok=False, output="config.md not found — run init first")
    text = path.read_text(encoding="utf-8")
    cfg: dict[str, str] = {}
    for key in CONFIG_KEYS:
        m = re.search(rf"^\s*{re.escape(key)}\s*:\s*(.+)$", text, re.MULTILINE)
        if m:
            val = _strip_inline_comment(key, m.group(1).strip())
            cfg[key] = val
    defaults = {
        "REQUIREMENTS_DOC": "requirements.md",
        "DESIGN_BRIEF_DOC": "design_brief.md",
        "STAGING_SCHEMA": "staging",
        "INTERMEDIATE_SCHEMA": "intermediate",
        "MARTS_SCHEMA": "marts",
        "BI_PBIP_DIR": "powerbi-project",
        "ENABLE_SEMANTIC_LAYER": "true",
        "TARGET_SAME_AS_SOURCE": "true",
        "DB_PORT": "5432",
        "SOURCE_DB_PORT": "5432",
        "TARGET_DB_PORT": "5432",
        "DB_THREADS": "4",
        "TARGET_DB_THREADS": "4",
        "PATH_CONFIDENCE_THRESHOLD": "70",
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    cfg = normalize_config(cfg)
    return ToolResult(ok=True, output="Parsed config.md", data=cfg)


def missing_intake_keys(project_dir: Path) -> list[str]:
    """Return intake keys still missing, respecting TARGET_SAME_AS_SOURCE."""
    # Read raw (without requiring target when same-as-source)
    path = project_dir / "config.md"
    if not path.exists():
        return list(INTAKE_CONFIG_KEYS)
    text = path.read_text(encoding="utf-8")
    raw: dict[str, str] = {}
    for key in CONFIG_KEYS:
        m = re.search(rf"^\s*{re.escape(key)}\s*:\s*(.+)$", text, re.MULTILINE)
        if m:
                val = m.group(1).strip()
                if "#" in val and "PASSWORD" not in key.upper():
                    if " #" in val:
                        val = val.split(" #", 1)[0].strip()
                raw[key] = val
    # Legacy → source for older engagements
    for src, legacy in (
        ("SOURCE_DATABASE_NAME", "DATABASE_NAME"),
        ("SOURCE_SCHEMA_NAME", "SCHEMA_NAME"),
        ("SOURCE_DB_HOST", "DB_HOST"),
        ("SOURCE_DB_PORT", "DB_PORT"),
        ("SOURCE_DB_USER", "DB_USER"),
        ("SOURCE_DB_PASSWORD", "DB_PASSWORD"),
        ("SOURCE_WAREHOUSE_TYPE", "WAREHOUSE_TYPE"),
    ):
        if _is_placeholder(raw.get(src, "")) and not _is_placeholder(raw.get(legacy, "")):
            raw[src] = raw[legacy]

    missing: list[str] = []
    always = [
        "PROJECT_NAME",
        "SOURCE_WAREHOUSE_TYPE",
        "SOURCE_DATABASE_NAME",
        "SOURCE_SCHEMA_NAME",
        "SOURCE_DB_HOST",
        "SOURCE_DB_PORT",
        "SOURCE_DB_USER",
        "SOURCE_DB_PASSWORD",
        "SOURCE_NAME",
        "TARGET_WAREHOUSE_TYPE",
        "TARGET_SAME_AS_SOURCE",
    ]
    for key in always:
        if _is_placeholder(raw.get(key, "")):
            missing.append(key)

    same_raw = (raw.get("TARGET_SAME_AS_SOURCE") or "").strip().lower()
    if same_raw and same_raw not in {"true", "yes", "y", "1"} and same_raw not in {
        "false",
        "no",
        "n",
        "0",
    }:
        if "TARGET_SAME_AS_SOURCE" not in missing:
            missing.append("TARGET_SAME_AS_SOURCE")

    same = same_raw in {"true", "yes", "y", "1"}
    if (not _is_placeholder(raw.get("TARGET_SAME_AS_SOURCE", ""))) and not same:
        for key in (
            "TARGET_DATABASE_NAME",
            "TARGET_DB_HOST",
            "TARGET_DB_PORT",
            "TARGET_DB_USER",
            "TARGET_DB_PASSWORD",
        ):
            if _is_placeholder(raw.get(key, "")):
                missing.append(key)
    return missing


YES_LIKE = {"yes", "y", "true", "1", "same", "same as source"}
NO_LIKE = {"no", "n", "false", "0", "different", "separate"}


def set_config_value(project_dir: Path, key: str, value: str) -> ToolResult:
    path = project_dir / "config.md"
    if not path.exists():
        return ToolResult(ok=False, output="config.md not found — run init first")
    if key not in CONFIG_KEYS:
        return ToolResult(ok=False, output=f"Unknown config key: {key}")
    # Never allow MCP / process commands via engagement config
    if key.upper().startswith("MCP_") or key.upper().endswith("_CMD"):
        return ToolResult(ok=False, output="MCP commands must be set in agent .env only")
    text = path.read_text(encoding="utf-8")
    value = value.strip()
    # Normalize yes/no for TARGET_SAME_AS_SOURCE
    if key == "TARGET_SAME_AS_SOURCE":
        low = value.lower()
        if low in YES_LIKE:
            value = "true"
        elif low in NO_LIKE:
            value = "false"
    if key in ("SOURCE_WAREHOUSE_TYPE", "TARGET_WAREHOUSE_TYPE", "WAREHOUSE_TYPE"):
        value = value.strip().lower()
        if value not in SUPPORTED_WAREHOUSE_TYPES:
            return ToolResult(
                ok=False,
                output=(
                    f"Warehouse type `{value}` is not enabled yet. "
                    f"Supported now: {', '.join(SUPPORTED_WAREHOUSE_TYPES)}"
                ),
            )
    if key == "BI_PBIP_DIR":
        rel = Path(value)
        if rel.is_absolute() or ".." in rel.parts:
            return ToolResult(
                ok=False,
                output="BI_PBIP_DIR must be a relative path under the engagement folder",
            )

    pattern = rf"^(\s*{re.escape(key)}\s*:\s*).*$"
    new_text, n = re.subn(
        pattern, lambda m, v=value: m.group(1) + v, text, count=1, flags=re.MULTILINE
    )
    if n == 0:
        # Append into the variables code fence if possible
        fence = re.search(r"```(.*?)```", text, flags=re.DOTALL)
        if fence:
            block = fence.group(1).rstrip() + f"\n{key}: {value}\n"
            new_text = text[: fence.start()] + "```" + block + "```" + text[fence.end() :]
        else:
            new_text = text.rstrip() + f"\n{key}: {value}\n"
    path.write_text(new_text, encoding="utf-8")

    # Keep legacy aliases in sync when source fields change
    alias_map = {
        "SOURCE_WAREHOUSE_TYPE": "WAREHOUSE_TYPE",
        "SOURCE_DATABASE_NAME": "DATABASE_NAME",
        "SOURCE_SCHEMA_NAME": "SCHEMA_NAME",
        "SOURCE_DB_HOST": "DB_HOST",
        "SOURCE_DB_PORT": "DB_PORT",
        "SOURCE_DB_USER": "DB_USER",
        "SOURCE_DB_PASSWORD": "DB_PASSWORD",
    }
    if key in alias_map:
        _write_key_direct(project_dir, alias_map[key], value)

    if key == "TARGET_SAME_AS_SOURCE" and value == "true":
        cfg = parse_config(project_dir).data or {}
        for t_key, s_key in (
            ("TARGET_DATABASE_NAME", "SOURCE_DATABASE_NAME"),
            ("TARGET_DB_HOST", "SOURCE_DB_HOST"),
            ("TARGET_DB_PORT", "SOURCE_DB_PORT"),
            ("TARGET_DB_USER", "SOURCE_DB_USER"),
            ("TARGET_DB_PASSWORD", "SOURCE_DB_PASSWORD"),
            ("TARGET_WAREHOUSE_TYPE", "SOURCE_WAREHOUSE_TYPE"),
        ):
            if cfg.get(s_key):
                _write_key_direct(project_dir, t_key, cfg[s_key])

    return ToolResult(
        ok=True,
        output=f"Set {key}",
        data={
            "key": key,
            "value": "********" if ("PASSWORD" in key.upper()) else value,
        },
    )


def _write_key_direct(project_dir: Path, key: str, value: str) -> None:
    path = project_dir / "config.md"
    text = path.read_text(encoding="utf-8")
    pattern = rf"^(\s*{re.escape(key)}\s*:\s*).*$"
    new_text, n = re.subn(
        pattern, lambda m, v=value: m.group(1) + v, text, count=1, flags=re.MULTILINE
    )
    if n == 0:
        fence = re.search(r"```(.*?)```", text, flags=re.DOTALL)
        if fence:
            block = fence.group(1).rstrip() + f"\n{key}: {value}\n"
            new_text = text[: fence.start()] + "```" + block + "```" + text[fence.end() :]
        else:
            new_text = text.rstrip() + f"\n{key}: {value}\n"
    path.write_text(new_text, encoding="utf-8")


def apply_intake_defaults(project_dir: Path) -> list[str]:
    """Fill safe defaults for non-asked config keys still placeholder."""
    applied: list[str] = []
    path = project_dir / "config.md"
    raw: dict[str, str] = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        for key in CONFIG_KEYS:
            m = re.search(rf"^\s*{re.escape(key)}\s*:\s*(.+)$", text, re.MULTILINE)
            if m:
                val = _strip_inline_comment(key, m.group(1).strip())
                raw[key] = val
        for src, legacy in (
            ("SOURCE_WAREHOUSE_TYPE", "WAREHOUSE_TYPE"),
            ("SOURCE_DATABASE_NAME", "DATABASE_NAME"),
            ("SOURCE_SCHEMA_NAME", "SCHEMA_NAME"),
            ("SOURCE_DB_HOST", "DB_HOST"),
            ("SOURCE_DB_PORT", "DB_PORT"),
            ("SOURCE_DB_USER", "DB_USER"),
            ("SOURCE_DB_PASSWORD", "DB_PASSWORD"),
        ):
            if _is_placeholder(raw.get(src, "")) and not _is_placeholder(raw.get(legacy, "")):
                _write_key_direct(project_dir, src, raw[legacy])
                raw[src] = raw[legacy]
                applied.append(f"{src}<={legacy}")

    for key, default in INTAKE_DEFAULTS.items():
        if _is_placeholder(raw.get(key, "")):
            res = set_config_value(project_dir, key, default)
            if res.ok:
                raw[key] = default
                applied.append(f"{key}={default}")
    return applied


def requirements_incomplete(project_dir: Path) -> bool:
    """True when requirements.md is missing, still a kit template, or too thin."""
    path = project_dir / "requirements.md"
    if not path.exists():
        return True
    text = path.read_text(encoding="utf-8")
    if "{{" in text:
        return True
    # Strip headings / blank lines for a rough substance check
    body = re.sub(r"(?m)^#+.*$", "", text)
    body = re.sub(r"(?m)^\*.*$", "", body)
    body = re.sub(r"(?m)^>.*$", "", body)
    body = re.sub(r"-{3,}", "", body)
    body = re.sub(r"\s+", " ", body).strip()
    return len(body) < 40


def write_requirements_text(project_dir: Path, content: str) -> ToolResult:
    path = project_dir / "requirements.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content.strip()
    if not text.startswith("#"):
        text = "# Business Requirements\n\n" + text + "\n"
    path.write_text(text, encoding="utf-8")
    return ToolResult(ok=True, output=f"Wrote requirements.md ({len(text)} chars)")


def approve_design_brief(project_dir: Path) -> ToolResult:
    path = project_dir / "design_brief.md"
    if not path.exists():
        return ToolResult(ok=False, output="design_brief.md not found")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    changed = False
    for i, line in enumerate(lines):
        low = line.lower().strip()
        if low.startswith("**status:**") or low.startswith("status:"):
            if line.strip().startswith("**"):
                lines[i] = "**Status:** approved"
            else:
                lines[i] = "Status: approved"
            changed = True
            break
    if not changed:
        lines.insert(0, "**Status:** approved")
        changed = True
    path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    return ToolResult(ok=True, output="design_brief.md Status set to approved")


def read_kit(project_dir: Path) -> ToolResult:
    from security_util import redact_config_dict, redact_config_text

    files = {}
    for name in ("config.md", "requirements.md", "design_brief.md", "client_feedback.md"):
        p = project_dir / name
        if p.exists():
            content = p.read_text(encoding="utf-8")
            if name == "config.md":
                content = redact_config_text(content)
            # Cap size for LLM context
            if len(content) > 30000:
                content = content[:30000] + "\n\n...[truncated]..."
            files[name] = content
    cfg = parse_config(project_dir)
    safe_cfg = redact_config_dict(cfg.data or {})
    return ToolResult(
        ok=True,
        output=f"Loaded kit files: {list(files)}",
        data={"files": files, "config": safe_cfg},
    )


def read_project_file(project_dir: Path, path: str) -> ToolResult:
    try:
        target = _safe_join(project_dir, path)
    except ValueError as exc:
        return ToolResult(ok=False, output=str(exc))
    if not target.exists():
        return ToolResult(ok=False, output=f"File not found: {path}")
    text = target.read_text(encoding="utf-8")
    if len(text) > 100000:
        text = text[:100000] + "\n...[truncated]..."
    return ToolResult(ok=True, output=text, data={"path": path})


def write_project_file(project_dir: Path, path: str, content: str) -> ToolResult:
    try:
        target = _safe_join(project_dir, path)
    except ValueError as exc:
        return ToolResult(ok=False, output=str(exc))
    # Block overwriting imported mart TMDL tables
    norm = path.replace("\\", "/").lower()
    if "/tables/" in norm and norm.endswith(".tmdl") and "powerbi" in norm:
        if "_kpis" not in norm and "relationship" not in norm:
            return ToolResult(
                ok=False,
                output="Refusing to write mart tables/*.tmdl — human Desktop owns those",
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return ToolResult(ok=True, output=f"Wrote {path}", data={"path": path})


def list_project_files(project_dir: Path, glob_pattern: str = "**/*") -> ToolResult:
    root = project_dir.resolve()
    pattern = (glob_pattern or "**/*").strip() or "**/*"
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        return ToolResult(ok=False, output="glob_pattern must stay under the project folder")
    matches = []
    for p in root.glob(pattern):
        if p.is_file():
            try:
                rel = p.resolve().relative_to(root)
            except ValueError:
                continue
            matches.append(str(rel).replace("\\", "/"))
    matches = sorted(matches)[:500]
    return ToolResult(
        ok=True,
        output="\n".join(matches) if matches else "(no files)",
        data={"files": matches},
    )
