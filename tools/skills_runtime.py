"""Load Cursor-style skills into LLM system prompts (Flask / Cursor API path).

Cursor IDE auto-attaches SKILL.md when relevant. The engagement app talks to the
Cursor API as a plain chat backend, so we inject the same skill bodies into the
system prompt here.

Resolution order for each skill pack:
1. ``<repo>/skills/<name>/`` (e.g. be-human)
2. ``<repo>/skills/dbt/<name>/`` (vendored dbt packs)
3. ``$DBT_SKILLS_ROOT/<name>/`` if set
4. ``~/.cursor/skills/<name>/``
5. ``~/.agents/skills/<name>/``
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable

# Cap total injected skill text so phase prompts still fit.
_MAX_SKILL_CHARS = 28_000
_MAX_REF_CHARS = 8_000

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILLS_ROOT = _REPO_ROOT / "skills"
_VENDORED_DBT = _SKILLS_ROOT / "dbt"

# Always attached — same role as Cursor IDE be-human for free-form prose.
ALWAYS_SKILLS: tuple[str, ...] = ("be-human",)

# Agent name → skill packs (Cursor-style "when to use")
AGENT_SKILLS: dict[str, tuple[str, ...]] = {
    "discovery": (
        "using-dbt-for-analytics-engineering",
        "fetching-dbt-docs",
    ),
    "modeling": (
        "using-dbt-for-analytics-engineering",
        "running-dbt-commands",
    ),
    "semantic": (
        "building-dbt-semantic-layer",
        "using-dbt-for-analytics-engineering",
    ),
    "quality_loop": (
        "using-dbt-for-analytics-engineering",
        "running-dbt-commands",
        "adding-dbt-unit-test",
        "troubleshooting-dbt-job-errors",
    ),
    "bi": (),
}

# Extra skills keyed off prompt / user text (phase files, topics)
KEYWORD_SKILLS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"phase_0|bootstrap|dbt_project|packages\.yml", re.I), ("running-dbt-commands",)),
    (re.compile(r"phase_1|discover|design_brief|scope", re.I), ("using-dbt-for-analytics-engineering", "fetching-dbt-docs")),
    (re.compile(r"phase_[2345]|staging|intermediate|marts|ref\(|source\(", re.I), ("using-dbt-for-analytics-engineering", "running-dbt-commands")),
    (re.compile(r"phase_6|semantic|metricflow|metrics", re.I), ("building-dbt-semantic-layer",)),
    (re.compile(r"phase_7|dbt build|dbt parse|Q7", re.I), ("running-dbt-commands", "troubleshooting-dbt-job-errors")),
    (re.compile(r"unit.?test|schema.?yml|data.?test", re.I), ("adding-dbt-unit-test",)),
    (re.compile(r"\bmesh\b|model contract|cross-project", re.I), ("working-with-dbt-mesh",)),
    (re.compile(r"mermaid|lineage|dag", re.I), ("creating-mermaid-dbt-dag",)),
)


def _skill_search_roots() -> list[Path]:
    roots: list[Path] = []
    if _SKILLS_ROOT.is_dir():
        roots.append(_SKILLS_ROOT)
    if _VENDORED_DBT.is_dir():
        roots.append(_VENDORED_DBT)
    env = (os.getenv("DBT_SKILLS_ROOT") or "").strip()
    if env:
        roots.append(Path(env).expanduser())
    cursor_skills = Path.home() / ".cursor" / "skills"
    if cursor_skills.is_dir():
        roots.append(cursor_skills)
    home = Path.home() / ".agents" / "skills"
    if home.is_dir():
        roots.append(home)
    return roots


def resolve_skill_dir(name: str) -> Path | None:
    for root in _skill_search_roots():
        candidate = root / name
        if (candidate / "SKILL.md").is_file():
            return candidate
    return None


def _strip_frontmatter(text: str) -> str:
    body = text.strip()
    if not body.startswith("---"):
        return body
    parts = body.split("---", 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return body


@lru_cache(maxsize=64)
def _read_skill_pack(name: str) -> str:
    """SKILL.md + optional references/*.md (truncated). Cached per process."""
    skill_dir = resolve_skill_dir(name)
    if skill_dir is None:
        return ""
    skill_path = skill_dir / "SKILL.md"
    try:
        raw = skill_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    chunks = [f"### Skill: `{name}`\n\n{_strip_frontmatter(raw)}"]
    refs = skill_dir / "references"
    if refs.is_dir():
        used = 0
        for path in sorted(refs.glob("*.md")):
            if used >= _MAX_REF_CHARS:
                chunks.append(
                    f"\n_(Additional references under `{name}/references/` omitted for length.)_"
                )
                break
            try:
                text = _strip_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            if len(text) > 4000:
                text = text[:4000] + "\n…[truncated]"
            block = f"\n\n#### Reference: `{path.name}`\n\n{text}"
            chunks.append(block)
            used += len(block)
    return "\n".join(chunks).strip()


def skills_for_agent(agent_name: str | None, *blobs: str) -> list[str]:
    """Ordered unique skill names for this agent + prompt context."""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(names: Iterable[str]) -> None:
        for n in names:
            if n not in seen:
                seen.add(n)
                ordered.append(n)

    # Voice/style first (like Cursor IDE always applying be-human to prose)
    add(ALWAYS_SKILLS)
    add(AGENT_SKILLS.get((agent_name or "").strip().lower(), ()))
    hay = "\n".join(b for b in blobs if b)
    for pattern, names in KEYWORD_SKILLS:
        if pattern.search(hay):
            add(names)
    # Always include core AE skill for modeling-ish work if nothing dbt-specific matched
    dbt_names = [n for n in ordered if n != "be-human"]
    if not dbt_names and agent_name in {"discovery", "modeling", "quality", "semantic"}:
        add(("using-dbt-for-analytics-engineering",))
    return ordered


def build_skills_block(skill_names: list[str]) -> str:
    if not skill_names:
        return ""
    parts: list[str] = [
        "## Installed skills (apply these the same way Cursor IDE would)",
        "Follow the skill guidance below for writing voice, dbt modeling, commands, tests, and semantics.",
        "Prefer skill rules when they conflict with vague improvisation; still obey engagement "
        "disk truth (approved design_brief.md) and hard layering rules in the system prompt.",
    ]
    total = 0
    for name in skill_names:
        body = _read_skill_pack(name)
        if not body:
            parts.append(f"\n_(Skill `{name}` not found on disk.)_")
            continue
        if total + len(body) > _MAX_SKILL_CHARS:
            parts.append(
                f"\n_(Skill `{name}` and later packs omitted — context budget. "
                f"Install/vendor under skills/.)_"
            )
            break
        parts.append("\n---\n")
        parts.append(body)
        total += len(body)
    return "\n".join(parts).strip()


def inject_skills_into_system(
    system: str,
    *,
    agent_name: str | None = None,
    user: str = "",
) -> str:
    """Append resolved skill packs to a system prompt."""
    names = skills_for_agent(agent_name, system, user)
    block = build_skills_block(names)
    if not block:
        return system
    base = (system or "").rstrip()
    return f"{base}\n\n{block}\n" if base else f"{block}\n"
