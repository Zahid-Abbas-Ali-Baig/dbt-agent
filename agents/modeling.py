"""ModelingAgent - generic phases 2..5 generation from brief + scope artifacts."""

from __future__ import annotations

import json
import re
from typing import Any

from agents.base import AgentResult, BaseAgent, load_prompt
from logging_util import log_event
from tools.registry import MODELING_TOOLS


class ModelingAgent(BaseAgent):
    name = "modeling"
    allowlist = MODELING_TOOLS

    def run(self, phase: int, context: dict[str, Any]) -> AgentResult:
        # Keep warehouse schemas as staging / intermediate / marts (not staging_*).
        from tools.dbt_schemas import ensure_plain_layer_schemas

        ensure_plain_layer_schemas(self.project_dir)
        if phase == 2:
            return self._phase2(context)
        if phase == 3:
            return self._phase3(context)
        if phase == 4:
            return self._phase4(context)
        if phase == 5:
            return self._phase5(context)
        if context.get("rework"):
            return self._rework(context)
        return AgentResult(ok=False, reply=f"ModelingAgent does not handle phase {phase}")

    def _read_brief_and_cfg(self) -> tuple[dict[str, Any], str, AgentResult | None]:
        kit = self.tool("read_kit")
        if not kit.ok:
            return {}, "", AgentResult(ok=False, reply=kit.output)
        cfg = (kit.data or {}).get("config") or {}
        files = (kit.data or {}).get("files") or {}
        brief = files.get("design_brief.md", "")
        # read_kit truncates large files for LLM context; modeling needs full brief.
        full = self.tool("read_project_file", path="design_brief.md")
        if full.ok and (full.output or "").strip():
            brief = full.output
        return cfg, brief, None

    def _read_json_artifact(self, name: str) -> dict[str, Any]:
        r = self.tool("read_project_file", path=f".dbt_agent/{name}")
        if not r.ok:
            return {}
        try:
            data = json.loads(r.output or "{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _in_scope_tables(self, brief: str) -> list[str]:
        scope = self._read_json_artifact("scope.json")
        tables = [str(t).strip() for t in (scope.get("in_scope") or []) if str(t).strip()]
        if tables:
            return tables
        # Fallback: extract from brief source inventory / KPI table mappings.
        found = []
        seen: set[str] = set()
        for m in re.finditer(r"`([a-z][a-z0-9_]{2,})`", brief.lower()):
            t = m.group(1)
            if "_" not in t:
                continue
            if t.startswith(("stg_", "int_", "fct_", "dim_", "bridge_")):
                continue
            if t in seen:
                continue
            seen.add(t)
            found.append(t)
        return found

    def _extract_expected_models(self, brief: str, prefix: str) -> list[str]:
        pattern = re.compile(rf"\b({prefix}_[a-z0-9_]+)\b")
        seen: set[str] = set()
        out: list[str] = []
        for m in pattern.finditer((brief or "").lower()):
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def _normalize_model_name(self, table: str) -> str:
        return re.sub(r"[^a-z0-9_]+", "_", table.lower()).strip("_")

    def _parse_columns_from_discovery_detail(self, detail: str) -> list[dict[str, str]]:
        cols: list[dict[str, str]] = []
        for raw in (detail or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # skip headers like schema.table
            if "." in line and " " not in line:
                continue
            m = re.match(r"^([a-zA-Z0-9_]+)\s+(.+?)($|\s+null=)", line)
            if not m:
                continue
            col = m.group(1).strip()
            dtype = m.group(2).strip().lower()
            cols.append({"name": col, "data_type": dtype})
        return cols

    def _table_specs(self, in_scope: list[str]) -> list[dict[str, Any]]:
        details_art = self._read_json_artifact("discovery_details.json")
        detail_map: dict[str, str] = {}
        for d in (details_art.get("details") or []):
            if not isinstance(d, dict):
                continue
            table = str(d.get("table") or "").strip()
            if table:
                detail_map[table] = str(d.get("detail") or "")
        specs: list[dict[str, Any]] = []
        for table in in_scope:
            cols = self._parse_columns_from_discovery_detail(detail_map.get(table, ""))
            specs.append({"table": table, "columns": cols})
        return specs

    def _render_sources_yaml(
        self,
        source_name: str,
        source_schema: str,
        source_db: str,
        specs: list[dict[str, Any]],
    ) -> str:
        lines = [
            "version: 2",
            "",
            "sources:",
            f"  - name: {source_name}",
            f"    database: {source_db}",
            f"    schema: {source_schema}",
            "    tables:",
        ]
        for spec in specs:
            table = spec["table"]
            cols = spec.get("columns") or []
            lines.append(f"      - name: {table}")
            if cols:
                lines.append("        columns:")
                for c in cols:
                    lines.append(f"          - name: {c['name']}")
                    lines.append(f"            data_type: {c['data_type']}")
        return "\n".join(lines) + "\n"

    def _phase2(self, context: dict[str, Any]) -> AgentResult:
        del context
        cfg, brief, err = self._read_brief_and_cfg()
        if err:
            return err
        source = str(cfg.get("SOURCE_NAME") or "source").strip()
        schema = str(cfg.get("SOURCE_SCHEMA_NAME") or cfg.get("SCHEMA_NAME") or "public").strip()
        database = str(cfg.get("SOURCE_DATABASE_NAME") or cfg.get("DATABASE_NAME") or "postgres").strip()

        in_scope = self._in_scope_tables(brief)
        specs = self._table_specs(in_scope)
        if not specs:
            # Last resort fallback to dbt-codegen output; still scoped to configured schema/db.
            gen = self.tool(
                "dbt_cli",
                args=[
                    "run-operation",
                    "generate_source",
                    "--args",
                    "{"
                    + f'"schema_name": "{schema}", "database_name": "{database}", "generate_columns": true'
                    + "}",
                ],
                confirmed=True,
            )
            body = gen.output if gen.ok and "sources:" in (gen.output or "") else self._render_sources_yaml(source, schema, database, [])
        else:
            body = self._render_sources_yaml(source, schema, database, specs)

        path = f"models/staging/{source}/_sources.yml"
        w = self.tool("write_project_file", path=path, content=body)
        log_event("INFO", "phase2_sources", path=path, ok=w.ok, tables=len(specs))
        return AgentResult(
            ok=w.ok,
            reply=f"Phase 2: wrote `{path}` with {len(specs)} scoped source table(s).",
            phase=2,
            artifacts={"sources": path, "source_tables": len(specs)},
        )

    def _staging_sql(self, source_name: str, table: str, cols: list[dict[str, str]]) -> str:
        by_name = {str(c.get("name") or ""): str(c.get("data_type") or "").lower() for c in cols}
        has_deleted = "deleted" in by_name
        has_etl_deleted = "_etl_deleted_at" in by_name
        has_cols = bool(cols)

        predicates: list[str] = []
        if has_deleted:
            predicates.append("coalesce(deleted, false) = false")
        if has_etl_deleted:
            predicates.append("_etl_deleted_at is null")
        where_sql = ""
        if predicates:
            where_sql = "    where " + "\n        and ".join(predicates) + "\n"

        if not has_cols:
            projection = "        *"
        else:
            lines: list[str] = []
            for c in cols:
                col = str(c.get("name") or "")
                dtype = str(c.get("data_type") or "").lower()
                is_char = any(k in dtype for k in ("char", "text", "varchar", "string"))
                if is_char and col.endswith("_id"):
                    lines.append(f"        nullif(trim({col}), '') as {col}")
                else:
                    lines.append(f"        {col}")
            projection = ",\n".join(lines)

        return (
            "with source as (\n\n"
            "    select *\n"
            f"    from {{{{ source('{source_name}', '{table}') }}}}\n"
            f"{where_sql}"
            "),\n\n"
            "renamed as (\n\n"
            "    select\n"
            f"{projection}\n"
            "    from source\n\n"
            ")\n\n"
            "select * from renamed\n"
        )

    def _phase3(self, context: dict[str, Any]) -> AgentResult:
        del context
        cfg, brief, err = self._read_brief_and_cfg()
        if err:
            return err
        source = str(cfg.get("SOURCE_NAME") or "source").strip()
        in_scope = self._in_scope_tables(brief)
        specs = self._table_specs(in_scope)
        if not specs:
            return AgentResult(ok=False, reply="Phase 3 blocked: no in-scope tables were found.", phase=3)

        written: list[str] = []
        for spec in specs:
            table = str(spec["table"])
            model_name = f"stg_{self._normalize_model_name(table)}"
            path = f"models/staging/{source}/{model_name}.sql"
            sql = self._staging_sql(source, table, spec.get("columns") or [])
            w = self.tool("write_project_file", path=path, content=sql)
            if w.ok:
                written.append(path)

        return AgentResult(
            ok=bool(written),
            reply=f"Phase 3: wrote {len(written)} staging model(s) from scoped tables.",
            phase=3,
            artifacts={"staging": written},
        )

    def _best_ref(self, model_name: str, candidates: list[str]) -> str:
        target_tokens = set(model_name.split("_"))
        best = candidates[0] if candidates else ""
        best_score = -1
        for c in candidates:
            score = len(target_tokens & set(c.split("_")))
            if score > best_score:
                best = c
                best_score = score
        return best

    def _question_map_by_model(self, brief: str) -> dict[str, list[str]]:
        model_qs: dict[str, list[str]] = {}
        for raw in (brief or "").splitlines():
            line = raw.strip()
            if not line.startswith("|") or line.startswith("|---"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 5:
                continue
            question = cells[1] if len(cells) > 1 else ""
            if not question or question.lower() in {"business question", "question"}:
                continue
            for m in re.finditer(r"\b((?:stg|int|fct|dim|bridge)_[a-z0-9_]+)\b", line.lower()):
                model = m.group(1)
                bucket = model_qs.setdefault(model, [])
                if question not in bucket:
                    bucket.append(question)
        return model_qs

    def _strip_sql_fences(self, text: str) -> str:
        out = (text or "").strip()
        out = out.replace("```sql", "").replace("```", "").strip()
        return out

    def _refs_in_sql(self, sql: str) -> set[str]:
        return {m.group(1) for m in re.finditer(r"ref\('([^']+)'\)", sql or "", re.IGNORECASE)}

    def _build_domain_sql(
        self,
        *,
        layer: str,
        model_name: str,
        allowed_refs: list[str],
        questions: list[str],
        fallback_sql: str,
    ) -> str:
        system = load_prompt("system.md") + "\n" + load_prompt(f"phase_{'4' if layer == 'intermediate' else '5'}.md")
        refs_block = "\n".join(f"- {r}" for r in allowed_refs[:200])
        q_block = "\n".join(f"- {q}" for q in questions[:10]) if questions else "- (no specific question text found)"
        prompt = (
            f"Write dbt SQL for model `{model_name}` in layer `{layer}`.\n"
            "Return SQL only (no markdown).\n"
            "Rules:\n"
            "- Keep domain semantics from the business questions.\n"
            "- Use only these dbt refs as upstream inputs.\n"
            "- Use explicit select list where possible.\n"
            "- For marts: use intermediate refs when available.\n"
            "- Never use placeholder/example names.\n\n"
            "Business question context:\n"
            f"{q_block}\n\n"
            "Allowed upstream refs:\n"
            f"{refs_block}\n\n"
            "If context is insufficient, return a minimal valid model using one best ref.\n"
        )
        sql = self._strip_sql_fences(self.llm_fill(system, prompt, max_tokens=2200))
        if "select" not in (sql or "").lower():
            return fallback_sql
        refs = self._refs_in_sql(sql)
        allowed = set(allowed_refs)
        if refs and not refs.issubset(allowed):
            return fallback_sql
        if layer == "marts" and any(r.startswith("int_") for r in allowed) and not any(r.startswith("int_") for r in refs):
            return fallback_sql
        if "example" in sql.lower():
            return fallback_sql
        return sql

    def _phase4(self, context: dict[str, Any]) -> AgentResult:
        del context
        cfg, brief, err = self._read_brief_and_cfg()
        if err:
            return err
        source = str(cfg.get("SOURCE_NAME") or "source").strip()
        expected_int = self._extract_expected_models(brief, "int")
        if not expected_int:
            expected_int = ["int_model_enriched"]

        in_scope = self._in_scope_tables(brief)
        stg_candidates = [f"stg_{self._normalize_model_name(t)}" for t in in_scope]
        if not stg_candidates:
            stg_candidates = [f"stg_{source}_base"]
        qmap = self._question_map_by_model(brief)

        written: list[str] = []
        for name in expected_int:
            src = self._best_ref(name, stg_candidates)
            fallback_sql = (
                "select\n"
                "    *\n"
                f"from {{{{ ref('{src}') }}}}\n"
            )
            sql = self._build_domain_sql(
                layer="intermediate",
                model_name=name,
                allowed_refs=stg_candidates,
                questions=qmap.get(name, []),
                fallback_sql=fallback_sql,
            )
            path = f"models/intermediate/{name}.sql"
            w = self.tool("write_project_file", path=path, content=sql)
            if w.ok:
                written.append(path)

        return AgentResult(
            ok=bool(written),
            reply=f"Phase 4: wrote {len(written)} intermediate model(s) from brief plan.",
            phase=4,
            artifacts={"intermediate": written},
        )

    def _phase5(self, context: dict[str, Any]) -> AgentResult:
        del context
        cfg, brief, err = self._read_brief_and_cfg()
        if err:
            return err
        source = str(cfg.get("SOURCE_NAME") or "source").strip()
        int_models = self._extract_expected_models(brief, "int")
        stg_models = [f"stg_{self._normalize_model_name(t)}" for t in self._in_scope_tables(brief)]
        qmap = self._question_map_by_model(brief)

        mart_names: list[str] = []
        for pfx in ("fct", "dim", "bridge"):
            mart_names.extend(self._extract_expected_models(brief, pfx))
        if not mart_names:
            mart_names = ["fct_metrics", "dim_entities"]

        written: list[str] = []
        for name in mart_names:
            src_pool = int_models or stg_models or [f"stg_{source}_base"]
            src = self._best_ref(name, src_pool)
            fallback_sql = (
                "select\n"
                "    *\n"
                f"from {{{{ ref('{src}') }}}}\n"
            )
            sql = self._build_domain_sql(
                layer="marts",
                model_name=name,
                allowed_refs=src_pool,
                questions=qmap.get(name, []),
                fallback_sql=fallback_sql,
            )
            path = f"models/marts/{name}.sql"
            w = self.tool("write_project_file", path=path, content=sql)
            if w.ok:
                written.append(path)

        return AgentResult(
            ok=bool(written),
            reply=f"Phase 5: wrote {len(written)} mart model(s) from brief plan. Ready for QualityLoop Q5.",
            phase=5,
            artifacts={"marts": written},
        )

    def _rework(self, context: dict[str, Any]) -> AgentResult:
        del context
        findings = self.tool("read_project_file", path="QUALITY_FINDINGS.md")
        _, brief, err = self._read_brief_and_cfg()
        if err:
            return err
        system = load_prompt("system.md")
        plan = self.llm_fill(
            system,
            "You are fixing dbt models after QA failure. Propose concrete file edits.\n"
            f"FINDINGS:\n{findings.output[:6000]}\n\nBRIEF excerpt:\n{brief[:4000]}",
            max_tokens=2000,
        )
        return AgentResult(
            ok=True,
            reply=f"Modeling rework pass complete (review LLM plan and apply).\n\n{plan}",
            artifacts={"rework": True},
        )
