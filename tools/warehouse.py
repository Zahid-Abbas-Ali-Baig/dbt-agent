"""Dynamic warehouse adapters (v1: postgres) + discovery/validation helpers.

SOURCE = landed tables. TARGET = dbt write destination.
Types are pluggable via WAREHOUSE_ADAPTERS for future releases.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from tools.files import SUPPORTED_WAREHOUSE_TYPES, parse_config
from tools.mcp_runtime import (
    mcp_call_tool,
    mcp_call_tool_after_connect,
    postgres_mcp_spec,
    powerbi_mcp_spec,
)
from tools.registry import ToolResult

Side = str  # "source" | "target"


def _cfg(project_dir: Path) -> dict[str, str]:
    return parse_config(project_dir).data or {}


def side_connection(project_dir: Path, side: Side = "source") -> dict[str, str]:
    cfg = _cfg(project_dir)
    side = (side or "source").lower()
    if side == "target":
        return {
            "warehouse_type": (cfg.get("TARGET_WAREHOUSE_TYPE") or "postgres").lower(),
            "host": cfg.get("TARGET_DB_HOST", ""),
            "port": cfg.get("TARGET_DB_PORT", "5432"),
            "user": cfg.get("TARGET_DB_USER", ""),
            "password": cfg.get("TARGET_DB_PASSWORD", ""),
            "database": cfg.get("TARGET_DATABASE_NAME", ""),
            "schema": cfg.get("MARTS_SCHEMA") or cfg.get("STAGING_SCHEMA") or "public",
            "source_schema": cfg.get("SOURCE_SCHEMA_NAME") or cfg.get("SCHEMA_NAME", ""),
        }
    return {
        "warehouse_type": (cfg.get("SOURCE_WAREHOUSE_TYPE") or cfg.get("WAREHOUSE_TYPE") or "postgres").lower(),
        "host": cfg.get("SOURCE_DB_HOST") or cfg.get("DB_HOST", ""),
        "port": cfg.get("SOURCE_DB_PORT") or cfg.get("DB_PORT", "5432"),
        "user": cfg.get("SOURCE_DB_USER") or cfg.get("DB_USER", ""),
        "password": cfg.get("SOURCE_DB_PASSWORD") or cfg.get("DB_PASSWORD", ""),
        "database": cfg.get("SOURCE_DATABASE_NAME") or cfg.get("DATABASE_NAME", ""),
        "schema": cfg.get("SOURCE_SCHEMA_NAME") or cfg.get("SCHEMA_NAME", ""),
        "source_schema": cfg.get("SOURCE_SCHEMA_NAME") or cfg.get("SCHEMA_NAME", ""),
    }


def connection_fingerprint(project_dir: Path, side: Side = "source") -> str:
    from security_util import secret_fingerprint

    c = side_connection(project_dir, side)
    return "|".join(
        [
            c.get("warehouse_type", ""),
            c.get("host", ""),
            c.get("port", ""),
            c.get("user", ""),
            c.get("database", ""),
            c.get("schema", ""),
            secret_fingerprint(c.get("password", "")),
        ]
    )


def _pg_connect(conn: dict[str, str]):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Install psycopg[binary]") from exc
    return psycopg.connect(
        host=conn["host"],
        port=int(conn.get("port") or 5432),
        user=conn["user"],
        password=conn.get("password") or "",
        dbname=conn["database"],
        connect_timeout=8,
    )


def test_postgres_side(project_dir: Path, side: Side = "source") -> ToolResult:
    conn = side_connection(project_dir, side)
    host, user, database = conn["host"], conn["user"], conn["database"]
    schema = conn["schema"] if side == "source" else conn.get("source_schema") or ""
    # For target, verify DB connectivity; schema may not exist yet
    check_schema = conn["schema"] if side == "source" else ""

    missing = [k for k, v in [("host", host), ("user", user), ("database", database)] if not v]
    if missing:
        return ToolResult(ok=False, output=f"Missing {side} fields: {', '.join(missing)}")

    try:
        pg = _pg_connect(conn)
        try:
            with pg.cursor() as cur:
                cur.execute("SELECT version()")
                version = str((cur.fetchone() or [""])[0])[:120]
                data: dict[str, Any] = {
                    "side": side,
                    "host": host,
                    "database": database,
                    "version": version,
                    "warehouse_type": "postgres",
                }
                if check_schema:
                    cur.execute(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = %s)",
                        (check_schema,),
                    )
                    exists = bool((cur.fetchone() or [False])[0])
                    data["schema"] = check_schema
                    data["schema_exists"] = exists
                    if not exists:
                        return ToolResult(
                            ok=False,
                            output=(
                                f"Connected to {side} {database}@{host}, "
                                f"but schema `{check_schema}` was not found."
                            ),
                            data={**data, "confirmed": False},
                        )
                    cur.execute(
                        """
                        SELECT COUNT(*) FROM information_schema.tables
                        WHERE table_schema = %s AND table_type = 'BASE TABLE'
                        """,
                        (check_schema,),
                    )
                    data["table_count"] = int((cur.fetchone() or [0])[0])
        finally:
            pg.close()
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, output=f"Could not reach {side} Postgres: {exc}")

    extra = ""
    if data.get("table_count") is not None:
        extra = f", {data['table_count']} table(s) in `{check_schema}`"
    return ToolResult(
        ok=True,
        output=f"Postgres {side} link confirmed: {user}@{host}/{database}{extra}.",
        data={**data, "confirmed": True},
    )


def test_warehouse(project_dir: Path, side: Side = "source") -> ToolResult:
    conn = side_connection(project_dir, side)
    wh = conn["warehouse_type"]
    adapter = WAREHOUSE_ADAPTERS.get(wh)
    if not adapter:
        return ToolResult(
            ok=False,
            output=(
                f"Warehouse type `{wh}` is not enabled yet for live checks. "
                f"Supported: {', '.join(SUPPORTED_WAREHOUSE_TYPES)}"
            ),
        )
    return adapter["test"](project_dir, side)


def list_tables(project_dir: Path, side: Side = "source", schema: str | None = None) -> ToolResult:
    conn = side_connection(project_dir, side)
    wh = conn["warehouse_type"]
    adapter = WAREHOUSE_ADAPTERS.get(wh)
    if not adapter:
        return ToolResult(ok=False, output=f"No adapter for {wh}")
    return adapter["list_tables"](project_dir, side, schema)


def describe_table(
    project_dir: Path, table: str, side: Side = "source", schema: str | None = None
) -> ToolResult:
    conn = side_connection(project_dir, side)
    wh = conn["warehouse_type"]
    adapter = WAREHOUSE_ADAPTERS.get(wh)
    if not adapter:
        return ToolResult(ok=False, output=f"No adapter for {wh}")
    return adapter["describe_table"](project_dir, side, table, schema)


def warehouse_query(project_dir: Path, sql: str, side: Side = "source") -> ToolResult:
    conn = side_connection(project_dir, side)
    wh = conn["warehouse_type"]
    adapter = WAREHOUSE_ADAPTERS.get(wh)
    if not adapter:
        return ToolResult(ok=False, output=f"No adapter for {wh}")
    return adapter["query"](project_dir, side, sql)


def _parse_mcp_json(text: str) -> Any:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Sometimes wrapped in prose — take first JSON array/object
    for opener, closer in (("[", "]"), ("{", "}")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _tables_from_mcp_list(output: str) -> list[str]:
    data = _parse_mcp_json(output)
    tables: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = item.get("table_name") or item.get("name") or item.get("table")
                if name:
                    tables.append(str(name))
            elif isinstance(item, str) and item.strip():
                tables.append(item.strip())
        return tables
    # Plain newline list fallback
    for ln in (output or "").splitlines():
        t = ln.strip().strip("-").strip()
        if t and " " not in t and not t.startswith(("{", "[", "}")):
            tables.append(t)
    return tables


def _describe_text_from_mcp(output: str, schema: str, table: str) -> str:
    data = _parse_mcp_json(output)
    if isinstance(data, list) and data and isinstance(data[0], dict) and "column_name" in data[0]:
        lines = [f"{schema}.{table}"]
        for col in data:
            name = col.get("column_name") or col.get("name")
            if not name:
                continue
            dtype = col.get("data_type") or col.get("type") or "?"
            nullable = col.get("is_nullable") or col.get("nullable") or "?"
            lines.append(f"{name} {dtype} null={nullable}")
        return "\n".join(lines)
    return output or ""


def _rows_from_mcp_query(output: str) -> list[dict[str, Any]]:
    data = _parse_mcp_json(output)
    if isinstance(data, list) and (not data or isinstance(data[0], dict)):
        return list(data)
    return []


def discover_source_schema(
    project_dir: Path,
    *,
    tables: list[str] | None = None,
    describe: bool = True,
) -> ToolResult:
    """Discovery bundle for Phase 1: list tables (no cap) + optional column describes.

    Prefers Postgres MCP when MCP_POSTGRES_CMD is set; falls back to native.
    MCP is always connected with engagement SOURCE credentials (same DB as config).

    Pass ``tables`` to describe/profile a confirmed in-scope set only.
    Pass ``describe=False`` to return names only (fast full-schema inventory).
    """
    cfg = parse_config(project_dir).data or {}
    schema = cfg.get("SOURCE_SCHEMA_NAME") or cfg.get("SCHEMA_NAME") or "public"
    use_mcp = postgres_mcp_spec() is not None
    via = "mcp" if use_mcp else "native"
    all_tables: list[str] = []
    details: list[dict[str, Any]] = []
    mcp_err = ""

    if use_mcp:
        mcp = mcp_postgres_tool(project_dir, "list_tables", {"schema": schema})
        if mcp.ok:
            all_tables = _tables_from_mcp_list(mcp.output or "")
            if not all_tables:
                data_tables = (mcp.data or {}).get("tables") if isinstance(mcp.data, dict) else None
                if data_tables:
                    all_tables = list(data_tables)
        else:
            mcp_err = mcp.output or "MCP list_tables failed"
        if not all_tables:
            listed = list_tables(project_dir, "source", schema)
            if listed.ok:
                via = "native_after_mcp" if use_mcp else "native"
                all_tables = list((listed.data or {}).get("tables") or [])
            elif not mcp.ok:
                return mcp
    else:
        listed = list_tables(project_dir, "source", schema)
        if not listed.ok:
            return listed
        all_tables = list((listed.data or {}).get("tables") or [])

    # Dedupe, keep stable order
    seen: set[str] = set()
    ordered: list[str] = []
    for t in all_tables:
        name = str(t).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    all_tables = ordered

    if tables is not None:
        wanted = {str(t).strip() for t in tables if str(t).strip()}
        missing = sorted(wanted - set(all_tables))
        target_tables = [t for t in all_tables if t in wanted]
        # Preserve caller order for in-scope preference
        caller_order = [str(t).strip() for t in tables if str(t).strip()]
        by_name = {t: t for t in target_tables}
        target_tables = [by_name[t] for t in caller_order if t in by_name]
        if missing:
            mcp_err = (mcp_err + " " if mcp_err else "") + f"not found: {', '.join(missing[:20])}"
    else:
        target_tables = list(all_tables)

    if describe:
        for name in target_tables:
            if via == "mcp":
                desc = mcp_postgres_tool(
                    project_dir,
                    "describe_table",
                    {"schema": schema, "table": name},
                )
                if desc.ok:
                    detail = _describe_text_from_mcp(desc.output or "", schema, name)
                else:
                    native = describe_table(project_dir, name, "source", schema)
                    detail = native.output if native.ok else (desc.output or native.output)
                    desc = native
            else:
                desc = describe_table(project_dir, name, "source", schema)
                detail = desc.output or ""
            details.append({"table": name, "ok": desc.ok, "detail": (detail or "")[:8000]})

    note = f" ({mcp_err[:200]})" if mcp_err else ""
    scope_note = (
        f" (describing {len(target_tables)} of {len(all_tables)} listed)"
        if tables is not None
        else ""
    )
    summary = (
        f"Source discovery via {via}: {len(all_tables)} table(s) listed in `{schema}`"
        f"{scope_note}{note}.\n"
        + "\n".join(f"- {t}" for t in (target_tables if describe else all_tables))
    )
    return ToolResult(
        ok=True,
        output=summary,
        data={
            "tables": target_tables if describe else all_tables,
            "all_tables": all_tables,
            "details": details,
            "side": "source",
            "via": via,
            "schema": schema,
            "described": bool(describe),
        },
    )


def profile_landed_tables(
    project_dir: Path,
    *,
    tables: list[str] | None = None,
    details: list[dict[str, Any]] | None = None,
    max_fk_cols: int = 12,
) -> ToolResult:
    """Profile landed tables for Phase 1 §3.5 / §4: row counts + char FK tri-state.

    No table-count cap. Pass ``tables`` (preferred: confirmed in-scope set).
    If omitted, profiles every table in the schema (can be slow on large CRM replicas).
    """
    cfg = parse_config(project_dir).data or {}
    schema = cfg.get("SOURCE_SCHEMA_NAME") or cfg.get("SCHEMA_NAME") or "public"

    detail_by_table: dict[str, str] = {}
    if details:
        for d in details:
            name = str(d.get("table") or "").strip()
            if name:
                detail_by_table[name] = str(d.get("detail") or "")

    if tables is None:
        disc = discover_source_schema(project_dir, describe=False)
        if not disc.ok:
            return disc
        tables = list((disc.data or {}).get("all_tables") or (disc.data or {}).get("tables") or [])
        via = (disc.data or {}).get("via")
    else:
        tables = [str(t).strip() for t in tables if str(t).strip()]
        via = "caller"

    # Ensure we have column text for FK heuristics
    missing_desc = [t for t in tables if t not in detail_by_table]
    if missing_desc:
        desc = discover_source_schema(project_dir, tables=missing_desc, describe=True)
        if desc.ok:
            via = (desc.data or {}).get("via") or via
            for d in (desc.data or {}).get("details") or []:
                name = str(d.get("table") or "").strip()
                if name:
                    detail_by_table[name] = str(d.get("detail") or "")

    profiles: list[dict[str, Any]] = []

    def _run_sql(sql: str) -> ToolResult:
        res = warehouse_query(project_dir, sql, "source")
        if res.ok:
            return res
        return mcp_postgres_tool(project_dir, "query", {"sql": sql})

    for table in tables:
        cols_text = detail_by_table.get(table) or ""
        id_cols: list[str] = []
        for line in cols_text.splitlines():
            parts = line.strip().split()
            if not parts:
                continue
            col = parts[0]
            low = col.lower()
            if low == "id" or low.endswith("_id") or low.endswith("_key") or low.endswith("_fk"):
                id_cols.append(col)
        id_cols = id_cols[:max_fk_cols]

        fq = f'"{schema}"."{table}"'
        row = _run_sql(f"SELECT COUNT(*) AS n FROM {fq}")
        row_count = None
        if row.ok:
            rows = (row.data or {}).get("rows") or []
            if rows and "n" in rows[0]:
                try:
                    row_count = int(rows[0]["n"])
                except (TypeError, ValueError):
                    row_count = None
            else:
                # MCP may return plain text
                m = re.search(r"\b(\d+)\b", row.output or "")
                if m:
                    try:
                        row_count = int(m.group(1))
                    except ValueError:
                        row_count = None

        fk_stats: list[dict[str, Any]] = []
        for col in id_cols:
            q = (
                f'SELECT '
                f'COUNT(*) FILTER (WHERE "{col}" IS NULL) AS null_n, '
                f'COUNT(*) FILTER (WHERE "{col}" IS NOT NULL AND TRIM(CAST("{col}" AS TEXT)) = \'\') AS empty_n, '
                f'COUNT(*) FILTER (WHERE "{col}" IS NOT NULL AND TRIM(CAST("{col}" AS TEXT)) <> \'\') AS nonempty_n '
                f'FROM {fq}'
            )
            st = _run_sql(q)
            entry: dict[str, Any] = {"column": col, "ok": st.ok}
            if st.ok:
                rows = (st.data or {}).get("rows") or []
                if rows:
                    entry.update(
                        {
                            "null_n": rows[0].get("null_n"),
                            "empty_n": rows[0].get("empty_n"),
                            "nonempty_n": rows[0].get("nonempty_n"),
                        }
                    )
                else:
                    entry["raw"] = (st.output or "")[:500]
            else:
                entry["error"] = (st.output or "")[:300]
            fk_stats.append(entry)

        sample = _run_sql(f"SELECT * FROM {fq} LIMIT 5")
        profiles.append(
            {
                "table": table,
                "row_count": row_count,
                "id_columns": id_cols,
                "fk_tri_state": fk_stats,
                "sample_ok": sample.ok,
                "sample": (sample.output or "")[:1500],
            }
        )

    lines = [f"Landed profiles for `{schema}` ({len(profiles)} tables):"]
    for p in profiles:
        lines.append(f"\n### {p['table']} rows={p.get('row_count')}")
        for fk in p.get("fk_tri_state") or []:
            if fk.get("ok"):
                lines.append(
                    f"- {fk['column']}: null={fk.get('null_n')} empty={fk.get('empty_n')} "
                    f"nonempty={fk.get('nonempty_n')}"
                )
            else:
                lines.append(f"- {fk['column']}: profile failed ({fk.get('error', '')[:120]})")
    return ToolResult(
        ok=True,
        output="\n".join(lines),
        data={
            "schema": schema,
            "profiles": profiles,
            "tables": tables,
            "via": via,
        },
    )


# --- Postgres adapter (native; MCP optional overlay) ---


def _pg_list_tables(project_dir: Path, side: Side, schema: str | None) -> ToolResult:
    conn = side_connection(project_dir, side)
    schema = schema or conn["schema"]
    try:
        pg = _pg_connect(conn)
        try:
            with pg.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = %s AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                    """,
                    (schema,),
                )
                tables = [r[0] for r in cur.fetchall()]
        finally:
            pg.close()
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, output=str(exc))
    return ToolResult(
        ok=True,
        output="\n".join(tables) if tables else "(no tables)",
        data={"tables": tables, "schema": schema, "side": side},
    )


def _pg_describe(project_dir: Path, side: Side, table: str, schema: str | None) -> ToolResult:
    conn = side_connection(project_dir, side)
    schema = schema or conn["schema"]
    try:
        pg = _pg_connect(conn)
        try:
            with pg.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name, data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (schema, table),
                )
                cols = [
                    {"name": r[0], "type": r[1], "nullable": r[2]} for r in cur.fetchall()
                ]
        finally:
            pg.close()
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, output=str(exc))
    lines = [f"{c['name']} {c['type']} null={c['nullable']}" for c in cols]
    return ToolResult(
        ok=True,
        output=f"{schema}.{table}\n" + "\n".join(lines),
        data={"table": table, "schema": schema, "columns": cols},
    )


def _pg_query(project_dir: Path, side: Side, sql: str) -> ToolResult:
    from security_util import assert_readonly_sql

    try:
        assert_readonly_sql(sql)
    except ValueError as exc:
        return ToolResult(ok=False, output=str(exc))
    conn = side_connection(project_dir, side)
    try:
        pg = _pg_connect(conn)
        try:
            # Force a read-only transaction with a short statement timeout
            pg.set_session(readonly=True, autocommit=False)
            with pg.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = 15000")
                cur.execute(sql)
                rows = cur.fetchmany(200)
                cols = [d[0] for d in (cur.description or [])]
            pg.rollback()
        finally:
            pg.close()
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, output=str(exc))
    preview = [dict(zip(cols, row)) for row in rows]
    return ToolResult(
        ok=True,
        output=f"{len(preview)} row(s)\n" + str(preview)[:8000],
        data={"columns": cols, "rows": preview},
    )


WAREHOUSE_ADAPTERS: dict[str, dict[str, Callable[..., ToolResult]]] = {
    "postgres": {
        "test": test_postgres_side,
        "list_tables": _pg_list_tables,
        "describe_table": _pg_describe,
        "query": _pg_query,
    }
}


# --- MCP-facing tools (agent disposable) ---


def mcp_postgres_tool(
    project_dir: Path, tool: str, arguments: dict[str, Any] | None = None
) -> ToolResult:
    """Call Postgres MCP using engagement SOURCE credentials (same DB as config.md).

    Always opens a session with connect_db for the engagement SOURCE host/db/user
    so MCP never uses a stale Cursor-default database.
    """
    from security_util import assert_readonly_sql

    conn = side_connection(project_dir, "source")
    # mcp-postgres-server reads PG_* ; also require non-empty password for its env path
    password = conn.get("password") or ""
    env = {
        "PG_HOST": conn["host"],
        "PG_PORT": str(conn["port"] or "5432"),
        "PG_DATABASE": conn["database"],
        "PG_USER": conn["user"],
        # Package getEnvConfig requires a truthy password; keep real value when set
        "PG_PASSWORD": password if password else " ",
    }
    spec = postgres_mcp_spec(env_overrides=env)
    args = dict(arguments or {})

    if tool in ("query", "execute"):
        sql = str(args.get("sql") or "")
        if tool == "execute":
            return ToolResult(ok=False, output="MCP execute (writes) is disabled by the agent")
        try:
            assert_readonly_sql(sql)
        except ValueError as exc:
            return ToolResult(ok=False, output=str(exc))

    if not spec:
        # Fallback: native tools map
        if tool == "list_tables":
            return list_tables(project_dir, "source", (arguments or {}).get("schema"))
        if tool == "describe_table":
            return describe_table(
                project_dir,
                (arguments or {}).get("table", ""),
                "source",
                (arguments or {}).get("schema"),
            )
        if tool == "query":
            return warehouse_query(project_dir, (arguments or {}).get("sql", ""), "source")
        if tool == "connect_db":
            return test_warehouse(project_dir, "source")
        return ToolResult(
            ok=False,
            output="MCP_POSTGRES_CMD not set in .env; using native postgres adapter where possible.",
        )

    connect_args: dict[str, Any] = {
        "host": conn["host"],
        "port": int(conn["port"] or 5432),
        "user": conn["user"],
        "password": password,
        "database": conn["database"],
    }
    if tool == "connect_db":
        # Explicit connect only — still use engagement SOURCE credentials
        res = mcp_call_tool(spec, "connect_db", args or connect_args)
    else:
        res = mcp_call_tool_after_connect(
            spec,
            tool,
            args,
            connect_tool="connect_db",
            connect_arguments=connect_args,
        )

    if not res.ok:
        return res

    data = dict(res.data or {})
    data["source"] = {
        "host": conn["host"],
        "port": conn["port"],
        "database": conn["database"],
        "user": conn["user"],
        "schema": conn.get("schema"),
    }

    if tool == "list_tables":
        tables = _tables_from_mcp_list(res.output or "")
        data["tables"] = tables
        data["schema"] = (arguments or {}).get("schema") or conn.get("schema")
        return ToolResult(
            ok=True,
            output="\n".join(tables) if tables else (res.output or "(no tables)"),
            data=data,
        )

    if tool == "describe_table":
        schema = (arguments or {}).get("schema") or conn.get("schema") or "public"
        table = (arguments or {}).get("table") or ""
        detail = _describe_text_from_mcp(res.output or "", schema, table)
        return ToolResult(ok=True, output=detail, data=data)

    if tool == "query":
        rows = _rows_from_mcp_query(res.output or "")
        if rows:
            data["rows"] = rows
            data["columns"] = list(rows[0].keys()) if rows else []
            return ToolResult(
                ok=True,
                output=f"{len(rows)} row(s)\n" + str(rows)[:8000],
                data=data,
            )

    return ToolResult(ok=True, output=res.output, data=data)


def _powerbi_mcp_success(text: str) -> bool:
    """Power BI MCP often returns HTTP 200 with {\"success\": false} in the body."""
    raw = (text or "").strip()
    if not raw:
        return False
    lower = raw.lower()
    if '"success": false' in lower or '"success":false' in lower:
        return False
    if "no connectionname provided" in lower:
        return False
    if "failed to" in lower and "success" in lower:
        return False
    return True


def mcp_powerbi_tool(
    project_dir: Path,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    bi_pbip_dir: str | None = None,
) -> ToolResult:
    """Call Power BI Modeling MCP (relationships, tables, measures, etc.).

    Opens a ConnectFolder session against the engagement SemanticModel when needed,
    matching how Postgres MCP always connect_db first.
    """
    spec = powerbi_mcp_spec()
    if not spec:
        return ToolResult(
            ok=False,
            output="MCP_POWERBI_CMD not set in .env — Power BI MCP unavailable.",
        )

    args = dict(arguments or {})
    cfg = _cfg(project_dir)
    bi_dir = (bi_pbip_dir or cfg.get("BI_PBIP_DIR") or "powerbi-project").strip()

    # connection_operations itself (Connect / ListConnections) — no prior connect
    if tool == "connection_operations":
        res = mcp_call_tool(spec, tool, args)
        if res.ok and not _powerbi_mcp_success(res.output or ""):
            return ToolResult(ok=False, output=res.output, data=res.data)
        return res

    from tools.pbip import find_semantic_model_folder

    model = find_semantic_model_folder(project_dir, bi_dir)
    if model is None:
        # Fall back to bare call (may fail without last-used connection)
        res = mcp_call_tool(spec, tool, args)
        if res.ok and not _powerbi_mcp_success(res.output or ""):
            return ToolResult(
                ok=False,
                output=res.output
                or "Power BI MCP returned success:false (no SemanticModel folder to ConnectFolder).",
                data=res.data,
            )
        return res

    folder = str(model.resolve())
    connect_args = {
        "request": {
            "operation": "ConnectFolder",
            "folderPath": folder,
        }
    }
    res = mcp_call_tool_after_connect(
        spec,
        tool,
        args,
        connect_tool="connection_operations",
        connect_arguments=connect_args,
    )
    if res.ok and not _powerbi_mcp_success(res.output or ""):
        return ToolResult(ok=False, output=res.output, data={**(res.data or {}), "folder": folder})
    if res.ok:
        data = dict(res.data or {})
        data["folder"] = folder
        return ToolResult(ok=True, output=res.output, data=data)
    return res


# Back-compat name used by orchestrator
def test_postgres_connection(project_dir: Path) -> ToolResult:
    return test_postgres_side(project_dir, "source")
