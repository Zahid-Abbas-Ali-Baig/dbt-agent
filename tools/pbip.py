"""Power BI PBIP helpers — relationships parse and scoped writes."""

from __future__ import annotations

import re
from pathlib import Path

from security_util import assert_under_project, is_under
from tools.registry import ToolResult

# fromColumn: 'marts fct_orders'.order_date_key  OR  Table.col
_FROM_TO_COL = re.compile(
    r"fromColumn:\s*(?:'([^']+)'|([^\s.]+))\.([^\s\n']+)"
    r".*?"
    r"toColumn:\s*(?:'([^']+)'|([^\s.]+))\.([^\s\n']+)",
    re.IGNORECASE | re.DOTALL,
)


def _pbip_root(project_dir: Path, bi_pbip_dir: str) -> Path:
    rel = (bi_pbip_dir or "powerbi-project").strip() or "powerbi-project"
    if Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise ValueError("BI_PBIP_DIR must be a relative path under the engagement folder")
    root = (project_dir / rel).resolve()
    assert_under_project(project_dir, root, label="BI_PBIP_DIR")
    return root


def _quote_tmdl_name(name: str) -> str:
    name = name.strip().strip("'")
    if re.search(r"[\s.]", name):
        return f"'{name}'"
    return name


def _logical_mart_name(pbi_table: str) -> str:
    """'marts fct_orders' / 'marts_fct_orders' → fct_orders."""
    t = pbi_table.strip().strip("'")
    for prefix in ("marts ", "marts_", "dbo ", "dbo_"):
        if t.lower().startswith(prefix):
            return t[len(prefix) :]
    return t


def _pick_relationships_path(root: Path) -> Path | None:
    candidates = list(root.rglob("relationships.tmdl"))
    if not candidates:
        # Prefer SemanticModel/definition with the most imported mart tables
        defs = list(root.rglob("definition"))
        scored: list[tuple[int, Path]] = []
        for d in defs:
            tables = d / "tables"
            if not tables.is_dir():
                continue
            n = sum(
                1
                for p in tables.glob("*.tmdl")
                if p.name.lower() != "_kpis.tmdl" and not p.name.lower().startswith("localdatetable")
            )
            scored.append((n, d / "relationships.tmdl"))
        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            return scored[0][1]
        return None

    def _score(p: Path) -> int:
        tables = p.parent / "tables"
        if not tables.is_dir():
            return 0
        return sum(
            1
            for x in tables.glob("*.tmdl")
            if x.name.lower() != "_kpis.tmdl" and not x.name.lower().startswith("localdatetable")
        )

    return max(candidates, key=_score)


def find_semantic_model_folder(project_dir: Path, bi_pbip_dir: str) -> Path | None:
    """Return …/<name>.SemanticModel that holds the richest mart import set."""
    try:
        root = _pbip_root(project_dir, bi_pbip_dir)
    except ValueError:
        return None
    rel_path = _pick_relationships_path(root)
    if not rel_path:
        return None
    # …/Name.SemanticModel/definition/relationships.tmdl → SemanticModel folder
    definition = rel_path.parent
    if definition.name.lower() == "definition":
        return definition.parent
    return definition


def list_imported_pbi_tables(project_dir: Path, bi_pbip_dir: str) -> dict[str, str]:
    """Map logical mart name (fct_orders) → Power BI table name (marts fct_orders)."""
    try:
        root = _pbip_root(project_dir, bi_pbip_dir)
    except ValueError:
        return {}
    rel_path = _pick_relationships_path(root)
    if not rel_path:
        return {}
    tables_dir = rel_path.parent / "tables"
    if not tables_dir.is_dir():
        return {}
    mapping: dict[str, str] = {}
    for path in tables_dir.glob("*.tmdl"):
        if path.name.lower() == "_kpis.tmdl" or path.name.lower().startswith("localdatetable"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"^table\s+'([^']+)'", text, re.MULTILINE | re.IGNORECASE)
        if not m:
            m = re.search(r"^table\s+([^\s\n]+)", text, re.MULTILINE | re.IGNORECASE)
        if not m:
            continue
        pbi_name = m.group(1).strip()
        logical = _logical_mart_name(pbi_name)
        mapping[logical.lower()] = pbi_name
        mapping[pbi_name.lower()] = pbi_name
    return mapping


def parse_brief_relationship_matrix(brief: str) -> list[dict[str, str]]:
    """Parse §8 'Power BI relationships' markdown table into edge dicts."""
    if not brief:
        return []
    lower = brief.lower()
    start = lower.find("power bi relationships")
    if start < 0:
        start = lower.find("### power bi relationships")
    if start < 0:
        return []
    chunk = brief[start:]
    end_markers = ("\n### ", "\n## ")
    end = len(chunk)
    for marker in end_markers:
        idx = chunk.find(marker, 80)
        if idx > 0:
            end = min(end, idx)
    chunk = chunk[:end]
    rows: list[dict[str, str]] = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip().strip("`") for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        from_t, to_t, from_c, to_c = cells[0], cells[1], cells[2], cells[3]
        if from_t.lower().startswith("from") or set(from_t) <= {"-", "—"}:
            continue
        if not from_t or not to_t or not from_c or not to_c:
            continue
        active = "yes"
        if len(cells) >= 5:
            active = cells[4].strip().lower()
        rows.append(
            {
                "from_table": from_t,
                "to_table": to_t,
                "from_col": from_c,
                "to_col": to_c,
                "active": "no" if active in ("no", "false", "inactive", "n") else "yes",
            }
        )
    return rows


def build_relationships_tmdl(
    project_dir: Path,
    bi_pbip_dir: str,
    brief: str,
) -> ToolResult:
    """Build valid TMDL from brief §8 matrix + imported Power BI table names."""
    imported = list_imported_pbi_tables(project_dir, bi_pbip_dir)
    matrix = parse_brief_relationship_matrix(brief)
    if not matrix:
        return ToolResult(
            ok=False,
            output="No Power BI relationships matrix found in design_brief.md §8",
            data={"relationships": [], "skipped": []},
        )
    if not imported:
        return ToolResult(
            ok=False,
            output="No imported mart tables found under BI_PBIP_DIR (Desktop import required)",
            data={"relationships": [], "skipped": []},
        )

    def _resolve(logical: str) -> str | None:
        key = logical.strip().strip("`").lower()
        if key in imported:
            return imported[key]
        # tolerate schema-qualified brief names
        for prefix in ("marts.", "marts ", "public."):
            if key.startswith(prefix):
                return imported.get(key[len(prefix) :])
        return None

    blocks: list[str] = []
    kept: list[dict[str, str]] = []
    skipped: list[str] = []
    for i, row in enumerate(matrix, start=1):
        from_pbi = _resolve(row["from_table"])
        to_pbi = _resolve(row["to_table"])
        if not from_pbi or not to_pbi:
            skipped.append(
                f"{row['from_table']}.{row['from_col']} → {row['to_table']}.{row['to_col']} "
                f"(missing import: from={from_pbi!r} to={to_pbi!r})"
            )
            continue
        name = (
            f"{_logical_mart_name(from_pbi)}_to_{_logical_mart_name(to_pbi)}"
            f"__{row['from_col']}"
        )
        # TMDL relationship names with spaces need quotes
        name_q = _quote_tmdl_name(name.replace(" ", "_"))
        lines = [f"relationship {name_q}"]
        if row.get("active") == "no":
            lines.append("\tisActive: false")
        lines.append(f"\tfromColumn: {_quote_tmdl_name(from_pbi)}.{row['from_col']}")
        lines.append(f"\ttoColumn: {_quote_tmdl_name(to_pbi)}.{row['to_col']}")
        blocks.append("\n".join(lines))
        kept.append(
            {
                "name": name,
                "from_table": from_pbi,
                "from_col": row["from_col"],
                "to_table": to_pbi,
                "to_col": row["to_col"],
                "active": row.get("active", "yes"),
            }
        )

    if not blocks:
        return ToolResult(
            ok=False,
            output="Could not map any brief §8 relationships to imported Power BI tables",
            data={"relationships": [], "skipped": skipped, "imported": sorted(set(imported.values()))},
        )

    content = "\n\n".join(blocks) + "\n"
    return ToolResult(
        ok=True,
        output=f"Built {len(blocks)} relationship(s); skipped {len(skipped)}",
        data={
            "content": content,
            "relationships": kept,
            "skipped": skipped,
            "count": len(blocks),
            "imported": sorted(set(imported.values())),
        },
    )


def pbip_read(project_dir: Path, bi_pbip_dir: str, rel_path: str) -> ToolResult:
    try:
        root = _pbip_root(project_dir, bi_pbip_dir)
    except ValueError as exc:
        return ToolResult(ok=False, output=str(exc))
    if Path(rel_path).is_absolute() or ".." in Path(rel_path).parts:
        return ToolResult(ok=False, output="Path escapes BI_PBIP_DIR")
    target = (root / rel_path).resolve()
    if not is_under(root, target):
        return ToolResult(ok=False, output="Path escapes BI_PBIP_DIR")
    if not target.exists():
        return ToolResult(ok=False, output=f"Not found: {rel_path}")
    return ToolResult(ok=True, output=target.read_text(encoding="utf-8"), data={"path": rel_path})


def sanitize_relationships_tmdl(content: str) -> str:
    """Keep only TMDL relationship blocks; drop LLM/agent prose and fences."""
    if not content:
        return ""
    text = content.replace("```tmdl", "").replace("```TMDL", "").replace("```", "")
    # Prefer content from the first real relationship keyword onward
    m0 = re.search(r"(?im)^relationship\b", text)
    if m0:
        text = text[m0.start() :]
    blocks: list[str] = []
    for m in re.finditer(
        r"(?ims)^relationship\b.*?(?=^relationship\b|\Z)",
        text,
    ):
        block = m.group(0).strip()
        if re.search(r"fromColumn\s*:", block, re.I) and re.search(r"toColumn\s*:", block, re.I):
            blocks.append(block)
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def _is_plausible_table_name(name: str) -> bool:
    n = (name or "").strip().strip("'")
    if not n or len(n) < 3:
        return False
    lower = n.lower()
    if lower in {"the", "a", "an", "and", "or", "to", "from", "definitions", "definitions."}:
        return False
    if lower.endswith(".") and lower[:-1] in {"definitions", "the", "a"}:
        return False
    # Mart-ish or dim/fct/bridge names
    if re.search(r"\b(dim_|fct_|bridge_|marts\b)", lower):
        return True
    return bool(re.match(r"^[A-Za-z_][\w ]*$", n)) and "_" in n


def pbip_write(project_dir: Path, bi_pbip_dir: str, rel_path: str, content: str) -> ToolResult:
    try:
        root = _pbip_root(project_dir, bi_pbip_dir)
    except ValueError as exc:
        return ToolResult(ok=False, output=str(exc))
    if Path(rel_path).is_absolute() or ".." in Path(rel_path).parts:
        return ToolResult(ok=False, output="Path escapes BI_PBIP_DIR")
    norm = rel_path.replace("\\", "/").lower()
    # Human owns mart table TMDL imports
    if "/tables/" in norm and norm.endswith(".tmdl"):
        base = Path(norm).name
        if base not in ("_kpis.tmdl",) and "relationship" not in base:
            return ToolResult(
                ok=False,
                output="Refusing write to mart tables/*.tmdl (human Desktop import owns these)",
            )
    if norm.endswith("relationships.tmdl"):
        cleaned = sanitize_relationships_tmdl(content)
        if not cleaned or "fromColumn:" not in cleaned:
            return ToolResult(
                ok=False,
                output=(
                    "Refusing relationships.tmdl write: content has no valid "
                    "relationship/fromColumn/toColumn TMDL blocks (prose-only rejected)"
                ),
            )
        content = cleaned
    target = (root / rel_path).resolve()
    if not is_under(root, target):
        return ToolResult(ok=False, output="Path escapes BI_PBIP_DIR")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return ToolResult(ok=True, output=f"Wrote PBIP {rel_path}", data={"path": rel_path})


def parse_pbip_relationships(project_dir: Path, bi_pbip_dir: str) -> ToolResult:
    """Parse relationships.tmdl for from/to table pairs (supports quoted names with spaces)."""
    try:
        root = _pbip_root(project_dir, bi_pbip_dir)
    except ValueError as exc:
        return ToolResult(ok=False, output=str(exc), data={"relationships": []})
    path = _pick_relationships_path(root)
    if path is None or not path.exists():
        return ToolResult(
            ok=False,
            output="relationships.tmdl not found under BI_PBIP_DIR",
            data={"relationships": []},
        )
    raw = path.read_text(encoding="utf-8")
    text = sanitize_relationships_tmdl(raw) or raw
    pairs: list[dict[str, str]] = []
    for m in _FROM_TO_COL.finditer(text):
        from_table = (m.group(1) or m.group(2) or "").strip()
        to_table = (m.group(4) or m.group(5) or "").strip()
        if not _is_plausible_table_name(from_table) or not _is_plausible_table_name(to_table):
            continue
        pairs.append(
            {
                "from_table": from_table,
                "from_col": m.group(3).strip(),
                "to_table": to_table,
                "to_col": m.group(6).strip(),
            }
        )

    tables = set()
    for p in pairs:
        tables.add(p.get("from_table", ""))
        tables.add(p.get("to_table", ""))
        # Also expose logical names for brief matching (dim_customers vs marts dim_customers)
        tables.add(_logical_mart_name(p.get("from_table", "")))
        tables.add(_logical_mart_name(p.get("to_table", "")))
    return ToolResult(
        ok=True,
        output=f"Found {len(pairs)} relationship(s) in {path.relative_to(project_dir)}",
        data={
            "path": str(path.relative_to(project_dir)).replace("\\", "/"),
            "relationships": pairs,
            "tables": sorted(t for t in tables if t),
            "raw_preview": text[:4000],
        },
    )
