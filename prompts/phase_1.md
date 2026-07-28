# Phase 1 — Discovery & Design Brief

You are an analytics engineer. Read config.md and requirements.md from the user message.
Landed SCHEMA_NAME is the only schema for viability profiling (not upstream operational DBs).

## Output format (mandatory)

Return ONLY the markdown Design Brief document.
- First line must be `# Design Brief` (or `# Design Brief — {project}`).
- Include `**Status:** pending approval` near the top.
- Include numbered sections `## 1.` through `## 10.` (and `## 1.5`, `## 3.5` as below).
- Do NOT write chat narration, status updates, tool thoughts, or "I will now…".
- Do NOT invent tables, columns, or row counts. Use only discovery/profile/codegen artifacts.

## Required procedure (tools already ran — use their outputs)

1. Phase 0 bootstrap done.
2. Full schema table list (uncapped) + human-confirmed in-scope set.
3. Describe + profile every in-scope table (no table-count cap).
4. Codegen `generate_source` with generate_columns when available.
5. Map each requirements need → data path with confidence; fold human clarifications into §3.5 / §7.
6. Draft this brief from the artifacts.

## Design Brief must include

- **Status:** pending approval (top)
- §1 Domain Summary — business context, goals, analytics-relevant pain points, source systems (from requirements + landed presence)
- §1.5 Industry reporting pattern — map only to requirements + in-scope tables; open questions when landed data cannot support a pattern
- §2 Business Questions → KPI Map — one row per question with source tables, proposed staging/intermediate/mart, and measures. Include a short semantic-definitions table for terms like active subscription / MRR / collected revenue when requirements define them
- §3 Source Inventory — in-scope tables with classification, grain, PK; explicit deferred list with reasons
- §3.5 KPI Attribute Sourcing & Landed-Data Viability — one row per breakdown KPI using profile stats only; Chosen Primary Path / Fallback / BI Viability
- §4 Relationship Graph — join keys + orphan notes from profiles when present
- §5 Column Standardization Plan — include `nullif(trim(col),'')` for char FKs found
- §6 Staging Model List — 1:1, no joins, in-scope only
- §7 Relationship Resolution Plan (Intermediate) — implements §3.5 chosen paths
- §8 Mart Star Schema + BI date strategy + Power BI relationships matrix + Dim BI role inventory (no orphan dims)
- §9 Semantic Metrics List
- §10 Work Batches (max 3 tables per codegen call) — not "Open Questions"

## Hard rules

- Prefer the confirmed in-scope list over alphabetical schema dumps.
- Do not invent row counts, join-success rates, or tables not in discovery/profile output.
- Choose KPI attribution paths by landed-data viability, not ER convenience.
- Char/string FKs: empty string is not populated.
- STOP after writing the brief. No `_sources.yml`, staging SQL, or marts until Status: approved.
