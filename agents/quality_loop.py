"""QualityLoopAgent — Q5/Q7/Q8a/Q8b audits + findings."""

from __future__ import annotations

from typing import Any

from agents.base import AgentResult, BaseAgent, load_prompt
from logging_util import log_event
from tools.quality_checks import audit_q8a_relationships, run_q7_build_checks
from tools.registry import QUALITY_TOOLS


class QualityLoopAgent(BaseAgent):
    name = "quality_loop"
    allowlist = QUALITY_TOOLS

    def run(self, phase: int, context: dict[str, Any]) -> AgentResult:
        del phase
        checkpoint = context.get("checkpoint", "Q5")
        if checkpoint == "Q5":
            return self._q5(context)
        if checkpoint == "Q7":
            return self._q7(context)
        if checkpoint == "Q8a":
            return self._q8a(context)
        if checkpoint == "Q8b":
            return self._q8b(context)
        return AgentResult(ok=False, reply=f"Unknown checkpoint {checkpoint}")

    def _finalize(self, checkpoint: str, data: dict) -> AgentResult:
        status = data.get("status", "needs_rework")
        failures = data.get("failures") or []
        rework_target = data.get("rework_target")
        suggested = ""
        if failures:
            suggested = self.llm_fill(
                load_prompt("system.md") + "\n" + load_prompt(f"quality_{checkpoint.lower()}.md"),
                f"Summarize fix direction for these QA failures in 5 bullets:\n{failures}",
                max_tokens=500,
            )
        wf = self.tool(
            "write_quality_findings",
            checkpoint=checkpoint,
            status=status,
            rework_target=rework_target,
            failures=failures,
            suggested_fix=suggested if not suggested.startswith("[LLM") else "See failures.",
        )
        log_event(
            "INFO",
            "quality_checkpoint",
            checkpoint=checkpoint,
            status=status,
            rework_target=rework_target,
            failure_count=len(failures),
        )
        return AgentResult(
            ok=status == "pass",
            reply=f"{checkpoint}: {status}. Findings: QUALITY_FINDINGS.md\n{wf.output}",
            artifacts={
                "checkpoint": checkpoint,
                "status": status,
                "rework_target": rework_target,
                "findings_path": "QUALITY_FINDINGS.md",
                "failures": failures,
            },
        )

    def _q5(self, context: dict[str, Any]) -> AgentResult:
        del context
        res = self.tool("audit_brief_vs_marts")
        return self._finalize("Q5", res.data or {})

    def _q7(self, context: dict[str, Any]) -> AgentResult:
        confirmed = bool(context.get("confirmed_build"))
        # Use module helper for parse/build; also expose via tools conceptually
        res = run_q7_build_checks(self.project_dir, confirmed_build=confirmed)
        return self._finalize("Q7", res.data or {})

    def _q8a(self, context: dict[str, Any]) -> AgentResult:
        del context
        cfg = self.tool("parse_config")
        bi_dir = (cfg.data or {}).get("BI_PBIP_DIR", "powerbi-project")
        res = audit_q8a_relationships(self.project_dir, bi_dir)
        return self._finalize("Q8a", res.data or {})

    def _q8b(self, context: dict[str, Any]) -> AgentResult:
        del context
        cfg = self.tool("parse_config")
        bi_dir = (cfg.data or {}).get("BI_PBIP_DIR", "powerbi-project")
        res = self.tool("audit_visual_relationship_paths", bi_pbip_dir=bi_dir)
        return self._finalize("Q8b", res.data or {})
