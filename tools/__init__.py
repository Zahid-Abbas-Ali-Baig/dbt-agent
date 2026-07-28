"""Build the shared tool registry bound to a project directory."""

from __future__ import annotations

from pathlib import Path

from tools import dbt_cli as dbt_mod
from tools import files as files_mod
from tools import pbip as pbip_mod
from tools import quality_checks as qa_mod
from tools.registry import (
    BI_TOOLS,
    DISCOVERY_TOOLS,
    MODELING_TOOLS,
    QUALITY_TOOLS,
    SEMANTIC_TOOLS,
    ToolRegistry,
)
from tools import warehouse as wh_mod


def build_registry(project_dir: Path) -> ToolRegistry:
    reg = ToolRegistry()
    bi_dir_holder = {"dir": "powerbi-project"}

    def _cfg_bi_dir() -> str:
        cfg = files_mod.parse_config(project_dir)
        return (cfg.data or {}).get("BI_PBIP_DIR", bi_dir_holder["dir"])

    reg.register("read_kit", lambda: files_mod.read_kit(project_dir))
    reg.register("parse_config", lambda: files_mod.parse_config(project_dir))
    reg.register(
        "read_project_file",
        lambda path: files_mod.read_project_file(project_dir, path),
    )
    reg.register(
        "write_project_file",
        lambda path, content: files_mod.write_project_file(project_dir, path, content),
    )
    reg.register(
        "list_project_files",
        lambda glob_pattern="**/*": files_mod.list_project_files(project_dir, glob_pattern),
    )
    reg.register(
        "dbt_cli",
        lambda args, confirmed=False: dbt_mod.dbt_cli(project_dir, list(args), confirmed=confirmed),
    )
    reg.register(
        "pbip_read",
        lambda rel_path, bi_pbip_dir=None: pbip_mod.pbip_read(
            project_dir, bi_pbip_dir or _cfg_bi_dir(), rel_path
        ),
    )
    reg.register(
        "pbip_write",
        lambda rel_path, content, bi_pbip_dir=None: pbip_mod.pbip_write(
            project_dir, bi_pbip_dir or _cfg_bi_dir(), rel_path, content
        ),
    )
    reg.register(
        "parse_pbip_relationships",
        lambda bi_pbip_dir=None: pbip_mod.parse_pbip_relationships(
            project_dir, bi_pbip_dir or _cfg_bi_dir()
        ),
    )
    reg.register("audit_brief_vs_marts", lambda: qa_mod.audit_brief_vs_marts(project_dir))
    reg.register(
        "audit_visual_relationship_paths",
        lambda bi_pbip_dir=None: qa_mod.audit_visual_relationship_paths(
            project_dir, bi_pbip_dir or _cfg_bi_dir()
        ),
    )
    reg.register(
        "write_quality_findings",
        lambda checkpoint, status, rework_target, failures, suggested_fix="": qa_mod.write_quality_findings(
            project_dir, checkpoint, status, rework_target, failures, suggested_fix
        ),
    )

    # Warehouse / MCP tools (discovery + validation)
    reg.register(
        "warehouse_test",
        lambda side="source": wh_mod.test_warehouse(project_dir, side),
    )
    reg.register(
        "warehouse_list_tables",
        lambda side="source", schema=None: wh_mod.list_tables(project_dir, side, schema),
    )
    reg.register(
        "warehouse_describe_table",
        lambda table, side="source", schema=None: wh_mod.describe_table(
            project_dir, table, side, schema
        ),
    )
    reg.register(
        "warehouse_query",
        lambda sql, side="source": wh_mod.warehouse_query(project_dir, sql, side),
    )
    reg.register(
        "warehouse_discover_source",
        lambda tables=None, describe=True: wh_mod.discover_source_schema(
            project_dir, tables=tables, describe=describe
        ),
    )
    reg.register(
        "warehouse_profile_landed",
        lambda tables=None, details=None: wh_mod.profile_landed_tables(
            project_dir, tables=tables, details=details
        ),
    )
    reg.register(
        "mcp_postgres",
        lambda tool, arguments=None: wh_mod.mcp_postgres_tool(project_dir, tool, arguments or {}),
    )
    reg.register(
        "mcp_powerbi",
        lambda tool, arguments=None, bi_pbip_dir=None: wh_mod.mcp_powerbi_tool(
            project_dir, tool, arguments or {}, bi_pbip_dir=bi_pbip_dir
        ),
    )

    # Allowlist constants imported for agents / docs; keep referenced.
    _ = (DISCOVERY_TOOLS, MODELING_TOOLS, SEMANTIC_TOOLS, BI_TOOLS, QUALITY_TOOLS)
    return reg
