# Design Brief — {{project_display_name}}

**Status:** pending approval

> Phase 1 writes the full Design Brief here (all sections per the Phase 1 prompt / dbt-ai-guide).
>
> **Approval:** Edit this file in place, correct any errors, then change status to `approved` before Phase 2.
>
> Phases 2–8 read this file from disk — not chat history.

---

## 1. Domain Summary

*(pending Phase 1)*

## 1.5 Industry reporting pattern

*(pending Phase 1 — infer from Domain Summary + landed entities; subject areas, pages, KPI families grounded in discovered tables)*

## 2. Business Questions → KPI Map

| Business Question | Proposed KPI / Metric | Target Grain |
| ----------------- | --------------------- | ------------ |
|                   |                       |              |

## 3. Source Inventory

| Raw Table | Row Grain | PK Column(s) | Classification (fact / dim / bridge) |
| --------- | --------- | ------------ | ------------------------------------ |
|           |           |              |                                      |

## 3.5 KPI Attribute Sourcing & Landed-Data Viability

| Business Question (from requirements) | Breakdown Attributes (inferred) | KPI Grain | Candidate Path (discovered) | Landed-Data Stats (SCHEMA_NAME) | Chosen Primary Path | Fallback Path | BI Viability (pass / fail / waiver) |
| --------------------------------------- | ------------------------------- | --------- | --------------------------- | ------------------------------- | ------------------- | ------------- | ----------------------------------- |
|                                         |                                 |           |                             |                                 |                     |               |                                     |

## 4. Relationship Graph

| From Table | To Table | Join Key | Cardinality | Orphan Count (from profiles / dbt show) |
| ---------- | -------- | -------- | ----------- | --------------------------------------- |
|            |          |          |             |                                         |

## 5. Column Standardization Plan

| Source Table | Source Column | Staging Column | Transformation |
| ------------ | ------------- | -------------- | -------------- |
|              |               |                |                |

## 6. Staging Model List

| Staging Model   | Source Table | Notes |
| --------------- | ------------ | ----- |
| stg_{raw_table} |              |       |

## 7. Relationship Resolution Plan (Intermediate)

| Intermediate Model   | Type                                  | Inputs (staging refs) | Join Keys | Output Grain |
| -------------------- | ------------------------------------- | --------------------- | --------- | ------------ |
| int_{entity}__{verb} | join / bridge / aggregate / reconcile |                       |           |              |

## 8. Mart Star Schema

| Mart Model      | Type   | Primary Intermediate Input(s) | Grain | Subject Area Folder |
| --------------- | ------ | ----------------------------- | ----- | ------------------- |
| fct_{entity}    | fact   |                               |       |                     |
| dim_{entity}    | dim    |                               |       |                     |
| bridge_{entity} | bridge |                               |       |                     |

### BI date strategy (Power BI)

| Setting | Value |
| ------- | ----- |
| Conformed date dim | |
| Strategy | `conformed_dim` \| `fact_native` |
| Conformed dim key column | |
| Fact date role columns | |

### Power BI relationships (agent Phase 8 wiring)

| From (fact) | To (dim) | From column | To column | Active | Notes |
| ----------- | -------- | ----------- | --------- | ------ | ----- |
|             |          |             |           |        |       |

### Dim BI role inventory

| Dim mart | BI role | Connected by (fact → dim rows) |
| -------- | ------- | ------------------------------ |
|          |         |                                |

## 9. Semantic Metrics List

| Metric Name | Type (simple / ratio / derived) | Base Mart Model | Measure / Formula | Filter |
| ----------- | ------------------------------- | --------------- | ----------------- | ------ |
|             |                                 |                 |                   |        |

## 10. Work Batches (max 3 tables per codegen call)

| Batch | Tables | Phase   |
| ----- | ------ | ------- |
| 1     |        | sources |
| 2     |        | sources |
| ...   |        | staging |
