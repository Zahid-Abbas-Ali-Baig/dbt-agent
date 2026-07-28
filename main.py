#!/usr/bin/env python3
"""CLI entry for the generic multi-agent dbt orchestrator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# Ensure project root on path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from logging_util import log_event  # noqa: E402
from orchestrator import Orchestrator, resolve_project_dir  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Custom DBT Agent — multi-agent + quality loop")
    parser.add_argument("--project", "-p", required=True, help="Engagement project directory")
    parser.add_argument("command", nargs="*", help="Optional one-shot command (e.g. status, init)")
    args = parser.parse_args(argv)

    project_dir = resolve_project_dir(args.project)
    project_dir.mkdir(parents=True, exist_ok=True)

    orch = Orchestrator(project_dir)
    log_event("INFO", "cli_start", project=str(project_dir))

    if args.command:
        msg = " ".join(args.command)
        print(orch.handle(msg))
        return 0

    print(f"dbt-agent ready. Engagement: {project_dir}")
    print("Type `help` for commands, `quit` to exit.\n")
    while True:
        try:
            user = input("dbt-agent> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user.lower() in ("quit", "exit", "q"):
            break
        print(orch.handle(user))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
