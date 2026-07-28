# Requirements — {{project_display_name}}

> Fill once per engagement. Attach with [`config.md`](config.md) for **Phase 1** discovery and later phases (not needed for Phase 0 bootstrap).
>
> **No `Status` field on this file.** After client feedback, the agent updates business questions and notes here on Feedback Re-run pass 1. **Technical implementation follows [`design_brief.md`](design_brief.md)** after that file is approved.
>
> Replace every `{{placeholder}}` below. Keep `DATABASE_NAME`, `SCHEMA_NAME`, and `WAREHOUSE_TYPE` aligned with [`config.md`](config.md).
>
> **Do not list tables, join keys, column renames, or metric formulas here** — Phase 1 infers those from live schema discovery plus this document.

---

## Domain Summary

{{What industry or business area? What entities or processes exist at a high level?}}

*Feeds Design Brief §1 and semantic descriptions for staging/mart models.*

---

## Business Goals

- {{Goal 1 — e.g. unified reporting, self-serve analytics}}
- {{Goal 2}}

*Feeds scope and prioritization during table classification in Phase 1.*

---

## Pain Points

- {{Pain point 1 — e.g. inconsistent metric definitions across teams}}
- {{Pain point 2}}

*Feeds KPI prioritization and reconciliation hints in the Design Brief.*

---

## Business Questions

- {{Question 1 — must be answerable from source data}}
- {{Question 2}}
- {{Question 3}}

*Feeds Design Brief §2 (KPI map) and §9 (semantic metrics).*

---

## Source Systems

- {{WAREHOUSE_TYPE}} database `{{DATABASE_NAME}}`, schema `{{SCHEMA_NAME}}`
- {{Optional: ERP name, SaaS tool, file feed, or other context}}
- {{Data freshness, batch vs streaming, known quality issues}}

*Provides context alongside `config.md` during Phase 1 schema discovery — not a table inventory. You may describe multiple upstream systems in prose; live codegen discovery uses `SCHEMA_NAME` from config (v1: one schema per engagement).*

---

## Reporting Preferences

- {{Star schema / wide tables / mixed}}
- {{Semantic layer for self-serve? BI tool expectations?}}
- {{Preferred grains: daily, weekly, monthly}}

*Feeds star-schema design, mart grains, and semantic layer expectations in the Design Brief. Semantic layer models are built only when `ENABLE_SEMANTIC_LAYER: true` in config.*

---

## Constraints and Notes

- {{Rows or statuses to exclude}}
- {{Currency, timezone, or definition caveats}}
- {{Known ambiguities for the agent to flag in the Design Brief}}

*Feeds filters, accepted business rules, and open questions surfaced at the Phase 1 approval gate.*

---

## Example (do not use as active requirements)

<!--
# Requirements — ShopSphere E-Commerce

## Domain Summary
Online retailer selling physical goods. Core processes: browse → cart → order → payment → fulfillment.

## Business Goals
- Unified reporting for sales, customers, and product performance
- Replace ad-hoc SQL with governed dbt models

## Pain Points
- Inconsistent revenue definitions across teams
- No single source of truth for customer lifetime value

## Business Questions
- What is total revenue and order count by month?
- Who are the top customers by lifetime spend?
- Which product categories drive the most revenue?

## Source Systems
- postgres database `shopsphere`, schema `ecommerce`
- Operational OLTP tables; nightly batch load

## Reporting Preferences
- Star schema for BI tools
- Semantic layer metrics for self-serve analytics
- Daily order grain; monthly executive rollups

## Constraints and Notes
- Exclude cancelled and fully refunded orders from revenue KPIs
- Currency assumed USD
-->
