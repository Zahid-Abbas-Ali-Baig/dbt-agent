# Phase 1 — table scope

Return ONLY valid JSON (no markdown fences):

```
{
  "in_scope": ["table_a", "table_b"],
  "deferred": ["table_c"],
  "noise_notes": [
    {"reason": "SuiteCRM ACL / permissions", "tables": ["crm_acl_actions", "crm_acl_roles"]},
    {"reason": "audit history", "tables": ["crm_accounts_audit"]}
  ],
  "notes": "one short sentence"
}
```

Rules:
- Choose tables that answer the business questions in requirements.md.
- Prefer transactional / dimension entities (orders, subscriptions, payments, invoices, devices, partners, programs, customers, SKUs).
- Defer SuiteCRM platform noise (ACL, audit-only, email, KB, report builders, empty bridges) unless a requirement needs them.
- **Never silently drop tables.** Every deferred/noise table must appear under `noise_notes` with a plain-language `reason` the human can judge. Group by reason; list representative table names (all if few).
- Only use table names from the provided full list. Do not invent names.
- Keep in_scope focused but complete enough for the KPI map — typically dozens, not hundreds.
- The human confirms scope; your job is propose + explain, not finalize.
