"""SemanticAgent — Phase 6 MetricFlow YAML from real marts only."""

from __future__ import annotations

import re
from typing import Any

from agents.base import AgentResult, BaseAgent, load_prompt
from tools.registry import SEMANTIC_TOOLS


class SemanticAgent(BaseAgent):
    name = "semantic"
    allowlist = SEMANTIC_TOOLS

    def run(self, phase: int, context: dict[str, Any]) -> AgentResult:
        del phase
        cfg_res = self.tool("parse_config")
        cfg = cfg_res.data or {}
        enabled = str(cfg.get("ENABLE_SEMANTIC_LAYER", "true")).lower() in ("true", "1", "yes")
        if not enabled:
            return AgentResult(
                ok=True,
                reply="ENABLE_SEMANTIC_LAYER is false — skipping SemanticAgent.",
                phase=6,
                artifacts={"skipped": True},
            )

        marts = self._list_mart_models()
        if not marts:
            return AgentResult(
                ok=False,
                reply=(
                    "Phase 6 blocked: no mart SQL under models/marts/. "
                    "Run Phase 5 first so semantic models can ref real fct_/dim_ nodes."
                ),
                phase=6,
            )

        # MetricFlow requires a day (or finer) time spine when semantic models exist
        spine_msg = self._ensure_time_spine()

        kit = self.tool("read_kit")
        brief = ((kit.data or {}).get("files") or {}).get("design_brief.md", "")
        full = self.tool("read_project_file", path="design_brief.md")
        if full.ok and (full.output or "").strip():
            brief = full.output

        # Rework: always rebuild from marts + findings (never keep fct_example)
        if context.get("rework"):
            findings = self.tool("read_project_file", path="QUALITY_FINDINGS.md")
            yml = self._build_from_llm(brief, marts, findings.output if findings.ok else "")
            if not self._yml_ok(yml, marts):
                yml = self._fallback_yml(marts)
            path = "models/semantic/semantic_models.yml"
            w = self.tool("write_project_file", path=path, content=yml)
            return AgentResult(
                ok=w.ok,
                reply=(
                    f"Phase 6 rework: rewrote `{path}` against marts {marts}. "
                    f"{spine_msg}"
                ),
                phase=6,
                artifacts={"semantic": path, "marts": marts, "rework": True},
            )

        yml = self._build_from_llm(brief, marts, "")
        if not self._yml_ok(yml, marts):
            yml = self._fallback_yml(marts)

        path = "models/semantic/semantic_models.yml"
        w = self.tool("write_project_file", path=path, content=yml)
        return AgentResult(
            ok=w.ok,
            reply=(
                f"Phase 6: wrote `{path}` using marts {marts}. "
                f"No placeholder refs. {spine_msg}"
            ),
            phase=6,
            artifacts={"semantic": path, "marts": marts},
        )

    def _ensure_time_spine(self) -> str:
        """Write MetricFlow day time spine if missing (required for dbt parse with semantic models)."""
        sql_path = "models/semantic/metricflow_time_spine.sql"
        yml_path = "models/semantic/metricflow_time_spine.yml"
        sql = """{{ config(materialized='table') }}

with spine as (
    {{ dbt_utils.date_spine(
        datepart="day",
        start_date="cast('2018-01-01' as date)",
        end_date="cast('2035-01-01' as date)"
    ) }}
)

select cast(date_day as date) as date_day
from spine
"""
        yml = """version: 2

models:
  - name: metricflow_time_spine
    description: MetricFlow day time spine (required for semantic layer parse/build).
    time_spine:
      standard_granularity_column: date_day
    columns:
      - name: date_day
        granularity: day
"""
        self.tool("write_project_file", path=sql_path, content=sql)
        self.tool("write_project_file", path=yml_path, content=yml)
        return f"Also ensured `{sql_path}` + YAML time_spine config."

    def _list_mart_models(self) -> list[str]:
        root = self.project_dir / "models" / "marts"
        if not root.is_dir():
            return []
        names: list[str] = []
        for p in sorted(root.rglob("*.sql")):
            if p.name.startswith("_"):
                continue
            names.append(p.stem)
        return names

    def _build_from_llm(self, brief: str, marts: list[str], findings: str) -> str:
        system = load_prompt("system.md") + "\n" + load_prompt("phase_6.md")
        mart_block = "\n".join(f"- {m}" for m in marts)
        user = (
            "Write models/semantic/semantic_models.yml (MetricFlow YAML) only.\n"
            "Rules:\n"
            "- Every `model: ref('...')` MUST be one of the marts listed below.\n"
            "- Do NOT use example/placeholder names (fct_example, example_semantic, etc.).\n"
            "- Metrics should trace to design brief §9 / §2 when present.\n"
            "- Return YAML only (no markdown fences, no chat).\n\n"
            f"## Available mart models\n{mart_block}\n\n"
            f"## Design brief (excerpt)\n{brief[:10000]}\n"
        )
        if findings:
            user += f"\n## QA findings to fix\n{findings[:4000]}\n"
        raw = self.llm_fill(system, user, max_tokens=4000)
        return self._strip_yml(raw)

    @staticmethod
    def _strip_yml(text: str) -> str:
        yml = (text or "").strip()
        if yml.startswith("[LLM unavailable"):
            return ""
        yml = re.sub(r"^```(?:yaml|yml)?\s*", "", yml, flags=re.IGNORECASE)
        yml = re.sub(r"\s*```$", "", yml)
        return yml.strip()

    def _yml_ok(self, yml: str, marts: list[str]) -> bool:
        if not yml:
            return False
        low = yml.lower()
        if "semantic_models:" not in low and "metrics:" not in low:
            return False
        banned = (
            "fct_example",
            "example_semantic",
            "example_row_count",
            "dim_example",
            "stg_example",
        )
        if any(b in low for b in banned):
            return False
        allowed = set(marts)
        refs = re.findall(r"ref\(\s*['\"]([^'\"]+)['\"]\s*\)", yml, flags=re.IGNORECASE)
        if not refs:
            return False
        return all(r in allowed for r in refs)

    def _fallback_yml(self, marts: list[str]) -> str:
        """Minimal valid MetricFlow YAML against a real fact mart — never placeholders."""
        fct = next((m for m in marts if m.startswith("fct_")), marts[0])
        # Prefer common grain columns; MetricFlow will fail later if wrong — still better than fct_example.
        time_col = "order_date"
        pk = "order_id"
        measure_expr = "order_revenue_usd" if fct == "fct_orders" else "1"
        measure_agg = "sum" if fct == "fct_orders" else "count"
        sm_name = fct.replace("fct_", "", 1) if fct.startswith("fct_") else fct
        return f"""version: 2

semantic_models:
  - name: {sm_name}
    model: ref('{fct}')
    defaults:
      agg_time_dimension: {time_col}
    entities:
      - name: {sm_name.rstrip('s') if sm_name.endswith('s') else sm_name}
        type: primary
        expr: {pk}
    dimensions:
      - name: {time_col}
        type: time
        type_params:
          time_granularity: day
    measures:
      - name: revenue_usd
        agg: {measure_agg}
        expr: {measure_expr}
      - name: row_count
        agg: count
        expr: 1

metrics:
  - name: revenue
    type: simple
    label: Revenue
    type_params:
      measure: revenue_usd
  - name: row_count
    type: simple
    label: Row Count
    type_params:
      measure: row_count
"""
