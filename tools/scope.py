"""Requirements-driven table scope for Phase 1 discovery."""

from __future__ import annotations

import re
from typing import Any

# Prefer business / domain tables (SuiteCRM tos_*, payments_*, invoices, …)
_INCLUDE = (
    r"tos_",
    r"payments_",
    r"aos_invoice",
    r"aos_quotes",
    r"credit_note",
    r"subscription",
    r"order",
    r"device",
    r"channel_partner",
    r"program",
    r"corporate",
    r"_sku",
    r"sku_",
    r"payment_method",
    r"payment_histor",
    r"address",
    r"countries",
    r"crm_accounts$",
    r"crm_accounts_cstm$",
    r"subscribers",
    r"refund",
    r"api_log",
)

# SuiteCRM platform / noise modules — (pattern, human reason). Never drop silently.
_EXCLUDE_LABELED: tuple[tuple[str, str], ...] = (
    (r"_audit$", "audit history (row change log, not a business fact table)"),
    (r"acl_", "SuiteCRM ACL / permissions"),
    (r"aod_", "SuiteCRM Knowledge Base (AOD) module"),
    (r"aok_", "SuiteCRM Knowledge Base (AOK) module"),
    (r"aop_", "SuiteCRM Cases portal (AOP) module"),
    (r"aor_", "SuiteCRM Reports (AOR) module"),
    (r"am_project", "SuiteCRM Project Management module"),
    (r"am_task", "SuiteCRM Project Management tasks"),
    (r"emailman", "email queue / mailer internals"),
    (r"emails", "CRM email records (usually not KPI grain)"),
    (r"inbound_email", "inbound email config"),
    (r"outbound_email", "outbound email config"),
    (r"job_queue", "background job queue"),
    (r"tracker", "UI / session tracker"),
    (r"sessions", "web sessions"),
    (r"oauth", "OAuth tokens / auth plumbing"),
    (r"custom_fields", "SuiteCRM field metadata"),
    (r"fields_meta", "SuiteCRM field metadata"),
    (r"bugs", "Bugs module (unless requirements mention defect KPIs)"),
    (r"prospect", "Prospects / leads module"),
    (r"campaign", "Campaigns module"),
    (r"document", "Documents module"),
    (r"notes$", "Notes module"),
    (r"calls$", "Calls activity module"),
    (r"meetings$", "Meetings activity module"),
    (r"tasks$", "Tasks activity module"),
    (r"folders", "UI folders"),
    (r"templates", "email / doc templates"),
    (r"saved_search", "saved UI searches"),
    (r"favorites", "UI favorites"),
    (r"vcals", "calendar sync"),
    (r"releases", "product releases module"),
    (r"roles", "user roles / ACL"),
    (r"users$", "CRM users (operators, not customers)"),
    (r"user_", "user preference / auth plumbing"),
    (r"team", "teams / security groups"),
    (r"securitygroup", "security groups"),
    (r"eapm", "external app password manager"),
    (r"oauth_tokens", "OAuth tokens"),
    (r"config$", "app config rows"),
    (r"currencies", "currency lookup (include only if FX KPIs need it)"),
    (r"sugarfeed", "activity feed"),
    (r"import_maps", "import mapping config"),
    (r"workflow", "workflow engine"),
    (r"schedulers", "cron / schedulers"),
    (r"cron", "cron / schedulers"),
    (r"_cstm$", "custom extension table (pair with base entity when needed)"),
)

_EXCLUDE = tuple(pat for pat, _ in _EXCLUDE_LABELED)

_BRIDGE_REASON = "empty CRM bridge / junction (accounts-bugs/cases/contacts/opportunities)"
_LOW_SCORE_REASON = "name does not match requirements or known analytics entities"


def score_table(name: str, requirements: str = "") -> int:
    low = (name or "").lower()
    req = (requirements or "").lower()
    score = 0
    for pat in _INCLUDE:
        if re.search(pat, low):
            score += 3
    for pat in _EXCLUDE:
        if re.search(pat, low):
            score -= 5
    # Exact / token hit in requirements
    bare = low.replace("crm_", "").replace("payments_", "")
    if bare and bare in req:
        score += 5
    if low in req:
        score += 6
    # Soft-deleted empty bridges often named accounts_X
    if re.search(r"accounts_(bugs|cases|contacts|opportunities)$", low):
        score -= 4
    return score


def defer_reason(name: str) -> str:
    """Human-readable why a table is treated as noise / deferred."""
    low = (name or "").lower()
    if re.search(r"accounts_(bugs|cases|contacts|opportunities)$", low):
        return _BRIDGE_REASON
    for pat, reason in _EXCLUDE_LABELED:
        if re.search(pat, low):
            return reason
    return _LOW_SCORE_REASON


def group_noise(
    deferred: list[str],
    *,
    examples_per_group: int = 6,
) -> list[dict[str, Any]]:
    """Group deferred tables by reason for human review (not silent drop)."""
    buckets: dict[str, list[str]] = {}
    for t in deferred:
        reason = defer_reason(t)
        buckets.setdefault(reason, []).append(t)
    groups: list[dict[str, Any]] = []
    for reason, tables in sorted(buckets.items(), key=lambda x: (-len(x[1]), x[0])):
        samples = tables[:examples_per_group]
        groups.append(
            {
                "reason": reason,
                "count": len(tables),
                "examples": samples,
                "tables": tables,
            }
        )
    return groups


def format_noise_summary(
    noise_groups: list[dict[str, Any]],
    *,
    max_groups: int = 12,
) -> str:
    """Markdown block explaining deferred/noise tables to the human."""
    if not noise_groups:
        return ""
    lines = [
        "Likely noise / deferred (not removed - say which to keep if any belong in scope):",
        "",
    ]
    shown = noise_groups[:max_groups]
    for g in shown:
        reason = str(g.get("reason") or "deferred")
        count = int(g.get("count") or 0)
        examples = list(g.get("examples") or g.get("tables") or [])[:6]
        sample = ", ".join(f"`{t}`" for t in examples)
        extra = f" (+{count - len(examples)} more)" if count > len(examples) else ""
        lines.append(f"- **{reason}** - {count} table(s): {sample}{extra}")
    rest = len(noise_groups) - len(shown)
    if rest > 0:
        lines.append(f"- ... and {rest} more reason group(s) in `.dbt_agent/scope_proposed.json`")
    return "\n".join(lines)


def propose_table_scope(
    all_tables: list[str],
    requirements: str = "",
    *,
    min_score: int = 1,
) -> dict[str, Any]:
    """Split full schema into proposed in-scope vs deferred tables."""
    scored: list[tuple[int, str]] = []
    for t in all_tables:
        scored.append((score_table(t, requirements), t))
    scored.sort(key=lambda x: (-x[0], x[1]))

    in_scope = [t for s, t in scored if s >= min_score]
    # Always keep crm_accounts + cstm when present (customer KPIs)
    for must in ("crm_accounts", "crm_accounts_cstm"):
        if must in all_tables and must not in in_scope:
            in_scope.append(must)

    deferred = [t for t in all_tables if t not in set(in_scope)]
    noise = group_noise(deferred)
    reasons = {t: defer_reason(t) for t in deferred}
    return {
        "all_tables": list(all_tables),
        "in_scope": in_scope,
        "deferred": deferred,
        "noise": noise,
        "defer_reasons": reasons,
        "scores": {t: s for s, t in scored},
    }


def parse_scope_reply(text: str, all_tables: set[str] | list[str]) -> list[str] | None:
    """Parse a human reply into a table list, or None if it is a yes/accept."""
    raw = (text or "").strip()
    if not raw:
        return None
    lower = raw.lower()
    if lower in {"yes", "y", "ok", "okay", "accept", "looks good", "go", "continue"}:
        return None
    known = set(all_tables)
    # Split on commas / newlines / whitespace
    parts = re.split(r"[\s,;]+", raw)
    found: list[str] = []
    seen: set[str] = set()
    for p in parts:
        name = p.strip().strip("`")
        if not name or name.lower() in {"yes", "y", "and", "the", "tables"}:
            continue
        # Allow without crm_ prefix match
        if name in known and name not in seen:
            found.append(name)
            seen.add(name)
            continue
        for t in known:
            if t.lower() == name.lower() and t not in seen:
                found.append(t)
                seen.add(t)
                break
    return found if found else None
