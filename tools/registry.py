"""Shared tool registry with per-agent allowlists."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ToolResult:
    ok: bool
    output: str
    data: dict[str, Any] | None = None


ToolFn = Callable[..., ToolResult]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolFn] = {}

    def register(self, name: str, fn: ToolFn) -> None:
        self._tools[name] = fn

    def call(self, name: str, allowlist: set[str], **kwargs) -> ToolResult:
        if name not in allowlist:
            return ToolResult(ok=False, output=f"Tool {name!r} not allowed for this agent")
        if name not in self._tools:
            return ToolResult(ok=False, output=f"Unknown tool {name!r}")
        return self._tools[name](**kwargs)

    def names(self) -> list[str]:
        return sorted(self._tools)


# Allowlists per specialist
DISCOVERY_TOOLS = {
    "read_kit",
    "read_project_file",
    "write_project_file",
    "list_project_files",
    "dbt_cli",
    "parse_config",
    "warehouse_test",
    "warehouse_list_tables",
    "warehouse_describe_table",
    "warehouse_query",
    "warehouse_discover_source",
    "warehouse_profile_landed",
    "mcp_postgres",
}
MODELING_TOOLS = {
    "read_kit",
    "read_project_file",
    "write_project_file",
    "list_project_files",
    "dbt_cli",
    "parse_config",
    "warehouse_test",
    "warehouse_list_tables",
    "warehouse_describe_table",
    "warehouse_query",
    "mcp_postgres",
}
SEMANTIC_TOOLS = {
    "read_kit",
    "read_project_file",
    "write_project_file",
    "list_project_files",
    "dbt_cli",
    "parse_config",
    "mcp_postgres",
}
BI_TOOLS = {
    "read_kit",
    "read_project_file",
    "write_project_file",
    "list_project_files",
    "parse_config",
    "pbip_write",
    "pbip_read",
    "parse_pbip_relationships",
    "mcp_powerbi",
}
QUALITY_TOOLS = {
    "read_kit",
    "read_project_file",
    "list_project_files",
    "dbt_cli",
    "parse_config",
    "parse_pbip_relationships",
    "audit_brief_vs_marts",
    "audit_visual_relationship_paths",
    "write_quality_findings",
    "warehouse_test",
    "warehouse_query",
    "warehouse_list_tables",
    "mcp_postgres",
    "mcp_powerbi",
}
