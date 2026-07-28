"""Minimal MCP stdio client for agent tool use (Postgres / Power BI / future)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.registry import ToolResult


@dataclass
class McpServerSpec:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def postgres_mcp_spec(*, env_overrides: dict[str, str] | None = None) -> McpServerSpec | None:
    cmd = (os.getenv("MCP_POSTGRES_CMD") or "").strip()
    if not cmd:
        return None
    args_raw = (os.getenv("MCP_POSTGRES_ARGS") or "").strip()
    args = [a for a in args_raw.split(" ") if a] if args_raw else []
    env = {**(env_overrides or {})}

    # Prefer node + index.js over Windows .cmd wrappers (more reliable under Popen)
    cmd_path = Path(cmd)
    if cmd_path.suffix.lower() == ".cmd":
        js = (
            cmd_path.parent
            / "node_modules"
            / "mcp-postgres-server"
            / "build"
            / "index.js"
        )
        if js.exists():
            node = (os.getenv("CURSOR_NODE_EXE") or os.getenv("NODE_EXE") or "node").strip()
            return McpServerSpec(name="postgres", command=node, args=[str(js), *args], env=env)

    return McpServerSpec(name="postgres", command=cmd, args=args, env=env)


def powerbi_mcp_spec() -> McpServerSpec | None:
    cmd = (os.getenv("MCP_POWERBI_CMD") or "").strip()
    if not cmd:
        return None
    args_raw = (os.getenv("MCP_POWERBI_ARGS") or "--start").strip()
    args = [a for a in args_raw.split(" ") if a]
    return McpServerSpec(name="powerbi", command=cmd, args=args, env={})


class McpStdioSession:
    """JSON-RPC MCP session over stdio. Short-lived: start → call → close."""

    def __init__(self, spec: McpServerSpec, *, timeout: float = 45.0):
        self.spec = spec
        self.timeout = timeout
        self._proc: subprocess.Popen[str] | None = None
        self._id = 0
        self._lock = threading.Lock()
        self._stderr_lines: list[str] = []

    def __enter__(self) -> "McpStdioSession":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> None:
        env = os.environ.copy()
        env.update({k: str(v) for k, v in self.spec.env.items() if v is not None})
        self._proc = subprocess.Popen(
            [self.spec.command, *self.spec.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            shell=False,
        )
        # Drain stderr so the pipe never blocks
        assert self._proc.stderr is not None

        def _drain() -> None:
            assert self._proc and self._proc.stderr
            for line in self._proc.stderr:
                if len(self._stderr_lines) < 50:
                    self._stderr_lines.append(line.rstrip())

        threading.Thread(target=_drain, daemon=True).start()
        self._initialize()

    def close(self) -> None:
        if not self._proc:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._proc = None

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        line = json.dumps(payload, ensure_ascii=False)
        self._proc.stdin.write(line + "\n")
        self._proc.stdin.flush()

    def _recv(self) -> dict[str, Any]:
        assert self._proc and self._proc.stdout
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                err = "\n".join(self._stderr_lines[-10:])
                raise RuntimeError(
                    f"MCP server {self.spec.name} exited early. stderr: {err or '(empty)'}"
                )
            line = self._proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Ignore notifications / server requests without result matching our style
            if "result" in msg or "error" in msg:
                return msg
        raise TimeoutError(f"MCP {self.spec.name} timed out after {self.timeout}s")

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            req_id = self._next_id()
            payload: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
            }
            if params is not None:
                payload["params"] = params
            self._send(payload)
            while True:
                msg = self._recv()
                if msg.get("id") != req_id:
                    # Unrelated message; keep reading briefly
                    continue
                if "error" in msg:
                    raise RuntimeError(f"MCP error: {msg['error']}")
                return msg.get("result")

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "dbt-agent", "version": "1.0.0"},
            },
        )
        # notifications/initialized (no response expected)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = self._request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
        )
        return result


def mcp_call_tool(spec: McpServerSpec, tool: str, arguments: dict[str, Any] | None = None) -> ToolResult:
    try:
        try:
            from activity_log import push_activity

            push_activity(
                f"MCP `{spec.name}` → `{tool}`",
                kind="tool",
                detail=str(arguments or {})[:200] or None,
            )
        except Exception:  # noqa: BLE001
            pass
        with McpStdioSession(spec) as session:
            result = session.call_tool(tool, arguments or {})
        # MCP content is often {"content":[{"type":"text","text":"..."}]}
        text = _flatten_mcp_result(result)
        try:
            from activity_log import push_activity

            push_activity(
                f"MCP `{spec.name}` → `{tool}` ok",
                kind="tool",
                detail=(text or "")[:240],
            )
        except Exception:  # noqa: BLE001
            pass
        return ToolResult(ok=True, output=text, data={"raw": result, "tool": tool, "server": spec.name})
    except Exception as exc:  # noqa: BLE001
        try:
            from activity_log import push_activity

            push_activity(
                f"MCP `{spec.name}` → `{tool}` failed",
                kind="error",
                detail=str(exc)[:240],
            )
        except Exception:  # noqa: BLE001
            pass
        return ToolResult(
            ok=False,
            output=f"MCP {spec.name}/{tool} failed: {exc}",
            data={"tool": tool, "server": spec.name, "error": str(exc)},
        )


def mcp_call_tool_after_connect(
    spec: McpServerSpec,
    tool: str,
    arguments: dict[str, Any] | None,
    *,
    connect_tool: str,
    connect_arguments: dict[str, Any],
) -> ToolResult:
    """One stdio session: connect with engagement credentials, then run the tool.

    Needed for MCP servers that keep connection state in-process (e.g. mcp-postgres).
    """
    try:
        try:
            from activity_log import push_activity

            push_activity(
                f"MCP `{spec.name}` → `{connect_tool}` + `{tool}`",
                kind="tool",
                detail=str(
                    {
                        "database": connect_arguments.get("database"),
                        "host": connect_arguments.get("host"),
                        "tool": tool,
                    }
                )[:200],
            )
        except Exception:  # noqa: BLE001
            pass
        with McpStdioSession(spec) as session:
            connect_result = session.call_tool(connect_tool, connect_arguments)
            connect_text = _flatten_mcp_result(connect_result)
            # Power BI (and similar) often return HTTP-ok JSON with success:false
            if '"success": false' in connect_text.lower() or '"success":false' in connect_text.lower():
                return ToolResult(
                    ok=False,
                    output=f"MCP {spec.name}/{connect_tool} failed: {connect_text[:1500]}",
                    data={
                        "tool": tool,
                        "server": spec.name,
                        "connect_tool": connect_tool,
                        "error": connect_text[:1500],
                    },
                )
            result = session.call_tool(tool, arguments or {})
        text = _flatten_mcp_result(result)
        try:
            from activity_log import push_activity

            push_activity(
                f"MCP `{spec.name}` → `{tool}` ok",
                kind="tool",
                detail=(text or "")[:240],
            )
        except Exception:  # noqa: BLE001
            pass
        return ToolResult(
            ok=True,
            output=text,
            data={"raw": result, "tool": tool, "server": spec.name, "connected": True},
        )
    except Exception as exc:  # noqa: BLE001
        try:
            from activity_log import push_activity

            push_activity(
                f"MCP `{spec.name}` → `{tool}` failed",
                kind="error",
                detail=str(exc)[:240],
            )
        except Exception:  # noqa: BLE001
            pass
        return ToolResult(
            ok=False,
            output=f"MCP {spec.name}/{tool} failed: {exc}",
            data={"tool": tool, "server": spec.name, "error": str(exc)},
        )


def _flatten_mcp_result(result: Any) -> str:
    if result is None:
        return "(empty)"
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            if parts:
                return "\n".join(parts)
        return json.dumps(result, ensure_ascii=False, indent=2)[:20000]
    return json.dumps(result, ensure_ascii=False, indent=2)[:20000]
