"""BiAgent — Phase 8 relationships, KPIs, wireframe, visuals."""

from __future__ import annotations

from typing import Any

from agents.base import AgentResult, BaseAgent, load_prompt
from tools.pbip import (
    build_relationships_tmdl,
    find_semantic_model_folder,
    list_imported_pbi_tables,
    sanitize_relationships_tmdl,
)
from tools.registry import BI_TOOLS


class BiAgent(BaseAgent):
    name = "bi"
    allowlist = BI_TOOLS

    def run(self, phase: int, context: dict[str, Any]) -> AgentResult:
        del phase
        mode = context.get("bi_mode", "relationships")  # relationships | visuals | rework
        if mode == "visuals":
            return self._visuals(context)
        if mode == "rework":
            return self._rework(context)
        return self._relationships_and_kpis(context)

    def _bi_dir(self) -> str:
        cfg = self.tool("parse_config")
        return (cfg.data or {}).get("BI_PBIP_DIR", "powerbi-project")

    def _semantic_rel_paths(self, bi_dir: str) -> tuple[str, str]:
        """Return (relationships.tmdl, tables/_KPIs.tmdl) relative to BI_PBIP_DIR."""
        model = find_semantic_model_folder(self.project_dir, bi_dir)
        if model is not None:
            try:
                rel_model = model.relative_to((self.project_dir / bi_dir).resolve())
                prefix = str(rel_model).replace("\\", "/")
            except ValueError:
                prefix = model.name
            return (
                f"{prefix}/definition/relationships.tmdl",
                f"{prefix}/definition/tables/_KPIs.tmdl",
            )
        listed = self.tool("list_project_files", glob_pattern=f"{bi_dir}/**/relationships.tmdl")
        existing = (listed.data or {}).get("files") or []
        if existing:
            rel_path = existing[0]
            if rel_path.replace("\\", "/").startswith(bi_dir.replace("\\", "/")):
                rel_path = rel_path[len(bi_dir) :].lstrip("/\\")
            kpi = rel_path.replace("relationships.tmdl", "tables/_KPIs.tmdl")
            return rel_path.replace("\\", "/"), kpi.replace("\\", "/")
        return (
            "demo.SemanticModel/definition/relationships.tmdl",
            "demo.SemanticModel/definition/tables/_KPIs.tmdl",
        )

    def _relationships_and_kpis(self, context: dict[str, Any]) -> AgentResult:
        del context
        kit = self.tool("read_kit")
        brief = ((kit.data or {}).get("files") or {}).get("design_brief.md", "")
        bi_dir = self._bi_dir()
        built = build_relationships_tmdl(self.project_dir, bi_dir, brief)
        if not built.ok:
            # Last resort: ask the model for TMDL-only content, then validate
            system = load_prompt("system.md") + "\n" + load_prompt("phase_8.md")
            imported = list_imported_pbi_tables(self.project_dir, bi_dir)
            raw = self.llm_fill(
                system,
                "Return ONLY valid Power BI TMDL relationship blocks — no prose, no markdown fences. "
                "Use fromColumn/toColumn. Quote table names that contain spaces "
                "(e.g. fromColumn: 'marts fct_orders'.customer_key).\n"
                f"IMPORTED TABLES:\n{sorted(set(imported.values()))}\n"
                f"BRIEF §8:\n{brief[:10000]}\n"
                f"BUILDER ERROR:\n{built.output}",
                max_tokens=4000,
            )
            raw = raw.replace("```tmdl", "").replace("```", "").strip()
            # Keep only lines that look like TMDL relationship content
            if "fromColumn:" not in raw.lower():
                return AgentResult(
                    ok=False,
                    reply=(
                        f"Phase 8a failed: could not build relationships.tmdl ({built.output}). "
                        "Import marts in Desktop first, then re-run Phase 8a."
                    ),
                    phase=8,
                )
            rel_tmdl = sanitize_relationships_tmdl(raw)
            if not rel_tmdl:
                return AgentResult(
                    ok=False,
                    reply=(
                        "Phase 8a failed: LLM fallback returned prose without TMDL relationship blocks. "
                        f"Builder error was: {built.output}"
                    ),
                    phase=8,
                )
            count_note = "LLM fallback TMDL"
        else:
            rel_tmdl = (built.data or {}).get("content") or ""
            count_note = built.output
            skipped = (built.data or {}).get("skipped") or []
            if skipped:
                count_note += f" (skipped {len(skipped)} unmapped)"

        rel_rel, kpi_rel = self._semantic_rel_paths(bi_dir)
        w1 = self.tool("pbip_write", rel_path=rel_rel, content=rel_tmdl, bi_pbip_dir=bi_dir)

        # Validate what we wrote is parseable before claiming success
        parsed = self.tool("parse_pbip_relationships", bi_pbip_dir=bi_dir)
        n_rels = len((parsed.data or {}).get("relationships") or [])
        if w1.ok and n_rels == 0:
            return AgentResult(
                ok=False,
                reply=(
                    f"Phase 8a wrote `{rel_rel}` but Q8a parser found 0 relationships. "
                    "File must use fromColumn/toColumn TMDL (quoted names if spaces)."
                ),
                phase=8,
                artifacts={"relationships": rel_rel},
            )

        imported = list_imported_pbi_tables(self.project_dir, bi_dir)
        fact = next(
            (imported[k] for k in imported if k.startswith("fct_")),
            "fct_example",
        )
        fact_q = f"'{fact}'" if " " in fact else fact
        kpis = f"""table _KPIs
\tmeasure example_row_count = COUNTROWS({fact_q})
"""
        w2 = self.tool("pbip_write", rel_path=kpi_rel, content=kpis, bi_pbip_dir=bi_dir)

        wireframe = (
            "# Phase 8 Wireframe\n\n"
            "## Pages\n"
            "1. Executive — totals/trends\n"
            "2. Detail — dimensional breakdowns\n\n"
            "Human: reply `Wireframe approved` to continue to visuals.\n"
        )
        self.tool("write_project_file", path="WIREFRAME.md", content=wireframe)

        # Q8a source of truth is on-disk TMDL parse — do not call Power BI MCP List here.
        # A fresh MCP stdio session has no Desktop connection; List then returns
        # success:false and was previously mis-reported as "ok", which confused the loop.

        return AgentResult(
            ok=w1.ok and n_rels > 0,
            reply=(
                f"Phase 8a: relationships → `{rel_rel}` ({'ok' if w1.ok else w1.output}; "
                f"{n_rels} parsed; {count_note}); "
                f"_KPIs → ({'ok' if w2.ok else w2.output}).\n"
                "Wrote WIREFRAME.md. QualityLoop Q8a will validate relationships before visuals.\n"
                "Human gate after Q8a pass: `Wireframe approved`."
            ),
            phase=8,
            artifacts={"relationships": rel_rel, "wireframe": "WIREFRAME.md", "relationship_count": n_rels},
        )

    def _visuals(self, context: dict[str, Any]) -> AgentResult:
        del context
        bi_dir = self._bi_dir()
        _, kpi_rel = self._semantic_rel_paths(bi_dir)
        # Report next to SemanticModel when possible
        model = find_semantic_model_folder(self.project_dir, bi_dir)
        if model is not None:
            report_name = model.name.replace(".SemanticModel", ".Report")
            path = f"{report_name}/report.json"
        else:
            path = "demo.Report/report.json"
        report = """{
  "pages": [
    {
      "name": "Executive",
      "visuals": [
        {"type": "card", "Entity": "_KPIs", "Property": "example_row_count"}
      ]
    }
  ]
}
"""
        w = self.tool("pbip_write", rel_path=path, content=report, bi_pbip_dir=bi_dir)
        return AgentResult(
            ok=w.ok,
            reply=f"Phase 8b: wrote report stub `{bi_dir}/{path}`. Q8b will audit paths.",
            phase=8,
            artifacts={"report": f"{bi_dir}/{path}", "kpis": kpi_rel},
        )

    def _rework(self, context: dict[str, Any]) -> AgentResult:
        del context
        findings = self.tool("read_project_file", path="QUALITY_FINDINGS.md")
        # Re-run relationships with findings context
        return self._relationships_and_kpis({"findings": findings.output})
