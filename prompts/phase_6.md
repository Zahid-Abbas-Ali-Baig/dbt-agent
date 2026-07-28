# Phase 6 — Semantic Layer

Write models/semantic/semantic_models.yml when ENABLE_SEMANTIC_LAYER=true.
Metrics trace to brief §9 / §2.

Hard rules:
- Every `model: ref('name')` must be an existing mart under models/marts/ (fct_/dim_/bridge_).
- Never use placeholders (fct_example, example_semantic, example_row_count).
- The agent also writes models/semantic/metricflow_time_spine.sql (+ YAML) — MetricFlow needs a day time spine.
- YAML only — no chat narration.
