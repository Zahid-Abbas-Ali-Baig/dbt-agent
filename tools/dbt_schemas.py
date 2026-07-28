"""Ensure dbt layer schemas use plain names: staging / intermediate / marts.

By default dbt prefixes the profiles.yml ``schema`` onto each model's custom
schema (e.g. staging + marts → staging_marts). This project overrides that so
warehouse schemas match the layer names from config.md.
"""

from __future__ import annotations

from pathlib import Path

# Profile default must NOT be a layer name — otherwise unconfigured models
# land in "staging" and look like the staging layer.
DEFAULT_PROFILE_SCHEMA = "dbt"

GENERATE_SCHEMA_NAME_SQL = """\
{% macro generate_schema_name(custom_schema_name, node) -%}
{%- if custom_schema_name is none -%}
    {{ target.schema }}
{%- else -%}
    {{ custom_schema_name | trim }}
{%- endif -%}
{%- endmacro %}
"""


def ensure_plain_layer_schemas(project_dir: Path) -> Path:
    """Write macros/generate_schema_name.sql (idempotent).

    Returns the macro path.
    """
    macros = project_dir / "macros"
    macros.mkdir(parents=True, exist_ok=True)
    path = macros / "generate_schema_name.sql"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current.strip() != GENERATE_SCHEMA_NAME_SQL.strip():
        path.write_text(GENERATE_SCHEMA_NAME_SQL, encoding="utf-8")
    return path
