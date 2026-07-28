"""dbt CLI wrapper with command whitelist."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tools.registry import ToolResult

ALLOWED_SUBCOMMANDS = {
    "deps",
    "parse",
    "compile",
    "list",
    "show",
    "test",
    "run",
    "build",
    "run-operation",
    "debug",
}

DESTRUCTIVE = {"run", "build"}


def dbt_cli(
    project_dir: Path,
    args: list[str],
    *,
    confirmed: bool = False,
    timeout: int = 600,
) -> ToolResult:
    if not args:
        return ToolResult(ok=False, output="dbt_cli requires args, e.g. ['parse']")
    sub = args[0]
    if sub not in ALLOWED_SUBCOMMANDS:
        return ToolResult(
            ok=False,
            output=f"dbt subcommand {sub!r} not allowed. Allowed: {sorted(ALLOWED_SUBCOMMANDS)}",
        )
    if sub in DESTRUCTIVE and not confirmed:
        return ToolResult(
            ok=False,
            output=f"Confirmation required before dbt {sub}. Re-run with confirmed=true after human OK.",
            data={"needs_confirmation": True, "command": ["dbt", *args]},
        )

    # Reject shell-like injection and profiles-dir escapes
    for a in args:
        if any(c in a for c in (";", "|", "&", "`", "$(", "\n", "\r")):
            return ToolResult(ok=False, output=f"Rejected unsafe dbt arg: {a!r}")
    for i, a in enumerate(args):
        if a in ("--profiles-dir", "--project-dir") and i + 1 < len(args):
            try:
                target = Path(args[i + 1]).expanduser().resolve()
                target.relative_to(project_dir.resolve())
            except (ValueError, OSError):
                return ToolResult(
                    ok=False,
                    output=f"{a} must stay under the engagement folder",
                )

    exe = os.getenv("DBT_EXECUTABLE", "").strip()
    if exe:
        cmd = [exe, *args]
    else:
        import sys

        scripts = Path(sys.executable).resolve().parent
        local = next(
            (scripts / name for name in ("dbt.exe", "dbt") if (scripts / name).exists()),
            None,
        )
        if local is None:
            return ToolResult(
                ok=False,
                output=(
                    f"dbt not installed in this environment ({sys.executable}). "
                    "Run: pip install dbt-core dbt-postgres"
                ),
            )
        cmd = [str(local), *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError:
        return ToolResult(
            ok=False,
            output=(
                f"dbt not found ({' '.join(cmd[:3])}). "
                "Install with: pip install dbt-core dbt-postgres "
                "(or set DBT_EXECUTABLE)."
            ),
        )
    except subprocess.TimeoutExpired:
        return ToolResult(ok=False, output=f"dbt timed out after {timeout}s: {' '.join(cmd)}")

    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if len(out) > 80000:
        out = out[:40000] + "\n...[truncated]...\n" + out[-20000:]
    return ToolResult(
        ok=proc.returncode == 0,
        output=out.strip() or f"(exit {proc.returncode}, no output)",
        data={"returncode": proc.returncode, "cmd": cmd},
    )
