# Project Config

> Fill once per engagement. Attach alongside [`dbt_ai_agent_prompts_generalized.md`](dbt_ai_agent_prompts_generalized.md) when running any phase prompt.

## How to Use

1. Replace every `{{placeholder}}` below with your project values.
2. Set `REQUIREMENTS_DOC`, `DESIGN_BRIEF_DOC`, and feedback-loop paths (defaults below).
3. Attach this file when running phase prompts. Attach `requirements.md` starting at **Phase 1**.
4. The agent reads `config.md` at the start of every phase and substitutes values into commands and file paths.
5. Optionally set `KPI_BREAKDOWN_MIN_POPULATED_PCT` and `KPI_BREAKDOWN_MIN_DISTINCT` — see [KPI breakdown thresholds](#kpi-breakdown-thresholds-optional) below.
6. After client review: fill [`client_feedback.md`](client_feedback.md), then run the **Feedback Re-run** prompt from the prompt library.

> **Security:** Do not commit real passwords. Use a local-only copy of this file or environment variables for production credentials.

---

## Variables

```
PROJECT_ROOT:        .                        # dbt project files created in current directory
REQUIREMENTS_DOC:    requirements.md
DESIGN_BRIEF_DOC:    design_brief.md
CLIENT_FEEDBACK_DOC: client_feedback.md       # filled by DE/analyst after client review

PROJECT_NAME:        {{my_project}}           # dbt project name, no spaces

# --- SOURCE warehouse (landed tables) — type is dynamic for future releases ---
SOURCE_WAREHOUSE_TYPE: {{postgres}}           # v1: postgres (later: snowflake|redshift|bigquery|…)
SOURCE_DATABASE_NAME:  {{my_database}}
SOURCE_SCHEMA_NAME:    {{my_schema}}          # landed schema
SOURCE_DB_HOST:        {{localhost}}
SOURCE_DB_PORT:        {{5432}}
SOURCE_DB_USER:        {{postgres}}
SOURCE_DB_PASSWORD:    {{password}}
SOURCE_NAME:           {{my_source}}          # dbt source name in _sources.yml

# --- TARGET warehouse (dbt writes) — type dynamic; may match SOURCE ---
TARGET_WAREHOUSE_TYPE: {{postgres}}           # v1: postgres only
TARGET_SAME_AS_SOURCE: {{true}}               # true | false
TARGET_DATABASE_NAME:  {{my_database}}
TARGET_DB_HOST:        {{localhost}}
TARGET_DB_PORT:        {{5432}}
TARGET_DB_USER:        {{postgres}}
TARGET_DB_PASSWORD:    {{password}}
TARGET_DB_THREADS:     {{4}}

# Legacy aliases (kept in sync from SOURCE_* by the agent)
WAREHOUSE_TYPE:      {{postgres}}
DATABASE_NAME:       {{my_database}}
SCHEMA_NAME:         {{my_schema}}
DB_HOST:             {{localhost}}
DB_PORT:             {{5432}}
DB_USER:             {{postgres}}
DB_PASSWORD:         {{password}}
DB_THREADS:          {{4}}

STAGING_SCHEMA:      {{staging}}              # Postgres schema for stg_* models (plain name)
INTERMEDIATE_SCHEMA: {{intermediate}}         # Postgres schema for int_* models (plain name)
MARTS_SCHEMA:        {{marts}}                # Postgres schema for fct_/dim_/bridge_* (plain name)
# Optional: profiles.yml fallback only (not a layer). Default is "dbt".
# TARGET_PROFILE_SCHEMA: dbt

ENABLE_SEMANTIC_LAYER: {{true}}
BI_TOOL:               {{powerbi}}
BI_PBIP_DIR:           {{powerbi-project}}

KPI_BREAKDOWN_MIN_POPULATED_PCT: {{5}}
KPI_BREAKDOWN_MIN_DISTINCT:      {{2}}
PATH_CONFIDENCE_THRESHOLD:       {{70}}
```

> **Not in this file:** table lists, fact/dimension classifications, column renames, join keys, metrics, or business questions. Those are inferred from the requirements doc + schema discovery in Phase 1.

> **Alignment:** `DATABASE_NAME`, `SCHEMA_NAME`, and `WAREHOUSE_TYPE` here should match the source-system hints in [`requirements.md`](requirements.md).

> **Phase 8 human step:** Human creates `BI_PBIP_DIR` and saves an empty `.pbip` into that folder in Power BI Desktop (Desktop creates linked `.Report/` and `.SemanticModel/` siblings) before Phase 8.

> **Phase 8 BI source:** Power BI imports from `MARTS_SCHEMA` (presentation layer), not `SCHEMA_NAME` (raw source). Confirm alignment in `dbt_project.yml` (`models.marts.+schema`).

---

## KPI breakdown thresholds (optional)

Two optional variables set a **pass/fail bar** for dimensional breakdowns (bar charts, donuts, grouped tables). They do **not** name tables or columns — the agent infers breakdown attributes from `requirements.md` and discovers join paths on **`SCHEMA_NAME`** (landed Postgres) in Phase 1.

| Variable | Type | Default if omitted |
| -------- | ---- | ------------------ |
| `KPI_BREAKDOWN_MIN_POPULATED_PCT` | number (0–100) | Agent proposes a value in Design Brief §3.5 |
| `KPI_BREAKDOWN_MIN_DISTINCT` | integer ≥ 1 | Agent proposes a value in Design Brief §3.5 |

### What each threshold means

**`KPI_BREAKDOWN_MIN_POPULATED_PCT`** — On the **KPI grain** (e.g. non-voided fact rows), at least this percentage of rows must have a **non-empty** breakdown attribute after the chosen join path. Empty string counts as empty (`nullif(trim(col), '')`).

**`KPI_BREAKDOWN_MIN_DISTINCT`** — For category visuals (bar, donut, slicer with meaningful split), the breakdown column needs at least this many **distinct non-empty** values. Use `2` so a chart is not a single blank bar.

### Where the agent uses them

| Phase | How config is applied |
| ----- | --------------------- |
| **1 — Discovery** | When comparing candidate join paths in §3.5, flag paths that cannot meet both thresholds on landed data. Stop with open questions if **no** path passes without a human waiver. |
| **7 — Validation** | After `dbt build`, run attribute smoke tests on mart columns used as breakdowns. Log **pass / fail / waiver** per column vs these thresholds. Failed columns are **Phase 8 blockers** unless waived in the brief. |
| **8 — BI wireframe** | Do not put failed breakdown columns on chart `Category` axes unless **deferred** with documented waiver. |

### Worked example (generic)

**Config snippet:**

```
KPI_BREAKDOWN_MIN_POPULATED_PCT: 5
KPI_BREAKDOWN_MIN_DISTINCT: 2
```

**Business question (from requirements):** “Which product categories sell most by channel?”

**Phase 1 — §3.5 (agent profiles `SCHEMA_NAME` on Postgres only):**

| Candidate path | Join-success (non-empty category) | Distinct categories | vs config |
| -------------- | --------------------------------: | ------------------: | --------- |
| Path A: `order_line.direct_product_fk` → product dim | 0% | 0 | **Fail** — FK never populated on landed data |
| Path B: `order_line.partner_product_fk` → bridge → product dim | 94% | 8 | **Pass** — primary path for brief |
| Path C: `order_line` → related line-item extension | 12% | 3 | Pass, but lower than B — document as fallback |

**Outcome:** Design Brief §3.5 chooses **Path B** as primary. Human approves brief.

**Phase 7 — smoke test on mart** (after dbt implements Path B):

| Mart column | Populated % on KPI grain | Distinct | Result |
| ----------- | ---------------------: | -------: | ------ |
| `product_category` | 93% | 8 | **Pass** → OK for Phase 8 bar chart |
| `product_category` (if agent had built Path A only) | 0% | 0 | **Fail** → block “Sales by category” visual |

**Phase 8 — wireframe:** Bar chart `Category = product_category` is allowed because Phase 7 passed. If smoke test had failed, the agent marks the visual **deferred** and proposes Path B/C from §3.5 instead of shipping one blank bar.

### Tuning guidance

| Engagement style | Suggested values | Rationale |
| ---------------- | ---------------- | --------- |
| Executive / standard dashboards | `5` / `2` | Blocks empty or single-value category charts |
| Sparse B2B (few partners or SKUs) | `2` / `2` | Lower population bar; still require ≥2 categories for bars |
| Strict QA before client delivery | `10` / `3` | Stricter; more KPIs may need waiver or fallback paths |
| Exploratory / first discovery run | Omit both | Agent documents proposed thresholds in §3.5 for you to approve |

**Waiver:** If the business accepts a sparse or empty breakdown (e.g. new product line not yet in source), document **waiver** in Design Brief §3.5 — do not lower config silently to force a pass.

---

## Example (do not use as active config)

<!--
PROJECT_NAME:        shopsphere
WAREHOUSE_TYPE:      postgres
DATABASE_NAME:       shopsphere
SCHEMA_NAME:         ecommerce
DB_HOST:             localhost
DB_PORT:             5432
DB_USER:             postgres
DB_PASSWORD:         <your-password>
DB_THREADS:          4

SOURCE_NAME:         ecommerce
STAGING_SCHEMA:      staging
INTERMEDIATE_SCHEMA: intermediate
MARTS_SCHEMA:        marts

ENABLE_SEMANTIC_LAYER: true
BI_TOOL: powerbi
BI_PBIP_DIR: powerbi-project

KPI_BREAKDOWN_MIN_POPULATED_PCT: 5
KPI_BREAKDOWN_MIN_DISTINCT: 2

-->
