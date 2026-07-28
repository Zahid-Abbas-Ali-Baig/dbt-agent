"""Quality check helpers — machine evidence for QualityLoop."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tools.dbt_cli import dbt_cli
from tools.files import list_project_files, read_project_file
from tools.pbip import parse_pbip_relationships
from tools.registry import ToolResult


def _extract_expected_models_from_brief(text: str) -> dict[str, set[str]]:
    low = (text or "").lower()
    out = {"staging": set(), "intermediate": set(), "marts": set()}
    for m in re.finditer(r"\b(stg_[a-z0-9_]+)\b", low):
        out["staging"].add(m.group(1))
    for m in re.finditer(r"\b(int_[a-z0-9_]+)\b", low):
        out["intermediate"].add(m.group(1))
    for m in re.finditer(r"\b((?:fct|dim|bridge)_[a-z0-9_]+)\b", low):
        out["marts"].add(m.group(1))
    return out


def _stems(paths: list[str]) -> set[str]:
    stems: set[str] = set()
    for p in paths:
        name = Path(str(p)).name
        if name.endswith(".sql"):
            stems.add(name[:-4].lower())
    return stems


def _model_sql_map(project_dir: Path, rel_paths: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in rel_paths:
        r = read_project_file(project_dir, rel)
        if not r.ok:
            continue
        stem = Path(str(rel)).name
        if stem.endswith(".sql"):
            out[stem[:-4].lower()] = r.output or ""
    return out


def _domain_families_from_scope(project_dir: Path) -> set[str]:
    p = project_dir / ".dbt_agent" / "scope.json"
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    tables = [str(t).lower() for t in (data.get("in_scope") or []) if str(t).strip()]
    families: set[str] = set()
    lexicon = {
        "payment": ("payment", "invoice", "transaction", "refund", "credit_note"),
        "subscription": ("subscription", "mrr", "churn", "upgrade"),
        "order": ("order", "delivery", "fulfillment"),
        "customer": ("customer", "account", "corporate", "nafath", "otp"),
        "partner": ("partner", "program", "webhook", "api_log"),
        "device": ("device", "sku"),
    }
    for t in tables:
        for fam, tokens in lexicon.items():
            if any(tok in t for tok in tokens):
                families.add(fam)
    return families


def _domain_keyword_coverage(
    family: str,
    models: set[str],
    sql_map: dict[str, str],
) -> bool:
    tokens = {
        "payment": ("payment", "invoice", "transaction", "refund"),
        "subscription": ("subscription", "mrr", "churn", "upgrade"),
        "order": ("order", "delivery", "fulfillment"),
        "customer": ("customer", "account", "corporate", "verified"),
        "partner": ("partner", "program", "webhook"),
        "device": ("device", "sku"),
    }.get(family, ())
    if not tokens:
        return True
    for m in models:
        if any(tok in m for tok in tokens):
            return True
        body = (sql_map.get(m) or "").lower()
        if any(tok in body for tok in tokens):
            return True
    return False


def write_quality_findings(
    project_dir: Path,
    checkpoint: str,
    status: str,
    rework_target: str | None,
    failures: list[dict],
    suggested_fix: str = "",
) -> ToolResult:
    lines = [
        f"# Quality findings — {checkpoint}",
        f"Status: {status}",
        f"Rework target: {rework_target or 'none'}",
        f"Engagement: {project_dir}",
        "",
        "## Failures",
    ]
    if not failures:
        lines.append("- (none)")
    for f in failures:
        lines.append(f"- id: {f.get('id', 'n/a')}")
        lines.append(f"  check: {f.get('check', '')}")
        lines.append(f"  evidence: {f.get('evidence', '')}")
        lines.append(f"  brief_ref: {f.get('brief_ref', '')}")
    lines.append("")
    lines.append("## Suggested fix direction")
    lines.append(f"- {suggested_fix or 'See failures above.'}")
    lines.append("")
    path = project_dir / "QUALITY_FINDINGS.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return ToolResult(
        ok=True,
        output=f"Wrote {path.name}",
        data={"path": str(path), "status": status, "rework_target": rework_target},
    )


def audit_brief_vs_marts(project_dir: Path) -> ToolResult:
    """Q5 structural + completeness checks for phases 2..5 output."""
    failures: list[dict] = []
    brief_text = ""
    brief = project_dir / "design_brief.md"
    if not brief.exists():
        failures.append(
            {
                "id": "missing_brief",
                "check": "design_brief.md present",
                "evidence": "file missing",
                "brief_ref": "all",
            }
        )
    else:
        brief_text = brief.read_text(encoding="utf-8")
        if "Business Questions" not in brief_text and "KPI" not in brief_text:
            failures.append(
                {
                    "id": "brief_kpi_section",
                    "check": "brief has KPI / business questions section",
                    "evidence": "no KPI/Business Questions heading found",
                    "brief_ref": "§2",
                }
            )
        if (
            "3.5" not in brief_text
            and "Landed-Data Viability" not in brief_text
            and "viability" not in brief_text.lower()
        ):
            failures.append(
                {
                    "id": "brief_35",
                    "check": "brief documents §3.5 viability (or equivalent)",
                    "evidence": "§3.5 / viability section not found",
                    "brief_ref": "§3.5",
                }
            )
        if "Power BI relationships" not in brief_text and "relationships" not in brief_text.lower():
            failures.append(
                {
                    "id": "brief_pbi_rels",
                    "check": "brief documents Power BI relationships matrix",
                    "evidence": "no Power BI relationships section",
                    "brief_ref": "§8",
                }
            )

    stg = list_project_files(project_dir, "models/staging/**/*.sql")
    stg_files = (stg.data or {}).get("files") or []
    intm = list_project_files(project_dir, "models/intermediate/**/*.sql")
    int_files = (intm.data or {}).get("files") or []
    marts = list_project_files(project_dir, "models/marts/**/*.sql")
    mart_files = (marts.data or {}).get("files") or []

    if not stg_files:
        failures.append(
            {
                "id": "no_staging",
                "check": "staging SQL models exist",
                "evidence": "models/staging/**/*.sql empty",
                "brief_ref": "§6",
            }
        )
    source_defs = list_project_files(project_dir, "models/staging/**/_sources.yml")
    source_files = (source_defs.data or {}).get("files") or []
    if not source_files:
        failures.append(
            {
                "id": "missing_sources_yml",
                "check": "staging source definitions exist",
                "evidence": "models/staging/**/_sources.yml not found",
                "brief_ref": "§3",
            }
        )
    if not int_files:
        failures.append(
            {
                "id": "no_intermediate",
                "check": "intermediate SQL models exist",
                "evidence": "models/intermediate/**/*.sql empty",
                "brief_ref": "§7",
            }
        )
    if not mart_files:
        failures.append(
            {
                "id": "no_marts",
                "check": "mart SQL models exist",
                "evidence": "models/marts/**/*.sql empty",
                "brief_ref": "§8",
            }
        )

    expected = _extract_expected_models_from_brief(brief_text)
    actual_stg = _stems(stg_files)
    actual_int = _stems(int_files)
    actual_marts = _stems(mart_files)
    for layer, expected_names, actual_names, ref in (
        ("staging", expected["staging"], actual_stg, "§6"),
        ("intermediate", expected["intermediate"], actual_int, "§7"),
        ("marts", expected["marts"], actual_marts, "§8"),
    ):
        missing = sorted(expected_names - actual_names)
        if missing:
            preview = ", ".join(missing[:15])
            more = "" if len(missing) <= 15 else f" (+{len(missing)-15} more)"
            failures.append(
                {
                    "id": f"missing_{layer}_models",
                    "check": f"{layer} models from brief are implemented",
                    "evidence": f"missing: {preview}{more}",
                    "brief_ref": ref,
                }
            )

    # Reject placeholder/example scaffold names.
    bad_placeholders = [
        s for s in (actual_stg | actual_int | actual_marts) if any(t in s for t in ("example", "_base", "_metrics"))
    ]
    if bad_placeholders:
        failures.append(
            {
                "id": "placeholder_models",
                "check": "model names are production-ready (no example scaffolds)",
                "evidence": ", ".join(sorted(bad_placeholders)[:20]),
                "brief_ref": "§6-§8",
            }
        )

    # Ensure marts are layered on intermediate (or at minimum ref another model).
    for rel in mart_files:
        r = read_project_file(project_dir, rel)
        if not r.ok:
            continue
        refs = re.findall(r"ref\('([^']+)'\)", r.output or "", re.IGNORECASE)
        if not refs:
            failures.append(
                {
                    "id": f"mart_no_ref:{rel}",
                    "check": "mart models use ref() and are layered",
                    "evidence": f"no ref() call in {rel}",
                    "brief_ref": "§8",
                }
            )
            continue
        if int_files and not any(x.startswith("int_") for x in refs):
            failures.append(
                {
                    "id": f"mart_without_intermediate:{rel}",
                    "check": "mart models should depend on intermediate layer",
                    "evidence": f"refs={refs}",
                    "brief_ref": "§8",
                }
            )
        if re.search(r"\bsource\(", r.output or "", re.IGNORECASE):
            failures.append(
                {
                    "id": f"mart_source_access:{rel}",
                    "check": "marts should not read raw sources directly",
                    "evidence": f"source() found in {rel}",
                    "brief_ref": "§8",
                }
            )

    # Staging must not contain obvious joins (heuristic)
    for rel in stg_files:
        r = read_project_file(project_dir, rel)
        if r.ok and not re.search(r"\bsource\(", r.output, re.IGNORECASE):
            failures.append(
                {
                    "id": f"staging_missing_source:{rel}",
                    "check": "staging models should read from source()",
                    "evidence": f"no source() call in {rel}",
                    "brief_ref": "§6",
                }
            )
        if r.ok and re.search(r"\bjoin\b", r.output, re.IGNORECASE):
            failures.append(
                {
                    "id": f"staging_join:{rel}",
                    "check": "no joins in staging",
                    "evidence": f"JOIN found in {rel}",
                    "brief_ref": "layer rules",
                }
            )
    for rel in int_files:
        r = read_project_file(project_dir, rel)
        if not r.ok:
            continue
        if re.search(r"\bsource\(", r.output, re.IGNORECASE):
            failures.append(
                {
                    "id": f"intermediate_source_access:{rel}",
                    "check": "intermediate models should read from ref(staging), not source()",
                    "evidence": f"source() found in {rel}",
                    "brief_ref": "§7",
                }
            )

    # Domain-level QA: ensure modeled subject areas are represented across layers.
    domain_families = _domain_families_from_scope(project_dir)
    all_models = actual_stg | actual_int | actual_marts
    int_sql = _model_sql_map(project_dir, int_files)
    mart_sql = _model_sql_map(project_dir, mart_files)
    all_sql = {**int_sql, **mart_sql}
    for fam in sorted(domain_families):
        fam_in_intermediate = _domain_keyword_coverage(fam, actual_int, int_sql)
        fam_in_marts = _domain_keyword_coverage(fam, actual_marts, mart_sql)
        fam_overall = _domain_keyword_coverage(fam, all_models, all_sql)
        if not fam_overall:
            failures.append(
                {
                    "id": f"domain_missing:{fam}",
                    "check": "scoped domain families are represented in model layer",
                    "evidence": f"no model/sql evidence for domain `{fam}`",
                    "brief_ref": "§2-§8",
                }
            )
        elif not fam_in_intermediate or not fam_in_marts:
            failures.append(
                {
                    "id": f"domain_layer_gap:{fam}",
                    "check": "domain families should flow through intermediate and marts",
                    "evidence": (
                        f"intermediate={fam_in_intermediate}, marts={fam_in_marts} "
                        f"for domain `{fam}`"
                    ),
                    "brief_ref": "§7-§8",
                }
            )

    status = "pass" if not failures else "needs_rework"
    return ToolResult(
        ok=True,
        output=f"audit_brief_vs_marts: {status} ({len(failures)} failure(s))",
        data={"status": status, "failures": failures, "rework_target": "modeling" if failures else None},
    )


def audit_visual_relationship_paths(project_dir: Path, bi_pbip_dir: str) -> ToolResult:
    """Q8b: ensure report JSON exists and relationships cover referenced tables when parseable."""
    failures: list[dict] = []
    rels = parse_pbip_relationships(project_dir, bi_pbip_dir)
    rel_tables = set((rels.data or {}).get("tables") or [])
    report_files = list(project_dir.joinpath(bi_pbip_dir).rglob("*.json")) if (project_dir / bi_pbip_dir).exists() else []
    report_files = [p for p in report_files if "report" in str(p).lower() or "visual" in str(p).lower() or p.name == "report.json"]
    if not report_files:
        # Also accept any Report folder json
        report_files = list((project_dir / bi_pbip_dir).rglob("*.json"))[:20] if (project_dir / bi_pbip_dir).exists() else []

    if not report_files:
        failures.append(
            {
                "id": "no_report_json",
                "check": "report JSON present under BI_PBIP_DIR",
                "evidence": "no json found",
                "brief_ref": "§8",
            }
        )
    else:
        # Extract Entity-like table names from report JSON
        entities: set[str] = set()
        for rf in report_files[:10]:
            try:
                text = rf.read_text(encoding="utf-8")
            except OSError:
                continue
            for m in re.finditer(r'"Entity"\s*:\s*"([^"]+)"', text):
                entities.add(m.group(1))
            for m in re.finditer(r'"entity"\s*:\s*"([^"]+)"', text):
                entities.add(m.group(1))
        # If we found entities and relationships, every non-measure table should appear in rel graph or be alone fact_native
        if entities and rel_tables:
            # Soft check: if multiple entities and zero relationships → fail
            if len(entities) >= 2 and len(rel_tables) == 0:
                failures.append(
                    {
                        "id": "visuals_without_rels",
                        "check": "multi-table visuals require relationships",
                        "evidence": f"entities={sorted(entities)} relationships=0",
                        "brief_ref": "§8",
                    }
                )

    if not rels.ok or not (rels.data or {}).get("relationships"):
        failures.append(
            {
                "id": "no_relationships_for_visuals",
                "check": "relationships.tmdl has edges before visual path audit",
                "evidence": rels.output,
                "brief_ref": "§8",
            }
        )

    status = "pass" if not failures else "needs_rework"
    return ToolResult(
        ok=True,
        output=f"audit_visual_relationship_paths: {status}",
        data={"status": status, "failures": failures, "rework_target": "bi" if failures else None},
    )


def run_q7_build_checks(project_dir: Path, *, confirmed_build: bool = False) -> ToolResult:
    """Q7: dbt parse + dbt build (always) to validate and materialize schemas."""
    del confirmed_build
    from tools.dbt_schemas import ensure_plain_layer_schemas

    # Always enforce plain staging / intermediate / marts (no staging_marts).
    ensure_plain_layer_schemas(project_dir)
    failures: list[dict] = []
    parse = dbt_cli(project_dir, ["parse"], confirmed=True)
    if not parse.ok:
        failures.append(
            {
                "id": "dbt_parse",
                "check": "dbt parse succeeds",
                "evidence": parse.output[:2000],
                "brief_ref": "Phase 7",
            }
        )
    build = dbt_cli(project_dir, ["build"], confirmed=True)
    if not build.ok:
        failures.append(
            {
                "id": "dbt_build",
                "check": "dbt build succeeds",
                "evidence": build.output[:2000],
                "brief_ref": "Phase 7",
            }
        )
    status = "pass" if not failures else "needs_rework"
    rework_target = None
    if failures:
        blob = " ".join(str(f.get("evidence") or "") for f in failures).lower()
        # Semantic YAML / MetricFlow refs → Phase 6, not modeling SQL rework
        if (
            "semantic_models.yml" in blob
            or "semantic_model." in blob
            or "fct_example" in blob
            or "example_semantic" in blob
            or "time spine" in blob
            or "metricflow" in blob
        ):
            rework_target = "semantic"
        else:
            rework_target = "modeling"
    return ToolResult(
        ok=True,
        output=f"run_q7_build_checks: {status}",
        data={"status": status, "failures": failures, "rework_target": rework_target},
    )


def audit_q8a_relationships(project_dir: Path, bi_pbip_dir: str) -> ToolResult:
    """Q8a: relationships.tmdl must exist and match brief §8 mentions when possible."""
    failures: list[dict] = []
    rels = parse_pbip_relationships(project_dir, bi_pbip_dir)
    if not rels.ok or not (rels.data or {}).get("relationships"):
        failures.append(
            {
                "id": "missing_relationships_tmdl",
                "check": "relationships.tmdl present with at least one relationship",
                "evidence": rels.output,
                "brief_ref": "§8",
            }
        )
        return ToolResult(
            ok=True,
            output="audit_q8a: needs_rework",
            data={"status": "needs_rework", "failures": failures, "rework_target": "bi"},
        )

    brief = project_dir / "design_brief.md"
    if brief.exists():
        from tools.pbip import parse_brief_relationship_matrix

        btext = brief.read_text(encoding="utf-8")
        rel_tables_l = {t.lower() for t in (rels.data or {}).get("tables") or []}
        matrix = parse_brief_relationship_matrix(btext)
        # Require dim_* endpoints that appear in the §8 matrix (not loose substring hints —
        # e.g. dim_customer is a substring of dim_customers and caused false failures).
        required_dims: set[str] = set()
        for row in matrix:
            for side in ("from_table", "to_table"):
                name = (row.get(side) or "").strip().lower()
                if name.startswith("dim_"):
                    required_dims.add(name)
        for hint in sorted(required_dims):
            covered = any(
                hint == t or hint in t or t.endswith(hint) or t.split()[-1] == hint
                for t in rel_tables_l
            )
            if not covered:
                failures.append(
                    {
                        "id": f"missing_rel_{hint}",
                        "check": f"brief §8 matrix includes {hint}; relationships should include it",
                        "evidence": f"rel tables={sorted(rel_tables_l)}",
                        "brief_ref": "§8",
                    }
                )

    status = "pass" if not failures else "needs_rework"
    return ToolResult(
        ok=True,
        output=f"audit_q8a: {status}",
        data={"status": status, "failures": failures, "rework_target": "bi" if failures else None},
    )
