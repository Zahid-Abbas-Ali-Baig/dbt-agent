"""DiscoveryAgent — Phases 0–1 bootstrap + design brief draft."""

from __future__ import annotations

import json
import re
from typing import Any

from agents.base import AgentResult, BaseAgent, load_prompt
from logging_util import log_event
from tools.registry import DISCOVERY_TOOLS

PATH_CONFIDENCE_THRESHOLD = 70  # fallback default; intake overrides via context


class DiscoveryAgent(BaseAgent):
    name = "discovery"
    allowlist = DISCOVERY_TOOLS

    def run(self, phase: int, context: dict[str, Any]) -> AgentResult:
        if phase == 0:
            return self._phase0(context)
        if phase == 1:
            return self._phase1(context)
        return AgentResult(ok=False, reply=f"DiscoveryAgent does not handle phase {phase}")

    @staticmethod
    def _threshold_from_context(context: dict[str, Any] | None) -> int:
        ctx = context or {}
        raw = ctx.get("path_confidence_threshold", PATH_CONFIDENCE_THRESHOLD)
        try:
            return max(1, min(100, int(raw)))
        except (TypeError, ValueError):
            return PATH_CONFIDENCE_THRESHOLD

    @staticmethod
    def format_clarify_question(path_clarify: dict[str, Any]) -> str:
        """Progress header + current question body for PATH_CLARIFY intake."""
        questions = list(path_clarify.get("questions") or [])
        idx = int(path_clarify.get("index") or 0)
        total = len(questions)
        if total <= 0 or idx < 0 or idx >= total:
            return ""
        q = questions[idx] if isinstance(questions[idx], dict) else {}
        remaining = total - idx - 1
        header = f"Question {idx + 1} of {total} ({remaining} remaining)"
        body = str(q.get("question") or "").strip()
        return f"{header}\n\n{body}" if body else header

    def _phase0(self, context: dict[str, Any]) -> AgentResult:
        del context
        cfg_res = self.tool("parse_config")
        if not cfg_res.ok:
            return AgentResult(ok=False, reply=cfg_res.output)
        cfg = cfg_res.data or {}
        project_name = cfg.get("PROJECT_NAME", "my_project")
        if project_name.startswith("{{"):
            return AgentResult(
                ok=False,
                reply="config.md still has placeholders (PROJECT_NAME). Fill config.md before Phase 0.",
            )

        prompt = load_prompt("phase_0.md") or load_prompt("system.md")
        # Playbook: write bootstrap files from config
        dbt_project = f"""name: '{project_name}'
version: '1.0.0'
config-version: 2
profile: '{project_name}'

model-paths: ["models"]
analysis-paths: ["analyses"]
test-paths: ["tests"]
seed-paths: ["seeds"]
macro-paths: ["macros"]
snapshot-paths: ["snapshots"]
target-path: "target"
clean-targets: ["target", "dbt_packages"]

models:
  {project_name}:
    staging:
      +materialized: view
      +schema: {cfg.get('STAGING_SCHEMA', 'staging')}
    intermediate:
      +materialized: view
      +schema: {cfg.get('INTERMEDIATE_SCHEMA', 'intermediate')}
    marts:
      +materialized: table
      +schema: {cfg.get('MARTS_SCHEMA', 'marts')}
"""
        packages = """packages:
  - package: dbt-labs/codegen
    version: 0.14.0
  - package: dbt-labs/dbt_utils
    version: [">=1.0.0", "<2.0.0"]
"""
        from security_util import yaml_quote
        from tools.dbt_schemas import DEFAULT_PROFILE_SCHEMA, GENERATE_SCHEMA_NAME_SQL

        # Profile schema is only a fallback for models without +schema.
        # Layer names (staging / intermediate / marts) come from dbt_project.yml
        # + generate_schema_name — never put a layer name here or dbt may create
        # staging_marts / staging_staging.
        profile_schema = str(
            cfg.get("TARGET_PROFILE_SCHEMA") or DEFAULT_PROFILE_SCHEMA
        ).strip() or DEFAULT_PROFILE_SCHEMA

        profiles = f"""{project_name}:
  target: dev
  outputs:
    dev:
      type: {cfg.get('TARGET_WAREHOUSE_TYPE', cfg.get('WAREHOUSE_TYPE', 'postgres'))}
      host: {yaml_quote(str(cfg.get('TARGET_DB_HOST', cfg.get('DB_HOST', 'localhost'))))}
      user: {yaml_quote(str(cfg.get('TARGET_DB_USER', cfg.get('DB_USER', 'postgres'))))}
      password: {yaml_quote(str(cfg.get('TARGET_DB_PASSWORD', cfg.get('DB_PASSWORD', ''))))}
      port: {cfg.get('TARGET_DB_PORT', cfg.get('DB_PORT', '5432'))}
      dbname: {yaml_quote(str(cfg.get('TARGET_DATABASE_NAME', cfg.get('DATABASE_NAME', 'postgres'))))}
      schema: {yaml_quote(profile_schema)}
      threads: {cfg.get('TARGET_DB_THREADS', cfg.get('DB_THREADS', '4'))}
"""
        source = cfg.get("SOURCE_NAME", "main")
        for path, content in (
            ("dbt_project.yml", dbt_project),
            ("packages.yml", packages),
            ("profiles.yml", profiles),
            ("macros/generate_schema_name.sql", GENERATE_SCHEMA_NAME_SQL),
        ):
            w = self.tool("write_project_file", path=path, content=content)
            if not w.ok:
                return AgentResult(ok=False, reply=w.output)

        for folder in (
            f"models/staging/{source}",
            "models/intermediate",
            "models/marts",
            "models/semantic",
        ):
            # ensure via writing a .gitkeep
            self.tool("write_project_file", path=f"{folder}/.gitkeep", content="")

        deps = self.tool("dbt_cli", args=["deps"], confirmed=True)
        log_event("INFO", "phase0_deps", ok=deps.ok, output=deps.output[:500])

        note = self.llm_fill(
            prompt or "You are a dbt bootstrap agent.",
            f"Bootstrap complete for project {project_name}. Summarize what was created in 3 bullets.",
            max_tokens=300,
        )
        reply = (
            f"Project setup complete for `{project_name}`.\n"
            f"- Wrote dbt_project.yml, packages.yml, profiles.yml (target warehouse)\n"
            f"- Wrote macros/generate_schema_name.sql "
            f"(layers use staging / intermediate / marts as-is)\n"
            f"- Created models/staging/{source}, intermediate, marts, semantic\n"
            f"- dbt deps: {'OK' if deps.ok else 'FAILED — install dbt-postgres and retry'}\n\n"
            f"{note}"
        )
        return AgentResult(
            ok=True,
            reply=reply,
            phase=0,
            artifacts={"dbt_project": "dbt_project.yml", "deps_ok": deps.ok},
        )

    ARTIFACT_DIR = ".dbt_agent"

    def _phase1(self, context: dict[str, Any]) -> AgentResult:
        """List all tables → confirm scope → profile in-scope → clarify → brief."""
        context = context or {}
        mode = str(context.get("mode") or "discover")
        threshold = self._threshold_from_context(context)
        path_clarify = context.get("path_clarify")
        table_scope = context.get("table_scope")

        kit = self.tool("read_kit")
        if not kit.ok:
            return AgentResult(ok=False, reply=kit.output)
        files = (kit.data or {}).get("files") or {}
        cfg = (kit.data or {}).get("config") or {}
        requirements = files.get("requirements.md", "")

        # Resume: draft brief from disk artifacts (+ optional path answers)
        if mode == "brief":
            return self._phase1_write_brief_from_artifacts(
                cfg=cfg,
                requirements=requirements,
                path_clarify=path_clarify if isinstance(path_clarify, dict) else {},
                threshold=threshold,
            )

        # After human confirmed in-scope tables
        if mode == "after_scope" and isinstance(table_scope, dict):
            return self._phase1_after_scope(
                cfg=cfg,
                requirements=requirements,
                table_scope=table_scope,
                threshold=threshold,
                path_clarify=path_clarify if isinstance(path_clarify, dict) else None,
            )

        # Default: full inventory (no cap) → propose scope → pause for confirm
        return self._phase1_propose_scope(cfg, requirements, threshold)

    def _artifact_path(self, name: str) -> str:
        return f"{self.ARTIFACT_DIR}/{name}"

    def _write_artifact(self, name: str, content: str) -> None:
        self.tool("write_project_file", path=self._artifact_path(name), content=content)

    def _read_artifact(self, name: str) -> str:
        res = self.tool("read_project_file", path=self._artifact_path(name))
        return res.output if res.ok else ""

    def _phase1_propose_scope(
        self, cfg: dict[str, Any], requirements: str, threshold: int
    ) -> AgentResult:
        from tools.scope import propose_table_scope

        listed = self.tool("warehouse_discover_source", describe=False)
        if not listed.ok:
            return AgentResult(ok=False, reply=listed.output, phase=1)
        all_tables = list(
            (listed.data or {}).get("all_tables")
            or (listed.data or {}).get("tables")
            or []
        )
        via = ((listed.data or {}).get("via") if listed.ok else None) or "unknown"
        log_event(
            "INFO",
            "phase1_list_tables",
            ok=listed.ok,
            count=len(all_tables),
            via=via,
            agent=self.name,
        )

        from tools.scope import format_noise_summary, group_noise

        heuristic = propose_table_scope(all_tables, requirements)
        in_scope = list(heuristic.get("in_scope") or [])
        deferred = list(heuristic.get("deferred") or [])
        noise = list(heuristic.get("noise") or [])

        # Optional LLM refine (must stay within all_tables)
        known = set(all_tables)
        system = (
            load_prompt("system.md")
            + "\n\n"
            + load_prompt("phase_1_scope.md")
        )
        raw = self.llm_fill(
            system,
            "Propose in-scope analytics tables for this engagement.\n"
            "Explain deferred/noise with reasons — do not silently drop tables.\n\n"
            f"## requirements.md\n{requirements[:14000]}\n\n"
            f"## heuristic proposal ({len(in_scope)} in-scope / {len(deferred)} deferred)\n"
            + "\n".join(f"- {t}" for t in in_scope[:120])
            + "\n\n## heuristic noise groups (why deferred)\n"
            + "\n".join(
                f"- {g.get('reason')}: {g.get('count')} "
                f"({', '.join((g.get('examples') or [])[:5])})"
                for g in noise[:20]
            )
            + "\n\n## full schema table list\n"
            + "\n".join(f"- {t}" for t in all_tables),
            max_tokens=3000,
        )
        data = self._extract_json(raw)
        if data and isinstance(data.get("in_scope"), list):
            refined = [str(t).strip() for t in data["in_scope"] if str(t).strip() in known]
            if refined:
                in_scope = refined
                deferred = [t for t in all_tables if t not in set(in_scope)]
                noise = group_noise(deferred)
                # Prefer LLM noise_notes when present (merge reasons for display)
                llm_notes = data.get("noise_notes")
                if isinstance(llm_notes, list) and llm_notes:
                    merged: list[dict[str, Any]] = []
                    for note in llm_notes:
                        if not isinstance(note, dict):
                            continue
                        reason = str(note.get("reason") or "").strip()
                        tables = [
                            str(t).strip()
                            for t in (note.get("tables") or [])
                            if str(t).strip() in known and str(t).strip() in set(deferred)
                        ]
                        if reason and tables:
                            merged.append(
                                {
                                    "reason": reason,
                                    "count": len(tables),
                                    "examples": tables[:6],
                                    "tables": tables,
                                }
                            )
                    if merged:
                        noise = merged

        scope = {
            "all_tables": all_tables,
            "in_scope": in_scope,
            "deferred": deferred,
            "noise": noise,
            "via": via,
            "schema": cfg.get("SOURCE_SCHEMA_NAME") or cfg.get("SCHEMA_NAME"),
            "database": cfg.get("SOURCE_DATABASE_NAME") or cfg.get("DATABASE_NAME"),
            "confirmed": False,
            "threshold": threshold,
        }
        self._write_artifact(
            "all_tables.json",
            json.dumps({"tables": all_tables, "via": via}, indent=2),
        )
        self._write_artifact("scope_proposed.json", json.dumps(scope, indent=2))

        preview = "\n".join(f"- `{t}`" for t in in_scope[:60])
        more = "" if len(in_scope) <= 60 else f"\n- … and {len(in_scope) - 60} more"
        deferred_n = len(deferred)
        noise_block = format_noise_summary(noise)
        reply = (
            f"I listed {len(all_tables)} tables in the landed schema (no cap).\n"
            f"Proposed {len(in_scope)} in-scope for analytics; "
            f"{deferred_n} look like platform/noise — listed below with why, "
            f"so you can pull any back in.\n\n"
            f"In-scope preview:\n{preview}{more}\n\n"
        )
        if noise_block:
            reply += f"{noise_block}\n\n"
        reply += (
            "Reply yes to accept this list, or paste a revised table list "
            "(comma or newline separated names from the schema — include any "
            "deferred tables you want kept)."
        )
        return AgentResult(
            ok=True,
            reply=reply,
            phase=1,
            artifacts={
                "table_scope": scope,
                "discovery_via": via,
                "tables_listed": len(all_tables),
            },
        )

    def _phase1_after_scope(
        self,
        *,
        cfg: dict[str, Any],
        requirements: str,
        table_scope: dict[str, Any],
        threshold: int,
        path_clarify: dict[str, Any] | None,
    ) -> AgentResult:
        in_scope = [str(t).strip() for t in (table_scope.get("in_scope") or []) if str(t).strip()]
        if not in_scope:
            return AgentResult(
                ok=False,
                reply="In-scope table list is empty. Re-run Phase 1 and confirm a scope.",
                phase=1,
            )
        deferred = [str(t) for t in (table_scope.get("deferred") or [])]
        all_tables = list(table_scope.get("all_tables") or [])
        schema = cfg.get("SOURCE_SCHEMA_NAME") or cfg.get("SCHEMA_NAME", "")
        database = cfg.get("SOURCE_DATABASE_NAME") or cfg.get("DATABASE_NAME", "")

        # Describe + profile confirmed in-scope only (no table-count cap)
        disc = self.tool("warehouse_discover_source", tables=in_scope, describe=True)
        if not disc.ok:
            return AgentResult(ok=False, reply=disc.output, phase=1)
        via = ((disc.data or {}).get("via") if disc.ok else None) or "unknown"
        details = (disc.data or {}).get("details") or []
        discovery_out = disc.output or ""
        for d in details:
            discovery_out += f"\n\n### {d.get('table')}\n{d.get('detail', '')[:4000]}"

        codegen_out = ""
        if schema and not str(schema).startswith("{{"):
            args = [
                "run-operation",
                "generate_source",
                "--args",
                "{"
                + f'"schema_name": "{schema}", "database_name": "{database}", "generate_columns": true'
                + "}",
            ]
            gen = self.tool("dbt_cli", args=args, confirmed=True)
            codegen_out = gen.output or ""
            log_event("INFO", "phase1_codegen", ok=gen.ok, agent=self.name)
        else:
            codegen_out = "(SOURCE_SCHEMA_NAME not filled — skipped codegen)"

        prof = self.tool(
            "warehouse_profile_landed",
            tables=in_scope,
            details=details,
        )
        log_event(
            "INFO",
            "phase1_profile",
            ok=prof.ok,
            tables=len(in_scope),
            agent=self.name,
        )
        profile_out = prof.output if prof.ok else f"(profiling failed: {prof.output})"

        # Persist artifacts for brief drafting (disk truth)
        scope_confirmed = {
            **table_scope,
            "in_scope": in_scope,
            "deferred": deferred or [t for t in all_tables if t not in set(in_scope)],
            "confirmed": True,
            "via": via,
        }
        self._write_artifact("scope.json", json.dumps(scope_confirmed, indent=2))
        self._write_artifact(
            "discovery_inscope.md",
            discovery_out,
        )
        self._write_artifact("profiles.md", profile_out)
        self._write_artifact(
            "codegen_excerpt.md",
            codegen_out[:200000],
        )
        self._write_artifact(
            "discovery_details.json",
            json.dumps({"via": via, "details": details, "tables": in_scope}, indent=2),
        )

        # If resuming with path answers already complete, go straight to brief
        if isinstance(path_clarify, dict) and path_clarify.get("answers") is not None:
            qs = path_clarify.get("questions") or []
            ans = path_clarify.get("answers") or {}
            if qs and len(ans) >= len(qs):
                return self._phase1_write_brief_from_artifacts(
                    cfg=cfg,
                    requirements=requirements,
                    path_clarify=path_clarify,
                    threshold=threshold,
                )

        assessment = self._assess_paths(
            cfg,
            requirements,
            discovery_out,
            profile_out,
            codegen_out,
            threshold=threshold,
            in_scope=in_scope,
        )
        paths = assessment.get("paths") or []
        questions = assessment.get("questions") or []
        self._write_artifact(
            "paths.json",
            json.dumps({"paths": paths, "questions": questions, "threshold": threshold}, indent=2),
        )
        log_event(
            "INFO",
            "phase1_path_assess",
            paths=len(paths),
            questions=len(questions),
            threshold=threshold,
            agent=self.name,
        )

        if questions:
            clarify = {
                "paths": paths,
                "questions": questions,
                "index": 0,
                "answers": {},
                "via": via,
                "threshold": threshold,
                "in_scope": in_scope,
            }
            low = [
                p
                for p in paths
                if isinstance(p, dict) and int(p.get("confidence") or 0) < threshold
            ]
            summary_lines = []
            for p in (low or paths)[:12]:
                if not isinstance(p, dict):
                    continue
                need = str(p.get("need") or "?").strip()
                conf = int(p.get("confidence") or 0)
                tables = ", ".join(str(t) for t in (p.get("tables") or [])[:6])
                summary_lines.append(f"- {need} ({conf}%): {tables or 'no tables yet'}")
            intro = (
                f"Scoped {len(in_scope)} tables and profiled them. "
                f"{len(questions)} path"
                f"{'s need' if len(questions) != 1 else ' needs'} your call "
                f"(under {threshold}% confidence) before I write the brief.\n\n"
            )
            if summary_lines:
                intro += "Low-confidence paths:\n" + "\n".join(summary_lines) + "\n\n"
            intro += self.format_clarify_question(clarify)
            return AgentResult(
                ok=True,
                reply=intro,
                phase=1,
                artifacts={
                    "path_clarify": clarify,
                    "table_scope": scope_confirmed,
                    "discovery_via": via,
                    "paths_assessed": len(paths),
                    "path_confidence_threshold": threshold,
                },
            )

        return self._phase1_write_brief_from_artifacts(
            cfg=cfg,
            requirements=requirements,
            path_clarify={"paths": paths, "questions": [], "answers": {}},
            threshold=threshold,
        )

    def _assess_paths(
        self,
        cfg: dict[str, Any],
        requirements: str,
        discovery_out: str,
        profile_out: str,
        codegen_out: str,
        *,
        threshold: int = PATH_CONFIDENCE_THRESHOLD,
        in_scope: list[str] | None = None,
    ) -> dict[str, Any]:
        # be-human is injected by skills_runtime for every llm_fill
        system = (
            load_prompt("system.md")
            + "\n\n"
            + load_prompt("phase_1_assess.md")
        )
        scope_block = "\n".join(f"- {t}" for t in (in_scope or [])[:200])
        user = (
            f"Map requirements to landed data paths. Confidence threshold for questions: "
            f"{threshold}.\n"
            "Only use tables from the in-scope list.\n\n"
            f"## in-scope tables\n{scope_block}\n\n"
            f"## config\n{cfg}\n\n## requirements.md\n{requirements[:12000]}\n\n"
            f"## discovery\n{discovery_out[:50000]}\n\n"
            f"## profiles\n{profile_out[:50000]}\n\n"
            f"## codegen\n{codegen_out[:20000]}\n"
        )
        raw = self.llm_fill(system, user, max_tokens=4000)
        data = self._extract_json(raw)
        if not data:
            log_event("WARN", "phase1_assess_parse_failed", agent=self.name)
            return {"paths": [], "questions": []}

        known = set(in_scope or [])
        paths_raw = data.get("paths") if isinstance(data.get("paths"), list) else []
        questions_raw = data.get("questions") if isinstance(data.get("questions"), list) else []

        paths: list[dict[str, Any]] = []
        for i, p in enumerate(paths_raw):
            if not isinstance(p, dict):
                continue
            try:
                conf = int(p.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0
            conf = max(0, min(100, conf))
            tables = p.get("tables") if isinstance(p.get("tables"), list) else []
            clean_tables = [str(t) for t in tables if not known or str(t) in known]
            paths.append(
                {
                    "need": str(p.get("need") or "").strip() or f"need_{i + 1}",
                    "tables": clean_tables[:20],
                    "join_path": str(p.get("join_path") or "").strip(),
                    "confidence": conf,
                    "rationale": str(p.get("rationale") or "").strip(),
                }
            )

        questions: list[dict[str, Any]] = []
        seen_needs: set[str] = set()
        for i, q in enumerate(questions_raw):
            if not isinstance(q, dict):
                continue
            text = str(q.get("question") or "").strip()
            if not text:
                continue
            need = str(q.get("need") or "").strip()
            try:
                conf = int(q.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0
            qid = str(q.get("id") or f"q{i + 1}").strip() or f"q{i + 1}"
            questions.append(
                {
                    "id": qid,
                    "need": need,
                    "question": text,
                    "confidence": conf,
                }
            )
            if need:
                seen_needs.add(need.lower())

        if not questions:
            for p in paths:
                if p["confidence"] >= threshold:
                    continue
                need = p["need"]
                if need.lower() in seen_needs:
                    continue
                tables = ", ".join(p["tables"][:4]) or "the landed tables"
                questions.append(
                    {
                        "id": f"q{len(questions) + 1}",
                        "need": need,
                        "question": (
                            f"For \"{need}\", I only have about {p['confidence']}% confidence "
                            f"using {tables}. Which tables or filters should define that measure?"
                        ),
                        "confidence": p["confidence"],
                    }
                )
                seen_needs.add(need.lower())
                if len(questions) >= 8:
                    break

        return {"paths": paths, "questions": questions[:8]}

    def _phase1_write_brief_from_artifacts(
        self,
        *,
        cfg: dict[str, Any],
        requirements: str,
        path_clarify: dict[str, Any],
        threshold: int,
    ) -> AgentResult:
        discovery_out = self._read_artifact("discovery_inscope.md")
        profile_out = self._read_artifact("profiles.md")
        codegen_out = self._read_artifact("codegen_excerpt.md")
        scope_raw = self._read_artifact("scope.json")
        try:
            scope = json.loads(scope_raw) if scope_raw else {}
        except json.JSONDecodeError:
            scope = {}
        in_scope = list(scope.get("in_scope") or [])
        deferred = list(scope.get("deferred") or [])
        via = str(scope.get("via") or "unknown")
        schema = scope.get("schema") or cfg.get("SOURCE_SCHEMA_NAME") or cfg.get("SCHEMA_NAME")
        database = scope.get("database") or cfg.get("SOURCE_DATABASE_NAME") or cfg.get("DATABASE_NAME")

        if not discovery_out and not profile_out:
            return AgentResult(
                ok=False,
                reply=(
                    "Missing discovery artifacts under `.dbt_agent/`. "
                    "Re-run Phase 1 so I can list tables, confirm scope, and profile again."
                ),
                phase=1,
            )

        paths = path_clarify.get("paths") or []
        answers = path_clarify.get("answers") or {}
        questions = path_clarify.get("questions") or []
        path_block = json.dumps(
            {"paths": paths, "questions": questions, "answers": answers, "threshold": threshold},
            indent=2,
        )
        scope_block = json.dumps(
            {"in_scope": in_scope, "deferred_count": len(deferred), "deferred_sample": deferred[:40]},
            indent=2,
        )

        prompt = load_prompt("phase_1.md")
        system = load_prompt("system.md") + "\n\n" + prompt

        # Pass A: domain + inventory + viability
        user_a = (
            "Write PART A of the Design Brief ONLY: title, Status pending approval, "
            "sections ## 1, ## 1.5, ## 2, ## 3, ## 3.5.\n"
            "No chat narration. Use only artifacts below.\n\n"
            f"## config\n{cfg}\n\n## requirements.md\n{requirements[:14000]}\n\n"
            f"## confirmed scope\n{scope_block}\n\n"
            f"## path assessment + human clarifications\n{path_block}\n\n"
            f"## discovery (in-scope)\n{discovery_out[:60000]}\n\n"
            f"## profiles\n{profile_out[:60000]}\n"
        )
        raw_a = self.llm_fill(system, user_a, max_tokens=8000)
        part_a = self._coerce_brief_markdown(raw_a)
        llm_fail_a = (raw_a or "").startswith("[LLM unavailable")
        if not part_a:
            # One hard retry for format
            raw_a2 = self.llm_fill(
                system,
                "OUTPUT ONLY markdown starting with `# Design Brief`.\n"
                "No preamble. Include ## 1 through ## 3.5.\n\n" + user_a[:50000],
                max_tokens=8000,
            )
            llm_fail_a = llm_fail_a or (raw_a2 or "").startswith("[LLM unavailable")
            part_a = self._coerce_brief_markdown(raw_a2)
            if not part_a:
                raw_a = raw_a2 or raw_a

        # Pass B: relationships through work batches
        user_b = (
            "Write PART B of the Design Brief ONLY: sections ## 4 through ## 10 "
            "(relationship graph, standardization, staging, intermediate, marts/BI, "
            "semantics, work batches).\n"
            "Do not repeat sections 1–3.5. No chat narration.\n"
            "Start at `## 4.` — do not include `# Design Brief` again.\n\n"
            f"## PART A already written\n{(part_a or '')[:20000]}\n\n"
            f"## path assessment + human clarifications\n{path_block}\n\n"
            f"## discovery\n{discovery_out[:40000]}\n\n"
            f"## profiles\n{profile_out[:40000]}\n\n"
            f"## codegen\n{codegen_out[:25000]}\n"
        )
        raw_b = self.llm_fill(system, user_b, max_tokens=8000) if part_a else ""
        part_b = ""
        if part_a and raw_b and not (raw_b or "").startswith("[LLM unavailable"):
            # B is a section fragment — accept even without # Design Brief title
            part_b = self._strip_md_fences(raw_b).strip()
            # Drop accidental second title / status
            lines = part_b.splitlines()
            while lines and (
                lines[0].strip().lower().startswith("# design brief")
                or lines[0].strip().lower().startswith("**status")
                or not lines[0].strip()
            ):
                lines.pop(0)
            part_b = "\n".join(lines).strip()

        draft = self._merge_brief_parts(part_a or "", part_b)
        draft = self._coerce_brief_markdown(draft) or draft

        # Salvage: keep a good Part A and append stub §4–§10 instead of wiping everything
        if part_a and not self._looks_like_brief(draft):
            draft = self._merge_brief_parts(
                part_a,
                self._stub_brief_tail(cfg, requirements, discovery_out, profile_out),
            )
            draft = self._coerce_brief_markdown(draft) or draft

        if not self._looks_like_brief(draft):
            log_event(
                "WARN",
                "phase1_brief_not_document",
                agent=self.name,
                llm_fail=llm_fail_a,
                preview=(raw_a or "")[:240],
            )
            draft = self._stub_brief(cfg, requirements, discovery_out, profile_out)
            # Fill inventory from real in-scope names
            inv_rows = "\n".join(f"| `{t}` | | | | |" for t in in_scope[:80])
            draft = draft.replace(
                "| *(from discovery)* | | | |",
                inv_rows or "| *(none)* | | | |",
            )
            brief_name = cfg.get("DESIGN_BRIEF_DOC", "design_brief.md")
            self.tool("write_project_file", path=brief_name, content=draft)
            why = (
                "Cursor brain returned an error"
                if llm_fail_a
                else "model output was chat/narration or missing `# Design Brief` / §1 / §3"
            )
            preview = (raw_a or "").strip().replace("\n", " ")[:180]
            return AgentResult(
                ok=False,
                reply=(
                    f"I could not produce a valid Design Brief ({why}), "
                    f"so I wrote a stub skeleton to `{brief_name}` with the confirmed "
                    f"{len(in_scope)} in-scope tables listed.\n"
                    f"Model preview: {preview or '(empty)'}\n"
                    "Check CURSOR_API_KEY / model in brain settings, then re-run Phase 1 "
                    "(or edit the stub and approve when ready)."
                ),
                phase=1,
                artifacts={
                    "design_brief": brief_name,
                    "brief_ok": False,
                    "brief_fail_reason": why,
                },
            )

        if "Status:" not in draft and "**Status:**" not in draft:
            draft = draft.replace(
                "# Design Brief",
                "# Design Brief\n\n**Status:** pending approval\n",
                1,
            )

        missing = self._brief_missing_sections(draft)
        if missing:
            log_event("INFO", "phase1_brief_incomplete", missing=missing, agent=self.name)
            repair = self.llm_fill(
                system,
                "Return the FULL Design Brief markdown only (start with # Design Brief). "
                f"Fill these missing pieces: {', '.join(missing)}.\n"
                "Keep Status: pending approval. Do not invent tables or row counts.\n\n"
                f"## current draft\n{draft[:20000]}\n\n"
                f"## scope\n{scope_block}\n\n"
                f"## profiles\n{profile_out[:30000]}\n",
                max_tokens=10000,
            )
            if self._looks_like_brief(repair):
                draft = repair
            missing = self._brief_missing_sections(draft)

        # Never append stub soup under chat — if still broken, replace with stub
        if missing and not self._looks_like_brief(draft):
            draft = self._stub_brief(cfg, requirements, discovery_out, profile_out)
            missing = self._brief_missing_sections(draft)

        brief_name = cfg.get("DESIGN_BRIEF_DOC", "design_brief.md")
        w = self.tool("write_project_file", path=brief_name, content=draft)
        if not w.ok:
            return AgentResult(ok=False, reply=w.output)

        log_event("INFO", "phase1_brief_written", path=brief_name, agent=self.name)
        note = ""
        if missing:
            note = f"\nStill thin/missing: {', '.join(missing)}. Review carefully before approving."
        return AgentResult(
            ok=True,
            reply=(
                f"Design brief ready (`{brief_name}`), waiting for your approval.\n"
                f"In-scope tables: {len(in_scope)}; deferred: {len(deferred)}; "
                f"discovery via `{via}`.\n"
                "Review especially §2, §3.5, §8, §10, then reply yes to approve "
                "so modeling can continue. No staging/marts until Status: approved."
                + note
            ),
            phase=1,
            artifacts={
                "design_brief": brief_name,
                "discovery_via": via,
                "in_scope_count": len(in_scope),
                "paths_assessed": len(paths),
                "clarifications": len(answers),
                "missing_sections": missing,
            },
        )

    @staticmethod
    def _strip_md_fences(text: str) -> str:
        raw = (text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:markdown|md)?\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*```$", "", raw)
        return raw.strip()

    @classmethod
    def _coerce_brief_markdown(cls, text: str) -> str | None:
        """Pull a Design Brief document out of model output (drop chat preamble / fences)."""
        raw = (text or "").strip()
        if not raw or raw.startswith("[LLM unavailable"):
            return None
        raw = cls._strip_md_fences(raw)
        # Prefer content from the Design Brief title onward
        m = re.search(r"(?im)^#\s+design brief\b[^\n]*$", raw)
        if m:
            raw = raw[m.start() :].strip()
        if not cls._looks_like_brief(raw):
            return None
        return raw if raw.endswith("\n") else raw + "\n"

    @staticmethod
    def _merge_brief_parts(part_a: str, part_b: str) -> str:
        a = DiscoveryAgent._strip_md_fences(part_a or "").strip()
        b = DiscoveryAgent._strip_md_fences(part_b or "").strip()
        if not b:
            return a + ("\n" if a and not a.endswith("\n") else "")
        # If B accidentally repeats title, drop leading title / status lines
        lines = b.splitlines()
        while lines and (
            lines[0].strip().lower().startswith("# design brief")
            or lines[0].strip().lower().startswith("**status")
            or not lines[0].strip()
        ):
            if lines[0].strip().startswith("## "):
                break
            lines.pop(0)
        b = "\n".join(lines).strip()
        return (a.rstrip() + "\n\n" + b).strip() + "\n"

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        raw = (text or "").strip()
        if not raw or raw.startswith("[LLM unavailable"):
            return None
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*```$", "", raw)
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(raw[start : end + 1])
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
        return None

    @staticmethod
    def _looks_like_brief(draft: str) -> bool:
        text = (draft or "").strip()
        if not text or text.startswith("[LLM unavailable"):
            return False
        low = text.lower()
        # Only reject chat narration in the preamble (before the brief title)
        title_idx = low.find("# design brief")
        preamble = low[:title_idx] if title_idx >= 0 else low[:400]
        chat_tells = (
            "checking discovery",
            "updating the design brief",
            "i will now",
            "let me ",
            "here's what i",
            "agent fill for missing",
        )
        if title_idx < 0 and any(t in preamble for t in chat_tells):
            return False
        if title_idx < 0:
            return False
        body = low[title_idx:]
        has_s1 = "## 1." in body or "## 1 " in body
        has_s3 = "## 3." in body or "## 3 " in body
        return bool(has_s1 and has_s3)

    def _stub_brief_tail(
        self,
        cfg: dict,
        requirements: str,
        discovery_out: str = "",
        profile_out: str = "",
    ) -> str:
        """§4–§10 only — used when Part A succeeded but Part B did not."""
        stub = self._stub_brief(cfg, requirements, discovery_out, profile_out)
        idx = stub.lower().find("## 4.")
        return stub[idx:] if idx >= 0 else stub

    @staticmethod
    def _brief_missing_sections(draft: str) -> list[str]:
        text = draft or ""
        low = text.lower()
        checks = [
            ("§1 Domain Summary", ("## 1.", "domain summary")),
            ("§1.5 Industry", ("## 1.5", "industry reporting")),
            ("§2 KPI Map", ("## 2.", "business questions")),
            ("§3 Source Inventory", ("## 3.", "source inventory")),
            ("§3.5 Viability", ("## 3.5", "landed-data viability", "kpi attribute")),
            ("§4 Relationships", ("## 4.", "relationship graph")),
            ("§5 Standardization", ("## 5.", "column standardization")),
            ("§6 Staging List", ("## 6.", "staging model")),
            ("§7 Intermediate", ("## 7.", "relationship resolution")),
            ("§8 Marts / BI", ("## 8.", "mart star", "power bi relationships")),
            ("§8 BI date", ("bi date strategy", "conformed_dim", "fact_native")),
            ("§9 Semantics", ("## 9.", "semantic metrics")),
            ("§10 Work Batches", ("## 10.", "work batches")),
        ]
        missing: list[str] = []
        for label, needles in checks:
            if not any(n in low for n in needles):
                missing.append(label)
        if "work batches" not in low and "open questions" in low:
            if "§10 Work Batches" not in missing:
                missing.append("§10 Work Batches")
        if len(text.strip()) < 1200:
            missing.append("(brief too thin)")
        return missing

    def _stub_brief(
        self,
        cfg: dict,
        requirements: str,
        discovery_out: str = "",
        profile_out: str = "",
    ) -> str:
        tables_hint = ""
        for line in (discovery_out or "").splitlines():
            if line.strip().startswith("- "):
                tables_hint += line.strip() + "\n"
        return f"""# Design Brief — {cfg.get('PROJECT_NAME', 'project')}

**Status:** pending approval

> Stub brief (LLM unavailable or incomplete draft). Edit before approving.

## 1. Domain Summary

See requirements.md.

{requirements[:2000]}

## 1.5 Industry reporting pattern

*(Infer industry from Domain Summary + landed entities below. List subject areas, typical pages, KPI families; map only to requirements + discovered tables.)*

Landed tables (from discovery):
{tables_hint or '*(none discovered)*'}

## 2. Business Questions → KPI Map

| Business Question | Proposed KPI / Metric | Target Grain |
| ----------------- | --------------------- | ------------ |
| *(from requirements)* | | |

## 3. Source Inventory

- Database: {cfg.get('SOURCE_DATABASE_NAME') or cfg.get('DATABASE_NAME')}
- Schema: {cfg.get('SOURCE_SCHEMA_NAME') or cfg.get('SCHEMA_NAME')}
- Source name: {cfg.get('SOURCE_NAME')}

| Raw Table | Row Grain | PK Column(s) | Classification (fact / dim / bridge) |
| --------- | --------- | ------------ | ------------------------------------ |
| *(from discovery)* | | | |

## 3.5 KPI Attribute Sourcing & Landed-Data Viability

| Business Question | Breakdown Attributes | KPI Grain | Candidate Path | Landed-Data Stats | Chosen Primary Path | Fallback | BI Viability |
| ----------------- | -------------------- | --------- | -------------- | ----------------- | ------------------- | -------- | ------------ |
| | | | | see profiles | | | |

### Profile excerpt

```
{(profile_out or '(no profiles)')[:4000]}
```

## 4. Relationship Graph

| From Table | To Table | Join Key | Cardinality | Orphan Count |
| ---------- | -------- | -------- | ----------- | ------------ |
| | | | | *(from profiles when available)* |

## 5. Column Standardization Plan

| Source Table | Source Column | Staging Column | Transformation |
| ------------ | ------------- | -------------- | -------------- |
| | *(char FKs)* | | nullif(trim(col),'') |

## 6. Staging Model List

| Staging Model | Source Table | Notes |
| ------------- | ------------ | ----- |
| | | |

## 7. Relationship Resolution Plan (Intermediate)

| Intermediate Model | Type | Inputs | Join Keys | Output Grain |
| ------------------ | ---- | ------ | --------- | ------------ |
| | | | | |

## 8. Mart Star Schema

| Mart Model | Type | Primary Intermediate Input(s) | Grain | Subject Area Folder |
| ---------- | ---- | ----------------------------- | ----- | ------------------- |
| | | | | |

### BI date strategy (Power BI)

| Setting | Value |
| ------- | ----- |
| Conformed date dim | |
| Strategy | conformed_dim |
| Conformed dim key column | |
| Fact date role columns | |

### Power BI relationships (agent Phase 8 wiring)

| From (fact) | To (dim) | From column | To column | Active | Notes |
| ----------- | -------- | ----------- | --------- | ------ | ----- |
| | | | | | |

### Dim BI role inventory

| Dim mart | BI role | Connected by (fact → dim rows) |
| -------- | ------- | ------------------------------ |
| | | |

## 9. Semantic Metrics List

ENABLE_SEMANTIC_LAYER={cfg.get('ENABLE_SEMANTIC_LAYER')}

| Metric Name | Type | Base Mart Model | Measure / Formula | Filter |
| ----------- | ---- | --------------- | ----------------- | ------ |
| | | | | |

## 10. Work Batches (max 3 tables per codegen call)

| Batch | Tables | Phase |
| ----- | ------ | ----- |
| 1 | *(fill from inventory)* | sources |
"""
