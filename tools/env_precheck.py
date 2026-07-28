"""Runtime environment precheck before dbt project bootstrap (Phase 0).

Checks everything the engagement needs. When auto_install=True (default),
missing pip/npm pieces are installed into this interpreter / agent root.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.registry import ToolResult

_AGENT_ROOT = Path(__file__).resolve().parent.parent

# pip packages required to run the agent (beyond a bare Python)
AGENT_IMPORTS = (
    ("flask", "flask"),
    ("dotenv", "python-dotenv"),
    ("openai", "openai"),
    ("psycopg", "psycopg[binary]"),
)

# Warehouse type -> dbt adapter pip package
DBT_ADAPTERS = {
    "postgres": "dbt-postgres",
}


@dataclass
class CheckItem:
    name: str
    ok: bool
    detail: str
    blocking: bool = True
    fixed: bool = False


@dataclass
class PrecheckReport:
    ok: bool
    items: list[CheckItem] = field(default_factory=list)
    installed: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        lines = ["Environment precheck:"]
        for it in self.items:
            mark = "OK" if it.ok else ("FIXED" if it.fixed else "FAIL")
            soft = "" if it.blocking else " (optional)"
            lines.append(f"- [{mark}]{soft} {it.name}: {it.detail}")
        if self.installed:
            lines.append("Installed / fixed: " + ", ".join(self.installed))
        if self.ok:
            lines.append("All required checks passed.")
        else:
            fails = [i.name for i in self.items if not i.ok and i.blocking]
            lines.append("Blocked until these are fixed: " + ", ".join(fails))
        return "\n".join(lines)

    def as_data(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "installed": list(self.installed),
            "items": [
                {
                    "name": i.name,
                    "ok": i.ok,
                    "detail": i.detail,
                    "blocking": i.blocking,
                    "fixed": i.fixed,
                }
                for i in self.items
            ],
        }


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _path_exists(raw: str) -> bool:
    p = Path(raw.strip().strip('"'))
    if p.exists():
        return True
    found = shutil.which(raw)
    return bool(found)


def _push(msg: str, *, kind: str = "tool") -> None:
    try:
        from activity_log import push_activity

        push_activity(msg, kind=kind, agent="precheck")
    except Exception:  # noqa: BLE001
        pass


def _pip_install(*packages: str) -> tuple[bool, str]:
    cmd = [sys.executable, "-m", "pip", "install", *packages]
    _push(f"pip install {' '.join(packages)[:120]}…")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=900,
            shell=False,
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if len(out) > 2000:
        out = out[-2000:]
    return proc.returncode == 0, out or f"exit {proc.returncode}"


def _npm_install(*args: str) -> tuple[bool, str]:
    npm = _which("npm") or _which("npm.cmd")
    if not npm:
        return False, "npm not found on PATH"
    cmd = [npm, "install", *args]
    _push(f"npm install {' '.join(args)[:120]}…")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_AGENT_ROOT),
            capture_output=True,
            text=True,
            timeout=600,
            shell=False,
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if len(out) > 2000:
        out = out[-2000:]
    return proc.returncode == 0, out or f"exit {proc.returncode}"


def _module_ok(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        # Partially installed / broken package name
        return False


def _dbt_cmd() -> list[str]:
    """Prefer the dbt console script next to this interpreter (venv-safe on Windows)."""
    explicit = (os.getenv("DBT_EXECUTABLE") or "").strip()
    if explicit:
        return [explicit]
    scripts = Path(sys.executable).resolve().parent
    for name in ("dbt.exe", "dbt"):
        candidate = scripts / name
        if candidate.exists():
            return [str(candidate)]
    found = shutil.which("dbt")
    if found:
        return [found]
    return []


def _run_dbt_version(cmd: list[str] | None = None) -> tuple[bool, str]:
    base = list(cmd if cmd is not None else _dbt_cmd())
    if not base:
        return False, "dbt executable not found in this venv or on PATH"
    argv = base + ["--version"]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=60,
            shell=False,
        )
    except FileNotFoundError:
        return False, f"not found ({' '.join(argv[:2])})"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return False, text[:500] or f"exit {proc.returncode}"
    # Prefer "installed: x.y" line when present
    for line in text.splitlines():
        if "installed:" in line.lower():
            return True, line.strip()[:200]
    first = text.splitlines()[0] if text else "dbt --version ok"
    return True, first[:200]


def _warehouse_type(project_dir: Path | None) -> str:
    if project_dir is None:
        return "postgres"
    try:
        from tools.files import parse_config

        cfg = parse_config(project_dir).data or {}
        wh = (
            cfg.get("TARGET_WAREHOUSE_TYPE")
            or cfg.get("SOURCE_WAREHOUSE_TYPE")
            or cfg.get("WAREHOUSE_TYPE")
            or "postgres"
        )
        return str(wh).strip().lower() or "postgres"
    except Exception:  # noqa: BLE001
        return "postgres"


def _ensure_pip_module(
    report: PrecheckReport,
    *,
    label: str,
    module: str,
    pip_name: str,
    auto_install: bool,
    blocking: bool = True,
) -> None:
    if _module_ok(module):
        report.items.append(CheckItem(label, True, "importable", blocking=blocking))
        return
    if not auto_install:
        report.items.append(
            CheckItem(label, False, f"missing — pip install {pip_name}", blocking=blocking)
        )
        return
    ok, detail = _pip_install(pip_name)
    # Invalidate cached find_spec
    importlib.invalidate_caches()
    if ok and _module_ok(module):
        report.installed.append(pip_name)
        report.items.append(
            CheckItem(label, True, f"installed {pip_name}", blocking=blocking, fixed=True)
        )
        return
    report.items.append(
        CheckItem(
            label,
            False,
            f"missing; pip install failed: {detail[:300]}",
            blocking=blocking,
        )
    )


def ensure_runtime_ready(
    project_dir: Path | None = None,
    *,
    auto_install: bool = True,
) -> ToolResult:
    """Check (and install/fix when possible) everything needed before Phase 0."""
    _push("Running environment precheck…")
    report = PrecheckReport(ok=True)
    wh = _warehouse_type(project_dir)
    adapter_pkg = DBT_ADAPTERS.get(wh, f"dbt-{wh}")
    provider = (os.getenv("LLM_PROVIDER") or "").strip().lower()

    # --- Sync requirements.txt when anything critical is missing ---
    req_file = _AGENT_ROOT / "requirements.txt"
    need_req = (not _module_ok("dbt")) or any(
        not _module_ok(mod) for mod, _ in AGENT_IMPORTS
    )
    if provider == "cursor" and not _module_ok("cursor_sdk"):
        need_req = True
    if auto_install and need_req and req_file.exists():
        ok, detail = _pip_install("-r", str(req_file))
        importlib.invalidate_caches()
        if ok:
            report.installed.append(f"-r {req_file.name}")
            report.items.append(
                CheckItem(
                    "requirements.txt",
                    True,
                    f"synced into {sys.executable}",
                    fixed=True,
                )
            )
        else:
            report.items.append(
                CheckItem(
                    "requirements.txt",
                    False,
                    f"pip install -r failed: {detail[:300]}",
                    blocking=False,
                )
            )

    # --- Python agent imports ---
    for module, pip_name in AGENT_IMPORTS:
        _ensure_pip_module(
            report,
            label=f"python:{module}",
            module=module,
            pip_name=pip_name,
            auto_install=auto_install,
        )

    # cursor-sdk when using Cursor brain
    if provider == "cursor":
        _ensure_pip_module(
            report,
            label="python:cursor_sdk",
            module="cursor_sdk",
            pip_name="cursor-sdk",
            auto_install=auto_install,
        )
    elif provider:
        report.items.append(
            CheckItem("brain provider", True, f"LLM_PROVIDER={provider}")
        )
    else:
        # Brain form usually sets this earlier; soft so precheck can still install dbt
        report.items.append(
            CheckItem(
                "brain provider",
                True,
                "LLM_PROVIDER unset — set via brain form before discovery",
                blocking=False,
            )
        )

    # --- dbt Core + adapter (install, then re-resolve venv dbt.exe) ---
    dbt_cmd = _dbt_cmd()
    dbt_ok, dbt_detail = _run_dbt_version(dbt_cmd)
    if (not dbt_ok or not _module_ok("dbt")) and auto_install:
        pkgs = ["dbt-core", adapter_pkg]
        ok, detail = _pip_install(*pkgs)
        importlib.invalidate_caches()
        if ok:
            report.installed.extend(pkgs)
            dbt_cmd = _dbt_cmd()  # pick up .venv/Scripts/dbt.exe after install
            dbt_ok, dbt_detail = _run_dbt_version(dbt_cmd)
            if dbt_ok and _module_ok("dbt"):
                report.items.append(
                    CheckItem(
                        "dbt",
                        True,
                        f"{dbt_detail} via {dbt_cmd[0] if dbt_cmd else 'dbt'}",
                        fixed=True,
                    )
                )
            else:
                report.items.append(
                    CheckItem(
                        "dbt",
                        False,
                        f"packages installed but still failing: {dbt_detail}",
                    )
                )
        else:
            report.items.append(
                CheckItem(
                    "dbt",
                    False,
                    f"not found; pip install failed: {detail[:300]}",
                )
            )
    elif not dbt_ok:
        report.items.append(
            CheckItem(
                "dbt",
                False,
                f"{dbt_detail}. Will auto-install dbt-core + {adapter_pkg} when auto_install is on.",
            )
        )
    else:
        report.items.append(
            CheckItem(
                "dbt",
                True,
                f"{dbt_detail} via {dbt_cmd[0] if dbt_cmd else 'dbt'}",
            )
        )

    if _module_ok("dbt"):
        report.items.append(
            CheckItem("python:dbt", True, f"importable ({sys.executable})")
        )
    else:
        report.items.append(
            CheckItem(
                "python:dbt",
                False,
                f"No module named 'dbt' in {sys.executable}",
            )
        )

    # Adapter
    if wh == "postgres":
        adapter_mod = "dbt.adapters.postgres"
        if _module_ok(adapter_mod):
            report.items.append(
                CheckItem(f"dbt-adapter:{wh}", True, f"{adapter_pkg} importable")
            )
        elif auto_install:
            ok, detail = _pip_install(adapter_pkg)
            importlib.invalidate_caches()
            if ok and _module_ok(adapter_mod):
                report.installed.append(adapter_pkg)
                report.items.append(
                    CheckItem(
                        f"dbt-adapter:{wh}",
                        True,
                        f"installed {adapter_pkg}",
                        fixed=True,
                    )
                )
            elif dbt_ok:
                # dbt --version may still list the plugin
                report.items.append(
                    CheckItem(
                        f"dbt-adapter:{wh}",
                        True,
                        f"dbt CLI ok; adapter import soft ({adapter_pkg})",
                        blocking=False,
                    )
                )
            else:
                report.items.append(
                    CheckItem(
                        f"dbt-adapter:{wh}",
                        False,
                        f"missing; pip install failed: {detail[:300]}",
                    )
                )
        else:
            report.items.append(
                CheckItem(
                    f"dbt-adapter:{wh}",
                    False,
                    f"missing — pip install {adapter_pkg}",
                )
            )
    elif wh not in DBT_ADAPTERS:
        report.items.append(
            CheckItem(
                f"dbt-adapter:{wh}",
                False,
                f"unsupported warehouse type {wh!r} in v1 (postgres only)",
            )
        )

    # --- Playbook prompts ---
    prompts_ok = (_AGENT_ROOT / "prompts" / "system.md").exists() and (
        _AGENT_ROOT / "prompts" / "phase_0.md"
    ).exists()
    report.items.append(
        CheckItem(
            "playbook prompts",
            prompts_ok,
            "prompts/system.md + phase_0.md present"
            if prompts_ok
            else "missing prompts — clone incomplete",
        )
    )

    # --- Cursor Node bridge ---
    if provider == "cursor":
        bridge = _AGENT_ROOT / "scripts" / "cursor_chat.mjs"
        if bridge.exists():
            report.items.append(CheckItem("cursor bridge", True, str(bridge.name)))
        else:
            report.items.append(
                CheckItem(
                    "cursor bridge",
                    False,
                    f"missing {bridge} — needed for LLM_PROVIDER=cursor",
                )
            )

        node = (os.getenv("CURSOR_NODE_EXE") or "node").strip()
        node_path = node if Path(node).exists() else _which(node)
        if node_path:
            report.items.append(CheckItem("node (Cursor)", True, str(node_path)))
        else:
            report.items.append(
                CheckItem(
                    "node (Cursor)",
                    False,
                    "Node not found. Install Node.js or set CURSOR_NODE_EXE in .env.",
                )
            )

        nm = _AGENT_ROOT / "node_modules" / "@cursor" / "sdk"
        pkg_json = _AGENT_ROOT / "package.json"
        if nm.exists():
            report.items.append(CheckItem("npm:@cursor/sdk", True, "present"))
        elif auto_install and node_path and pkg_json.exists():
            ok, detail = _npm_install()
            if ok and nm.exists():
                report.installed.append("npm install")
                report.items.append(
                    CheckItem("npm:@cursor/sdk", True, "npm install completed", fixed=True)
                )
            else:
                # Try explicit package
                ok2, detail2 = _npm_install("@cursor/sdk")
                if ok2 and nm.exists():
                    report.installed.append("npm:@cursor/sdk")
                    report.items.append(
                        CheckItem(
                            "npm:@cursor/sdk",
                            True,
                            "installed @cursor/sdk",
                            fixed=True,
                        )
                    )
                else:
                    report.items.append(
                        CheckItem(
                            "npm:@cursor/sdk",
                            False,
                            f"npm install failed: {(detail2 or detail)[:300]}",
                            blocking=False,
                        )
                    )
        else:
            report.items.append(
                CheckItem(
                    "npm:@cursor/sdk",
                    False,
                    "missing — run npm install in dbt-agent",
                    blocking=False,
                )
            )

    # --- MCP commands (configured = must exist; try to resolve npx when possible) ---
    pg_cmd = (os.getenv("MCP_POSTGRES_CMD") or "").strip()
    if pg_cmd:
        if _path_exists(pg_cmd):
            report.items.append(CheckItem("MCP postgres", True, pg_cmd))
        else:
            # Common: npx / global node package path — report clearly
            report.items.append(
                CheckItem(
                    "MCP postgres",
                    False,
                    f"MCP_POSTGRES_CMD not found: {pg_cmd}. Fix the path in .env "
                    "(native warehouse tools still work without MCP).",
                    blocking=False,
                )
            )
    else:
        report.items.append(
            CheckItem(
                "MCP postgres",
                True,
                "not set — native warehouse tools will be used",
                blocking=False,
            )
        )

    pbi_cmd = (os.getenv("MCP_POWERBI_CMD") or "").strip()
    if pbi_cmd:
        if _path_exists(pbi_cmd):
            report.items.append(CheckItem("MCP powerbi", True, pbi_cmd, blocking=False))
        else:
            report.items.append(
                CheckItem(
                    "MCP powerbi",
                    False,
                    f"MCP_POWERBI_CMD not found: {pbi_cmd}",
                    blocking=False,
                )
            )
    else:
        report.items.append(
            CheckItem(
                "MCP powerbi",
                True,
                "not set — Phase 8 skips live Power BI MCP validate",
                blocking=False,
            )
        )

    report.ok = all(i.ok for i in report.items if i.blocking)
    text = report.as_text()
    _push(
        "Environment precheck " + ("passed" if report.ok else "failed"),
        kind="tool" if report.ok else "error",
    )
    return ToolResult(ok=report.ok, output=text, data=report.as_data())
