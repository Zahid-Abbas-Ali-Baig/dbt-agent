# Client Feedback

> **Who fills this:** Data engineer + data analyst — after the client sends review comments.  
> **What happens next:** Run **Feedback Re-run** from [`dbt_ai_agent_prompts_generalized.md`](dbt_ai_agent_prompts_generalized.md) **twice** per cycle: (1) agent updates `requirements.md` + `design_brief.md`, sets `Status: pending approval` on **design_brief.md only** → you review; (2) set `design_brief.md` to `Status: approved` → run again to implement from the design brief.

---

| Date | Cycle | Deliverable reviewed |
| ---- | ----- | -------------------- |
| {{YYYY-MM-DD}} | {{2}} | {{e.g. dashboard v1, marts + semantic layer}} |

## Changes

| KPI / area | What the client said | Agreed fix (definition or behavior) | Phases |
| ---------- | -------------------- | ----------------------------------- | ------ |
| {{Finance variance}} | {{"Shows zero but numbers don't match"}} | {{Collected − invoiced by month; use created_at when payment_timestamp is null}} | {{1, 4, 5, 7, 8}} |
| {{Pre-order share}} | {{"Card is blank"}} | {{Show 0% when there are no pre-orders}} | {{8}} |

## Phases to re-run this cycle

List every phase number needed **once** (union of the Phases column above):

```
1, 4, 5, 7, 8
```

> **Phase guide (pick the minimum):**  
> **1** — KPI definition changed → update `requirements.md` (business) and `design_brief.md` (technical); **approve design_brief.md** before code  
> **3** — row filters / status flags in staging  
> **4** — joins, reconciliation, intermediate logic  
> **5** — mart columns or grain  
> **6** — semantic layer metrics  
> **7** — `dbt build` + tests (almost always include if 3–6 ran)  
> **8** — DAX measures or PBIP report pages/visuals under `BI_PBIP_DIR`

---

## Example (do not use)

<!--
| KPI | What the client said | Agreed fix | Phases |
| Revenue | Exclude cancelled orders | SUM where status not cancelled | 1, 3, 7 |
| Conversion card | Blank should be 0% | DIVIDE with 0 fallback | 8 |

Phases to re-run: 1, 3, 7, 8
-->
