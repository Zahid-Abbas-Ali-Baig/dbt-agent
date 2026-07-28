"""Orchestrator — phase routing, human gates, quality rework loops."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from agents import BiAgent, DiscoveryAgent, ModelingAgent, QualityLoopAgent, SemanticAgent
from llm import get_llm_client
from logging_util import log_event
from pipeline_state import load_state, save_state, sync_brief_status
from tools import build_registry
from tools.files import (
    INTAKE_CONFIG_KEYS,
    INTAKE_PROMPTS,
    apply_intake_defaults,
    approve_design_brief,
    init_engagement,
    missing_intake_keys,
    parse_config,
    requirements_incomplete,
    set_config_value,
    write_requirements_text,
)
from tools.warehouse import connection_fingerprint, test_warehouse

MAX_REWORK = 2

OFF_TOPIC_REDIRECT = (
    "I only handle this dbt + Power BI engagement (setup, warehouse, brief, models, BI).\n"
    "I can't help with that. Reply yes/no when I ask, or say continue / status / help."
)

# Topics allowed when the user asks a question (still short / flow-bound answers only).
_DOMAIN_TOKENS = (
    "dbt",
    "warehouse",
    "postgres",
    "schema",
    "source",
    "target",
    "staging",
    "intermediate",
    "mart",
    "brief",
    "design brief",
    "requirements",
    "config",
    "kpi",
    "power bi",
    "powerbi",
    "pbip",
    "tmdl",
    "relationship",
    "wireframe",
    "semantic",
    "brain",
    "cursor",
    "openai",
    "connection",
    "engagement",
    "intake",
    "codegen",
    "deps",
    "precheck",
    "environment",
    "mcp",
)

YES_WORDS = {"yes", "y", "ok", "okay", "approve", "approved", "go", "proceed", "confirm", "yep", "yeah"}
NO_WORDS = {"no", "n", "cancel", "stop", "nope", "hold"}

# Internal command -> user-facing description (never show phase numbers in chat).
ACTION_LABELS = {
    "init": "create the engagement workspace",
    "start": "collect what's still missing",
    "approve brief": "approve the design brief",
    "confirm warehouse": "confirm the warehouse link",
    "confirm brain": "confirm the brain link",
    "env precheck": "check and install the local environment",
    "run phase 0": "set up the dbt project",
            "run phase 1": "discover the schema, map data paths, and draft the design brief",
    "run phase 2": "define data sources",
    "run phase 3": "build staging models",
    "run phase 4": "build intermediate relationships",
    "run phase 5": "build marts",
    "run phase 6": "add semantic models and docs",
    "run phase 7": "validate the build",
    "run phase 8": "wire Power BI relationships and KPIs",
    "run phase 8 visuals": "build Power BI visuals",
    "run all": "continue the full delivery",
    "Desktop import OK": "confirm Power BI .pbip import is saved under BI_PBIP_DIR",
    "Wireframe approved": "approve the wireframe",
    "Phase 8 OK": "sign off the Power BI delivery",
}


class Orchestrator:
    def __init__(self, project_dir: Path):
        from security_util import validate_engagement_dir

        self.project_dir = validate_engagement_dir(project_dir)
        try:
            self.llm = get_llm_client(cwd=str(self.project_dir))
        except Exception:  # noqa: BLE001
            self.llm = None
        self.registry = build_registry(self.project_dir)
        self.state = sync_brief_status(self.project_dir, load_state(self.project_dir))
        self.discovery = DiscoveryAgent(self.llm, self.registry, self.project_dir)
        self.modeling = ModelingAgent(self.llm, self.registry, self.project_dir)
        self.semantic = SemanticAgent(self.llm, self.registry, self.project_dir)
        self.bi = BiAgent(self.llm, self.registry, self.project_dir)
        self.quality = QualityLoopAgent(self.llm, self.registry, self.project_dir)
        self._pending_confirm: dict[str, Any] | None = None

    def _save(self) -> None:
        save_state(self.project_dir, self.state)

    def status_text(self) -> str:
        self.state = sync_brief_status(self.project_dir, self.state)
        self._sync_warehouse_fingerprint()
        cfg = parse_config(self.project_dir)
        name = (cfg.data or {}).get("PROJECT_NAME", "")
        if name and "{{" in str(name):
            name = ""
        guidance, _ = self.next_step()
        pending = self.state.pending_approval
        parts = []
        if name:
            parts.append(f"Project: {name}")
        parts.append(
            f"Brain: {'confirmed' if self.state.brain_confirmed else 'set in .env (OpenAI/Cursor)'}"
        )
        if self.state.path_confidence_threshold is not None:
            parts.append(f"Path confidence gate: {self.state.path_confidence_threshold}%")
        parts.append(
            f"Warehouse: {'confirmed' if self.state.warehouse_confirmed else 'not confirmed yet'}"
        )
        parts.append(f"Where we are: {guidance}")
        if self.state.intake_field:
            parts.append("Waiting for your answer in chat.")
        elif pending:
            label = pending.get("label") or ACTION_LABELS.get(
                str(pending.get("command") or ""), "next action"
            )
            parts.append(f"Waiting for yes/no: {label}")
        q = self.state.quality
        if q.last_checkpoint and q.status and q.status != "pass":
            parts.append("Quality check needs attention.")
        return "\n".join(parts)

    def _sync_warehouse_fingerprint(self) -> None:
        """Invalidate warehouse confirmation if connection details changed."""
        if missing_intake_keys(self.project_dir):
            return
        fp = (
            connection_fingerprint(self.project_dir, "source")
            + "||"
            + connection_fingerprint(self.project_dir, "target")
        )
        if self.state.warehouse_fingerprint and self.state.warehouse_fingerprint != fp:
            self.state.warehouse_confirmed = False
            self.state.warehouse_fingerprint = None
            self._save()

    def verify_brain(self) -> str:
        """Probe the LLM brain configured in .env (human wires keys manually)."""
        from activity_log import push_activity

        provider = (os.getenv("LLM_PROVIDER") or "").strip() or "(unset)"
        model = (os.getenv("MODEL") or "").strip() or "(unset)"
        push_activity("Checking brain link…", kind="brain")
        if self.llm is None:
            try:
                self.llm = get_llm_client(cwd=str(self.project_dir))
            except Exception as exc:  # noqa: BLE001
                self.state.brain_confirmed = False
                self._save()
                return (
                    f"Brain not configured ({provider}/{model}): {exc}\n"
                    "Use the brain form below (Cursor is enabled for now)."
                )
        try:
            from activity_log import clear_thinking, set_thinking

            set_thinking(
                "Waiting for model stream…",
                topic=f"link check ({provider}/{model})",
                phase="thinking",
            )

            def on_event(event: str, payload: dict) -> None:
                if event == "thinking":
                    set_thinking(
                        str(payload.get("text") or ""),
                        topic=f"link check ({provider}/{model})",
                        phase="thinking",
                    )
                elif event == "assistant":
                    set_thinking(
                        str(payload.get("text") or ""),
                        topic=f"link check ({provider}/{model})",
                        phase="writing",
                    )

            push_activity(
                "Brain thinking: link check (ping)",
                kind="brain",
                detail=f"provider={provider} model={model}",
            )
            try:
                reply = self.llm.chat(
                    [
                        {"role": "system", "content": "Reply with exactly: BRAIN_OK"},
                        {"role": "user", "content": "Ping"},
                    ],
                    max_tokens=24,
                    on_event=on_event,
                )
            except TypeError:
                reply = self.llm.chat(
                    [
                        {"role": "system", "content": "Reply with exactly: BRAIN_OK"},
                        {"role": "user", "content": "Ping"},
                    ],
                    max_tokens=24,
                )
            clear_thinking()
            push_activity(
                "Brain ping complete",
                kind="brain",
                detail=(reply or "")[:120],
            )
        except Exception as exc:  # noqa: BLE001
            try:
                from activity_log import clear_thinking

                clear_thinking()
            except Exception:  # noqa: BLE001
                pass
            self.state.brain_confirmed = False
            self._save()
            return (
                f"Brain link failed ({provider}/{model}): {exc}\n"
                "Use the brain form below to fix provider / model / API key."
            )
        if "BRAIN_OK" not in (reply or ""):
            self.state.brain_confirmed = False
            self._save()
            return (
                f"Brain responded unexpectedly ({provider}/{model}). "
                "Check MODEL / API key in the brain form."
            )
        self.state.brain_confirmed = True
        self._save()
        return f"Brain link confirmed: {provider}/{model}."

    def verify_warehouse(self, *, ask_human: bool = True) -> str:
        """Live-test SOURCE then TARGET warehouse; ask human to confirm."""
        from activity_log import push_activity

        self._sync_warehouse_fingerprint()
        parts: list[str] = []

        push_activity("Probing SOURCE warehouse…", kind="tool", agent="warehouse")
        source = test_warehouse(self.project_dir, "source")
        push_activity(
            "SOURCE probe " + ("ok" if source.ok else "failed"),
            kind="tool" if source.ok else "error",
            agent="warehouse",
            detail=(source.output or "")[:200],
        )
        parts.append(source.output)
        if not source.ok:
            self.state.warehouse_confirmed = False
            self.state.warehouse_fingerprint = None
            self.state.intake_field = "CONNECTION_FORM"
            self.state.pending_approval = None
            self._save()
            parts.append(
                "\nSOURCE warehouse failed. Update the connection form and save again."
            )
            return "\n".join(parts)

        push_activity("Probing TARGET warehouse…", kind="tool", agent="warehouse")
        target = test_warehouse(self.project_dir, "target")
        push_activity(
            "TARGET probe " + ("ok" if target.ok else "failed"),
            kind="tool" if target.ok else "error",
            agent="warehouse",
            detail=(target.output or "")[:200],
        )
        parts.append(target.output)
        if not target.ok:
            self.state.warehouse_confirmed = False
            self.state.warehouse_fingerprint = None
            self.state.intake_field = "CONNECTION_FORM"
            self.state.pending_approval = None
            self._save()
            parts.append(
                "\nSOURCE is OK, but TARGET failed. Update TARGET settings in the connection form."
            )
            return "\n".join(parts)

        try:
            from tools.warehouse import mcp_postgres_tool

            mcp = mcp_postgres_tool(self.project_dir, "connect_db")
            if mcp.ok:
                parts.append("Postgres MCP link confirmed (discovery tools ready).")
            else:
                parts.append(
                    "Postgres MCP not reachable — native warehouse tools will be used "
                    f"({mcp.output[:200]})."
                )
        except Exception as exc:  # noqa: BLE001
            parts.append(f"Postgres MCP skipped: {exc}")

        fp = (
            connection_fingerprint(self.project_dir, "source")
            + "||"
            + connection_fingerprint(self.project_dir, "target")
        )
        self.state.warehouse_fingerprint = fp
        self.state.last_artifacts["warehouse_probe"] = {
            "source": source.data,
            "target": target.data,
        }
        if ask_human:
            self.state.pending_approval = {
                "command": "confirm warehouse",
                "label": ACTION_LABELS["confirm warehouse"],
            }
            self.state.intake_field = None
            self._save()
            parts.append("\nDo these warehouse links look right?")
            return "\n".join(parts)

        self.state.warehouse_confirmed = True
        self._save()
        return "\n".join(parts)

    def _bi_pbip_dir(self) -> str:
        cfg = parse_config(self.project_dir)
        return str((cfg.data or {}).get("BI_PBIP_DIR") or "powerbi-project").strip() or "powerbi-project"

    def _marts_schema(self) -> str:
        cfg = parse_config(self.project_dir)
        return str((cfg.data or {}).get("MARTS_SCHEMA") or "marts").strip() or "marts"

    def desktop_import_instructions(self) -> str:
        """Human gate: exact folder + PBIP format for Power BI Desktop import."""
        bi_dir = self._bi_pbip_dir()
        marts = self._marts_schema()
        folder = (self.project_dir / bi_dir).resolve()
        return (
            "Human gate — Power BI Desktop import\n\n"
            f"**Store here:** `{folder}`\n"
            f"(engagement-relative: `{bi_dir}/` from config `BI_PBIP_DIR`)\n\n"
            "**Format:** Power BI Project **`.pbip`** — not `.pbix`.\n"
            f"Save the `.pbip` **inside** that folder. Desktop will also create "
            "sibling `.Report/` and `.SemanticModel/` folders there.\n\n"
            f"**Data:** Get data from the target warehouse → schema `{marts}` "
            "(mart tables: `fct_` / `dim_` / `bridge_`). Refresh so tables load.\n\n"
            "Save the PBIP again, then reply **yes** (or say `Desktop import OK`)."
        )

    def next_step(self) -> tuple[str, str | None]:
        """Return human guidance and an optional internal command (hidden from UI)."""
        self.state = sync_brief_status(self.project_dir, self.state)
        self._sync_warehouse_fingerprint()
        cfg_path = self.project_dir / "config.md"
        req_path = self.project_dir / "requirements.md"
        brief_path = self.project_dir / "design_brief.md"

        if not cfg_path.exists() or not req_path.exists():
            return ("I need to create the engagement workspace first.", "init")

        if missing_intake_keys(self.project_dir):
            return ("I still need a few connection details from you.", "start")

        if not self.state.warehouse_confirmed:
            return (
                "I need to verify the warehouse link next.",
                "verify warehouse",
            )

        if requirements_incomplete(self.project_dir):
            return ("I still need your business requirements.", "start")

        if self.state.intake_field == "PATH_CLARIFY":
            pc = self.state.path_clarify or {}
            qs = pc.get("questions") or []
            idx = int(pc.get("index") or 0)
            total = len(qs)
            if total:
                return (
                    f"I'm clarifying data paths with you (question {min(idx + 1, total)} of {total}).",
                    None,
                )
            return ("I'm clarifying data paths with you.", None)

        if self.state.intake_field == "TABLE_SCOPE":
            ts = self.state.table_scope or {}
            n = len(ts.get("in_scope") or [])
            return (
                f"I'm waiting for you to confirm the analytics table scope ({n} proposed).",
                None,
            )

        if not (self.project_dir / "dbt_project.yml").exists():
            if not self.state.env_precheck_ok:
                return (
                    "Next I'll check that dbt and the other tools this engagement needs are installed.",
                    "env precheck",
                )
            return ("Next I'll set up the dbt project.", "run phase 0")

        if not brief_path.exists() or self.state.current_phase < 1:
            return (
                "Next I'll discover the schema, map data paths, and draft the design brief.",
                "run phase 1",
            )

        if self.state.brief_status != "approved":
            return (
                "Please review the design brief. Reply yes to approve it so I can continue.",
                "approve brief",
            )

        if self.state.current_phase < 2:
            return ("Next I'll define the data sources.", "run phase 2")
        if self.state.current_phase < 3:
            return ("Next I'll build staging models.", "run phase 3")
        if self.state.current_phase < 4:
            return ("Next I'll build intermediate relationships.", "run phase 4")
        if self.state.current_phase < 5:
            return ("Next I'll build marts.", "run phase 5")
        if self.state.current_phase < 6:
            return ("Next I'll add semantic models and docs.", "run phase 6")
        if self.state.current_phase < 7:
            return ("Next I'll validate the build.", "run phase 7")
        if not self.state.desktop_import_ok:
            return (self.desktop_import_instructions(), None)
        if not self.state.phase8_relationships_done:
            return ("Next I'll wire Power BI relationships and KPIs.", "run phase 8")
        if self.state.quality.last_checkpoint == "Q8a" and self.state.quality.status != "pass":
            return ("Relationships need a fix. I'll rework that next.", "run phase 8")
        if not self.state.wireframe_approved:
            return (
                "Please review the wireframe. Reply yes to approve it.",
                None,
            )
        if not self.state.phase8_visuals_done:
            return ("Next I'll build the Power BI visuals.", "run phase 8 visuals")
        if self.state.quality.last_checkpoint == "Q8b" and self.state.quality.status != "pass":
            return ("Visuals need a fix. I'll rework that next.", "run phase 8 visuals")
        if not self.state.phase8_ok:
            return (
                "Please verify the Power BI project, then reply yes to sign off.",
                None,
            )
        return ("Delivery looks complete. You can start a new engagement or share feedback.", None)

    def explain_config(self) -> str:
        """Explain required connection details in plain language."""
        return (
            "I'll ask for SOURCE and TARGET warehouse details:\n"
            "- SOURCE = where landed tables already live\n"
            "- TARGET = where dbt writes models (often the same Postgres)\n"
            "- Warehouse type is dynamic later; v1 supports postgres only\n\n"
            "Say continue and I'll collect whatever is still missing."
        )

    def explain_requirements(self) -> str:
        return (
            "I need business context only (no SQL/table lists):\n"
            "- domain summary\n"
            "- goals and pain points\n"
            "- questions the report must answer\n"
            "- source systems at a high level\n"
            "- reporting preferences\n\n"
            "Say continue and paste that when I ask."
        )

    def start_intake(self) -> str:
        """Ask for the next missing input, or propose the next action."""
        parts: list[str] = []

        cfg_path = self.project_dir / "config.md"
        req_path = self.project_dir / "requirements.md"
        if not cfg_path.exists() or not req_path.exists():
            self.state.pending_approval = {
                "command": "init",
                "label": ACTION_LABELS["init"],
            }
            self.state.intake_field = None
            self._save()
            msg = (
                "I need a workspace folder before we start.\n"
                "Create the engagement kit here?"
            )
            return ("\n\n".join(parts + [msg]) if parts else msg)

        # 1) Always ask brain via dropdown menu — do not auto-skip from .env
        if not self.state.brain_confirmed:
            self.state.intake_field = "BRAIN_FORM"
            self.state.pending_approval = None
            self._save()
            return (
                "First — which brain should I use?\n"
                "Pick a provider from the dropdown (Cursor and OpenAI are available)."
            )

        # 2) Path confidence threshold (when to ask clarifying questions in discovery)
        if not self.state.path_confidence_confirmed:
            self._sync_path_confidence_from_config(prefer_existing=True)
            self.state.intake_field = "CONFIDENCE_FORM"
            self.state.pending_approval = None
            self._save()
            return (
                "Next — how picky should discovery path questions be?\n"
                "Paths scored below this % will pause for your answer before I draft the brief."
            )

        apply_intake_defaults(self.project_dir)
        missing = missing_intake_keys(self.project_dir)

        # 3) Ask warehouse type via dropdown before credentials
        # Use raw missing keys — parse_config defaults must not skip this step
        if "SOURCE_WAREHOUSE_TYPE" in missing:
            self.state.intake_field = "WAREHOUSE_TYPE_FORM"
            self.state.pending_approval = None
            self._save()
            return (
                "Next — which source warehouse should I use?\n"
                "Pick a type from the dropdown (Postgres is available now)."
            )

        cfg = parse_config(self.project_dir).data or {}
        wh_type = (cfg.get("SOURCE_WAREHOUSE_TYPE") or "").strip() or "postgres"

        # 4) Connection credentials form for the selected type
        if missing:
            self.state.intake_field = "CONNECTION_FORM"
            self.state.pending_approval = None
            self._save()
            return (
                f"Got it — source is `{wh_type}`.\n"
                "Fill in the connection details below."
            )

        if not self.state.warehouse_confirmed:
            probe = self.verify_warehouse(ask_human=True)
            return ("\n\n".join(parts + [probe]) if parts else probe)

        if requirements_incomplete(self.project_dir):
            self.state.intake_field = "REQUIREMENTS"
            self.state.pending_approval = None
            self._save()
            msg = INTAKE_PROMPTS["REQUIREMENTS"]
            return ("\n\n".join(parts + [msg]) if parts else msg)

        self.state.intake_field = None
        self._save()
        nxt = self.propose_next_step()
        return ("\n\n".join(parts + [nxt]) if parts else nxt)

    def propose_next_step(self) -> str:
        """Describe next action in plain language and ask yes/no before running."""
        guidance, suggested = self.next_step()
        if suggested == "start":
            return self.start_intake()
        if suggested == "verify warehouse":
            return self.verify_warehouse(ask_human=True)
        if not suggested:
            self.state.pending_approval = None
            self._save()
            if (
                ("Power BI Desktop" in guidance or "`.pbip`" in guidance or ".pbip" in guidance)
                and not self.state.desktop_import_ok
            ):
                self.state.pending_approval = {
                    "command": "Desktop import OK",
                    "label": ACTION_LABELS["Desktop import OK"],
                }
                self._save()
                return guidance
            if "wireframe" in guidance.lower() and not self.state.wireframe_approved:
                self.state.pending_approval = {
                    "command": "Wireframe approved",
                    "label": ACTION_LABELS["Wireframe approved"],
                }
                self._save()
                return guidance
            if "sign off" in guidance.lower() and not self.state.phase8_ok:
                self.state.pending_approval = {
                    "command": "Phase 8 OK",
                    "label": ACTION_LABELS["Phase 8 OK"],
                }
                self._save()
                return guidance
            return guidance

        label = ACTION_LABELS.get(suggested, guidance)
        self.state.pending_approval = {
            "command": suggested,
            "label": label,
        }
        self._save()
        return f"{guidance}\n\nShall I continue?"

    def get_ui_prompt(self) -> dict[str, Any] | None:
        """Structured prompt for the chat UI (forms, yes/no, choices, or free text)."""
        from tools.forms import (
            brain_form_schema,
            confidence_form_schema,
            connection_form_schema,
            connection_form_values,
            warehouse_type_form_schema,
        )

        pending = self.state.pending_approval
        if pending:
            label = pending.get("label") or ACTION_LABELS.get(
                str(pending.get("command") or ""), "continue"
            )
            return {
                "kind": "yes_no",
                "question": f"Ready to {label}?",
                "choices": [
                    {"value": "yes", "label": "Yes, go ahead"},
                    {"value": "no", "label": "Not yet"},
                ],
            }

        field = self.state.intake_field
        if not field:
            return None

        if field == "BRAIN_FORM":
            return {
                "kind": "brain_form",
                "question": "Which brain should I use?",
                "schema": brain_form_schema(),
            }

        if field == "CONFIDENCE_FORM":
            self._sync_path_confidence_from_config(prefer_existing=True)
            return {
                "kind": "confidence_form",
                "question": "Path confidence threshold for clarifying questions?",
                "schema": confidence_form_schema(
                    current=self.state.path_confidence_threshold
                ),
            }

        if field == "TABLE_SCOPE":
            ts = self.state.table_scope or {}
            n = len(ts.get("in_scope") or [])
            deferred_n = len(ts.get("deferred") or [])
            noise_n = len(ts.get("noise") or [])
            noise_hint = ""
            if deferred_n:
                noise_hint = (
                    f" Chat lists {deferred_n} deferred/noise table(s)"
                    + (f" in {noise_n} reason group(s)" if noise_n else "")
                    + " — review those reasons before accepting."
                )
            return {
                "kind": "text",
                "question": (
                    f"Reply yes to accept the proposed {n} in-scope tables, "
                    "or paste a revised comma/newline-separated list "
                    "(include any deferred tables you want kept)."
                    f"{noise_hint}"
                ),
                "choices": [
                    {"value": "yes", "label": "Yes — accept proposed scope"},
                ],
            }

        if field == "PATH_CLARIFY":
            pc = self.state.path_clarify or {}
            questions = list(pc.get("questions") or [])
            idx = int(pc.get("index") or 0)
            total = len(questions)
            q = questions[idx] if 0 <= idx < total and isinstance(questions[idx], dict) else {}
            body = str(q.get("question") or "").strip()
            remaining = max(0, total - idx - 1)
            header = (
                f"Question {idx + 1} of {total} ({remaining} remaining)"
                if total
                else "Path clarification"
            )
            return {
                "kind": "text",
                "question": f"{header}\n\n{body}" if body else header,
                "choices": [],
            }

        if field == "WAREHOUSE_TYPE_FORM":
            return {
                "kind": "warehouse_type_form",
                "question": "Which source warehouse should I use?",
                "schema": warehouse_type_form_schema(values=connection_form_values(self.project_dir)),
            }

        if field == "CONNECTION_FORM":
            return {
                "kind": "connection_form",
                "question": "Enter the connection details",
                "schema": connection_form_schema(values=connection_form_values(self.project_dir)),
            }

        question = INTAKE_PROMPTS.get(field, f"Enter a value for {field}")

        if field == "TARGET_SAME_AS_SOURCE":
            return {
                "kind": "choice",
                "question": question,
                "choices": [
                    {"value": "yes", "label": "Yes — same as SOURCE"},
                    {"value": "no", "label": "No — separate TARGET"},
                ],
            }

        if field in ("SOURCE_WAREHOUSE_TYPE", "TARGET_WAREHOUSE_TYPE", "WAREHOUSE_TYPE"):
            return {
                "kind": "choice",
                "question": question,
                "choices": [{"value": t, "label": t} for t in SUPPORTED_WAREHOUSE_TYPES],
            }

        return {
            "kind": "text",
            "question": question,
            "choices": [],
        }

    def apply_warehouse_type_menu(self, payload: dict[str, Any]) -> str:
        from tools.forms import apply_warehouse_type_form

        res = apply_warehouse_type_form(self.project_dir, payload)
        if not res.ok:
            self.state.intake_field = "WAREHOUSE_TYPE_FORM"
            self._save()
            return res.output
        self.state.warehouse_confirmed = False
        self.state.warehouse_fingerprint = None
        self.state.intake_field = "CONNECTION_FORM"
        self._save()
        src = (res.data or {}).get("source_type", "postgres")
        return (
            f"{res.output}\n\n"
            f"Now enter the `{src}` connection details below."
        )

    def apply_confidence_menu(self, payload: dict[str, Any]) -> str:
        from tools.forms import apply_confidence_form

        res = apply_confidence_form(self.project_dir, payload)
        if not res.ok:
            self.state.intake_field = "CONFIDENCE_FORM"
            self.state.path_confidence_confirmed = False
            self._save()
            return res.output
        threshold = int((res.data or {}).get("threshold") or 70)
        self.state.path_confidence_threshold = threshold
        self.state.path_confidence_confirmed = True
        self.state.intake_field = None
        self._save()
        return (
            f"{res.output}\n"
            f"I'll ask about data paths scored below {threshold}%.\n\n"
            + self.start_intake()
        )

    def _sync_path_confidence_from_config(self, *, prefer_existing: bool = False) -> int | None:
        """Load PATH_CONFIDENCE_THRESHOLD from config.md into state when present."""
        if prefer_existing and self.state.path_confidence_threshold is not None:
            return self.state.path_confidence_threshold
        cfg = parse_config(self.project_dir).data or {}
        raw = cfg.get("PATH_CONFIDENCE_THRESHOLD")
        if raw is None or str(raw).strip() == "" or "{{" in str(raw):
            return self.state.path_confidence_threshold
        try:
            threshold = max(1, min(100, int(str(raw).strip().rstrip("%"))))
        except (TypeError, ValueError):
            return self.state.path_confidence_threshold
        self.state.path_confidence_threshold = threshold
        self._save()
        return threshold

    def _path_confidence_threshold(self) -> int:
        from tools.forms import DEFAULT_PATH_CONFIDENCE_THRESHOLD

        self._sync_path_confidence_from_config(prefer_existing=True)
        val = self.state.path_confidence_threshold
        if val is None:
            return DEFAULT_PATH_CONFIDENCE_THRESHOLD
        return max(1, min(100, int(val)))

    def apply_connection_menu(self, payload: dict[str, Any]) -> str:
        from tools.forms import apply_connection_form

        res = apply_connection_form(self.project_dir, payload)
        if not res.ok:
            self.state.intake_field = "CONNECTION_FORM"
            self._save()
            return res.output
        self.state.warehouse_confirmed = False
        self.state.warehouse_fingerprint = None
        self.state.intake_field = None
        self._save()
        return res.output + "\n\n" + self.verify_warehouse(ask_human=True)

    def apply_brain_menu(self, payload: dict[str, Any]) -> str:
        from tools.forms import apply_brain_form

        agent_root = Path(__file__).resolve().parent
        res = apply_brain_form(agent_root, payload)
        if not res.ok:
            self.state.intake_field = "BRAIN_FORM"
            self._save()
            return res.output
        # Recreate LLM client + specialists with new env
        self.llm = get_llm_client(cwd=str(self.project_dir))
        self.discovery = DiscoveryAgent(self.llm, self.registry, self.project_dir)
        self.modeling = ModelingAgent(self.llm, self.registry, self.project_dir)
        self.semantic = SemanticAgent(self.llm, self.registry, self.project_dir)
        self.bi = BiAgent(self.llm, self.registry, self.project_dir)
        self.quality = QualityLoopAgent(self.llm, self.registry, self.project_dir)
        self.state.brain_confirmed = False
        self.state.intake_field = None
        self._save()
        probe = self.verify_brain()
        if not self.state.brain_confirmed:
            self.state.intake_field = "BRAIN_FORM"
            self._save()
            return probe + "\n\nFix the brain form and save again."
        return probe + "\n\n" + self.start_intake()

    def _is_yes(self, lower: str) -> bool:
        return lower in YES_WORDS or lower.startswith("yes ")

    def _is_no(self, lower: str) -> bool:
        return lower in NO_WORDS or lower.startswith("no ")

    def _execute_command(self, command: str) -> str:
        """Run an internal command without re-asking approval."""
        cmd = (command or "").strip()
        lower = cmd.lower()

        if lower == "init" or lower.startswith("init "):
            force = "force" in lower
            res = init_engagement(self.project_dir, force=force)
            log_event("INFO", "init", ok=res.ok, project=str(self.project_dir))
            return "Workspace ready.\n\n" + self.start_intake()

        if lower in ("start", "begin", "setup"):
            return self.start_intake()

        if lower == "verify warehouse":
            return self.verify_warehouse(ask_human=True)

        if lower == "confirm warehouse":
            fp = (
                connection_fingerprint(self.project_dir, "source")
                + "||"
                + connection_fingerprint(self.project_dir, "target")
            )
            self.state.warehouse_confirmed = True
            self.state.warehouse_fingerprint = fp
            self._save()
            follow = self.start_intake() if requirements_incomplete(self.project_dir) else self.propose_next_step()
            return "Warehouse links confirmed by you.\n\n" + follow

        if lower == "confirm brain":
            msg = self.verify_brain()
            return msg + "\n\n" + self.propose_next_step()

        if lower in ("env precheck", "check env", "precheck"):
            return self.run_env_precheck(auto_install=True)

        if lower == "approve brief":
            res = approve_design_brief(self.project_dir)
            self.state = sync_brief_status(self.project_dir, self.state)
            self._save()
            if not res.ok:
                return res.output
            return "Design brief approved.\n\n" + self.propose_next_step()

        if "desktop import ok" in lower:
            self.state.desktop_import_ok = True
            self._save()
            return "Got it — Desktop import confirmed.\n\n" + self.propose_next_step()

        if "wireframe approved" in lower:
            self.state.wireframe_approved = True
            self._save()
            return "Got it — wireframe approved.\n\n" + self.propose_next_step()

        if "phase 8 ok" in lower or lower == "phase8 ok":
            self.state.phase8_ok = True
            self._save()
            return "Signed off. Power BI delivery is complete."

        m = re.match(r"run\s+phase\s+(\d+)\s*(visuals)?", lower)
        if m:
            phase = int(m.group(1))
            visuals = bool(m.group(2))
            out = self.run_phase(phase, visuals=visuals)
            if self.state.intake_field in ("PATH_CLARIFY", "TABLE_SCOPE"):
                return out
            return out + "\n\n" + self.propose_next_step()

        if lower in ("run all", "run pipeline"):
            out = self.run_all()
            if self.state.intake_field in ("PATH_CLARIFY", "TABLE_SCOPE"):
                return out
            return out + "\n\n" + self.propose_next_step()

        return "I couldn't run that action. Tell me to continue and I'll try again."

    def _handle_approval_reply(self, lower: str) -> str | None:
        pending = self.state.pending_approval
        if not pending:
            return None
        if self._is_yes(lower):
            command = str(pending.get("command") or "")
            label = pending.get("label") or ACTION_LABELS.get(command, "that")
            self.state.pending_approval = None
            self._save()
            return f"Okay — I'll {label}.\n\n" + self._execute_command(command)
        if self._is_no(lower):
            command = str(pending.get("command") or "")
            self.state.pending_approval = None
            self._save()
            if command == "confirm warehouse":
                self.state.warehouse_confirmed = False
                self.state.warehouse_fingerprint = None
                self.state.intake_field = "CONNECTION_FORM"
                self._save()
                return (
                    "Okay — warehouse not confirmed. Update the connection form and save again."
                )
            return "Okay, paused. Say yes when you want me to continue, or tell me what changed."
        return None

    def _handle_intake_answer(self, msg: str, lower: str) -> str | None:
        field = self.state.intake_field
        if not field:
            return None
        # Forms are submitted via dedicated API endpoints, not chat text
        if field in ("CONNECTION_FORM", "BRAIN_FORM", "WAREHOUSE_TYPE_FORM", "CONFIDENCE_FORM"):
            if lower in (
                "start",
                "begin",
                "setup",
                "continue",
                "help",
                "?",
                "status",
                "cancel",
            ):
                return None
            return (
                "Use the form below and click Continue — chat text won't fill those settings."
            )

        # Path clarifications: one answer at a time; don't let "continue" skip the queue
        if field == "PATH_CLARIFY":
            if lower in ("help", "?", "status", "cancel"):
                return None
            if lower in (
                "start",
                "begin",
                "setup",
                "continue",
                "next",
                "what next",
                "init",
            ) or lower.startswith("run "):
                q = DiscoveryAgent.format_clarify_question(self.state.path_clarify or {})
                return q or "Answer the open path question first (or say cancel)."
            if not msg.strip():
                return "I need an answer to continue."
            return self._apply_path_clarify_answer(msg.strip())

        if field == "TABLE_SCOPE":
            if lower in ("help", "?", "status", "cancel"):
                return None
            if lower in (
                "start",
                "begin",
                "setup",
                "continue",
                "next",
                "what next",
                "init",
            ) or lower.startswith("run "):
                return (
                    "Confirm the table scope first: reply yes to accept the proposed list, "
                    "or paste a revised list of table names."
                )
            return self._apply_table_scope_answer(msg.strip())

        # Only short control commands escape intake. Do NOT run config/requirements
        # keyword detectors here — pasted docs often contain words like
        # "credentials" or "requirements.md" and must still be saved as answers.
        if lower in (
            "start",
            "begin",
            "setup",
            "continue",
            "help",
            "?",
            "status",
            "init",
            "what next",
            "next",
            "cancel",
        ) or lower.startswith("run "):
            return None
        if self.state.pending_approval and (self._is_yes(lower) or self._is_no(lower)):
            return None

        if field == "REQUIREMENTS":
            if len(msg.strip()) < 40:
                return (
                    "That looks too short. Paste the full business requirements "
                    "(domain, goals, pain points, and questions), or say cancel to stop for now."
                )
            write_requirements_text(self.project_dir, msg)
            self.state.intake_field = None
            self._save()
            return "Thanks — I saved the requirements.\n\n" + self.propose_next_step()

        # Config field: allow "KEY: value" or bare value
        value = msg.strip()
        m = re.match(rf"^{re.escape(field)}\s*:\s*(.+)$", value, re.IGNORECASE)
        if m:
            value = m.group(1).strip()
        # Also accept "key = value"
        m2 = re.match(rf"^{re.escape(field)}\s*=\s*(.+)$", value, re.IGNORECASE)
        if m2:
            value = m2.group(1).strip()
        if not value or "{{" in value:
            return f"I need a real value. {INTAKE_PROMPTS.get(field, '')}"

        res = set_config_value(self.project_dir, field, value)
        if not res.ok:
            return res.output

        # Any connection change needs a fresh warehouse probe
        if field in INTAKE_CONFIG_KEYS:
            self.state.warehouse_confirmed = False
            self.state.warehouse_fingerprint = None

        apply_intake_defaults(self.project_dir)
        missing = missing_intake_keys(self.project_dir)
        if missing:
            nxt = missing[0]
            self.state.intake_field = nxt
            self._save()
            return f"Got it.\n\n{INTAKE_PROMPTS[nxt]}"

        if requirements_incomplete(self.project_dir):
            # Config complete → probe warehouse before requirements
            if not self.state.warehouse_confirmed:
                self.state.intake_field = None
                self._save()
                return "Got it — connection details look complete.\n\n" + self.verify_warehouse(
                    ask_human=True
                )
            self.state.intake_field = "REQUIREMENTS"
            self._save()
            return f"Got it — connection details look complete.\n\n{INTAKE_PROMPTS['REQUIREMENTS']}"

        self.state.intake_field = None
        self._save()
        if not self.state.warehouse_confirmed:
            return "Got it.\n\n" + self.verify_warehouse(ask_human=True)
        return "Got it.\n\n" + self.propose_next_step()

    def _apply_table_scope_answer(self, answer: str) -> str:
        """Accept or revise proposed in-scope tables, then describe/profile/brief."""
        from tools.scope import parse_scope_reply

        ts = dict(self.state.table_scope or {})
        all_tables = list(ts.get("all_tables") or [])
        proposed = list(ts.get("in_scope") or [])
        if not all_tables and not proposed:
            self.state.intake_field = None
            self.state.table_scope = None
            self._save()
            return "No proposed table scope on file. Say continue and I'll re-run discovery."

        parsed = parse_scope_reply(answer, all_tables or proposed)
        if parsed is None:
            # yes / accept
            in_scope = proposed
        else:
            in_scope = parsed
            if not in_scope:
                return (
                    "I couldn't match those names to the schema. "
                    "Reply yes to accept the proposal, or paste exact table names."
                )

        known = set(all_tables) if all_tables else set(in_scope)
        in_scope = [t for t in in_scope if t in known] or in_scope
        deferred = [t for t in all_tables if t not in set(in_scope)] if all_tables else []
        ts["in_scope"] = in_scope
        ts["deferred"] = deferred
        ts["confirmed"] = True
        self.state.table_scope = ts
        self.state.intake_field = None
        self._save()

        from activity_log import push_activity

        push_activity(
            f"Table scope confirmed ({len(in_scope)} in-scope) — profiling",
            kind="info",
            agent="discovery",
        )
        result = self.discovery.run(
            1,
            {
                "mode": "after_scope",
                "table_scope": ts,
                "path_confidence_threshold": self._path_confidence_threshold(),
            },
        )
        self.state.last_specialist = "discovery"
        self.state.current_phase = max(self.state.current_phase, 1)
        self.state.last_artifacts.update(result.artifacts or {})

        clarify = (result.artifacts or {}).get("path_clarify")
        if clarify:
            self.state.path_clarify = clarify
            self.state.intake_field = "PATH_CLARIFY"
            self.state.pending_approval = None
            scope_art = (result.artifacts or {}).get("table_scope")
            if isinstance(scope_art, dict):
                self.state.table_scope = scope_art
            self._save()
            return result.reply

        if (result.artifacts or {}).get("table_scope"):
            self.state.table_scope = result.artifacts["table_scope"]
        self.state = sync_brief_status(self.project_dir, self.state)
        self._save()
        if not result.ok:
            return result.reply
        return result.reply + "\n\n" + self.propose_next_step()

    def _apply_path_clarify_answer(self, answer: str) -> str:
        """Record one PATH_CLARIFY answer; ask next or draft the brief."""
        pc = dict(self.state.path_clarify or {})
        questions = list(pc.get("questions") or [])
        idx = int(pc.get("index") or 0)
        answers = dict(pc.get("answers") or {})
        if not questions or idx >= len(questions):
            self.state.intake_field = None
            self.state.path_clarify = None
            self._save()
            return "No open path questions. Say continue and I'll draft the brief."

        q = questions[idx] if isinstance(questions[idx], dict) else {}
        qid = str(q.get("id") or f"q{idx + 1}")
        answers[qid] = answer
        pc["answers"] = answers
        idx += 1
        pc["index"] = idx
        self.state.path_clarify = pc
        self._save()

        if idx < len(questions):
            nxt = DiscoveryAgent.format_clarify_question(pc)
            return nxt or "Next question is ready — reply in chat."

        # All answers in — draft brief
        self.state.intake_field = None
        self._save()
        from activity_log import push_activity

        push_activity(
            "Path clarifications complete — drafting design brief",
            kind="info",
            agent="discovery",
        )
        result = self.discovery.run(
            1,
            {
                "mode": "brief",
                "path_clarify": pc,
                "path_confidence_threshold": self._path_confidence_threshold(),
            },
        )
        self.state.last_specialist = "discovery"
        self.state.current_phase = max(self.state.current_phase, 1)
        self.state.path_clarify = None
        self.state.last_artifacts.update(result.artifacts or {})
        self.state = sync_brief_status(self.project_dir, self.state)
        self._save()
        if not result.ok:
            return result.reply
        return result.reply + "\n\n" + self.propose_next_step()

    def handle(self, user_message: str) -> str:
        msg = (user_message or "").strip()
        if not msg:
            return "Open a project folder and I'll ask for what I need. Use the buttons when I ask a question."

        lower = msg.lower().strip()

        # Cancel intake
        if lower == "cancel" and self.state.intake_field:
            was_clarify = self.state.intake_field == "PATH_CLARIFY"
            was_scope = self.state.intake_field == "TABLE_SCOPE"
            self.state.intake_field = None
            if was_clarify:
                self.state.path_clarify = None
            if was_scope:
                self.state.table_scope = None
            self._save()
            return "Okay, paused. Say continue when you're ready."

        # Yes/no against pending approval (highest priority when pending)
        approved = self._handle_approval_reply(lower)
        if approved is not None:
            return approved

        # Answering current intake question
        intake_reply = self._handle_intake_answer(msg, lower)
        if intake_reply is not None:
            return intake_reply

        # Gate confirmations (also via exact phrases)
        if "desktop import ok" in lower:
            self.state.desktop_import_ok = True
            self._save()
            return "Got it — Desktop import confirmed.\n\n" + self.propose_next_step()

        if "wireframe approved" in lower:
            self.state.wireframe_approved = True
            self._save()
            return "Got it — wireframe approved.\n\n" + self.propose_next_step()

        if "phase 8 ok" in lower or lower == "phase8 ok":
            self.state.phase8_ok = True
            self._save()
            return "Signed off. Power BI delivery is complete."

        if lower in ("help", "?"):
            return (
                "Your job:\n"
                "- Connect the brain once in `.env` (Cursor or OpenAI keys)\n"
                "- Answer my questions\n"
                "- Reply yes/no when I verify links or ask to continue\n\n"
                "My job: check the local tools (dbt, adapters, MCP), collect warehouse details, "
                "prove the Postgres link, then build the dbt + BI delivery.\n\n"
                "I only answer questions about this engagement flow — not general topics.\n"
                "Useful: continue · status · check env · check links · help"
            )

        if lower in ("start", "begin", "setup", "continue"):
            return self.start_intake()

        if lower in ("check links", "check link", "check connection", "verify links"):
            brain = self.verify_brain()
            if missing_intake_keys(self.project_dir):
                return brain + "\n\nWarehouse: not ready yet — I still need connection details."
            warehouse = self.verify_warehouse(ask_human=True)
            return brain + "\n\n" + warehouse

        if lower in ("check env", "env precheck", "precheck", "check environment"):
            return self.run_env_precheck(auto_install=True)

        if lower == "status":
            return self.status_text()

        if lower in ("next", "what next", "what do you need", "what is needed", "needed"):
            return self.propose_next_step()

        if self._asks_about_config(lower):
            return self.explain_config()
        if self._asks_about_requirements(lower):
            return self.explain_requirements()

        if lower.startswith("init"):
            self.state.pending_approval = {
                "command": lower if "force" in lower else "init",
                "label": ACTION_LABELS["init"],
            }
            self._save()
            return (
                "I need to create the engagement workspace first.\n"
                "Should I continue?"
            )

        if lower == "approve brief":
            self.state.pending_approval = {
                "command": "approve brief",
                "label": ACTION_LABELS["approve brief"],
            }
            self._save()
            return (
                "I'll mark the design brief as approved.\n"
                "Should I continue?"
            )

        if self._is_off_topic(lower):
            guidance, _ = self.next_step()
            return f"{OFF_TOPIC_REDIRECT}\n\nWhere we are: {guidance}"

        m = re.match(r"run\s+phase\s+(\d+)\s*(visuals)?", lower)
        if m:
            cmd = f"run phase {m.group(1)}" + (" visuals" if m.group(2) else "")
            label = ACTION_LABELS.get(cmd, "continue")
            self.state.pending_approval = {"command": cmd, "label": label}
            self._save()
            return f"I can {label} next.\nShould I continue?"

        if lower in ("run all", "run pipeline"):
            self.state.pending_approval = {
                "command": "run all",
                "label": ACTION_LABELS["run all"],
            }
            self._save()
            return (
                "I can keep going through the full delivery (I'll still stop when I need you).\n"
                "Should I continue?"
            )

        # Free-form: if looks like KEY: value for a missing key, accept it
        kv = re.match(r"^([A-Z][A-Z0-9_]+)\s*[:=]\s*(.+)$", msg.strip())
        if kv:
            key, val = kv.group(1), kv.group(2).strip()
            if key in INTAKE_CONFIG_KEYS:
                res = set_config_value(self.project_dir, key, val)
                if res.ok:
                    apply_intake_defaults(self.project_dir)
                    return "Got it.\n\n" + self.start_intake()

        # No free-form LLM Q&A — stay on the engagement track
        guidance, _ = self.next_step()
        return (
            "I only follow this engagement flow (I don't answer open-ended questions).\n\n"
            f"Where we are: {guidance}\n\n"
            "Say continue, status, check env, or reply yes/no if I asked."
        )

    def _asks_about_config(self, lower: str) -> bool:
        return (
            "config.md" in lower
            or (
                "config" in lower
                and any(w in lower for w in ("what", "should", "need", "fill", "put", "example"))
            )
            or "credentials" in lower
            or ("connection" in lower and "db" in lower)
        )

    def _asks_about_requirements(self, lower: str) -> bool:
        return "requirements.md" in lower or (
            "requirements" in lower
            and any(w in lower for w in ("what", "should", "need", "fill", "put", "paste"))
        )

    def _is_off_topic(self, lower: str) -> bool:
        allow = {
            "init",
            "status",
            "help",
            "start",
            "begin",
            "setup",
            "continue",
            "check links",
            "check link",
            "check connection",
            "verify links",
            "check env",
            "env precheck",
            "precheck",
            "check environment",
            "yes",
            "no",
            "y",
            "n",
            "next",
            "what next",
            "what do you need",
            "what is needed",
            "needed",
        }
        if lower.startswith("run ") or lower in allow:
            return False
        if self._asks_about_config(lower) or self._asks_about_requirements(lower):
            return False
        # Explicit junk
        blocked = (
            "weather",
            "recipe",
            "joke",
            "capital of",
            "who is",
            "write a poem",
            "write me a",
            "tell me a story",
            "horoscope",
            "sports score",
        )
        if any(b in lower for b in blocked):
            return True
        # Question-shaped text with no engagement domain tokens → off topic
        looks_like_q = (
            "?" in lower
            or lower.startswith(
                (
                    "what ",
                    "why ",
                    "how ",
                    "who ",
                    "when ",
                    "where ",
                    "can you ",
                    "could you ",
                    "explain ",
                    "tell me ",
                    "define ",
                    "help me with ",
                )
            )
        )
        if looks_like_q and not any(t in lower for t in _DOMAIN_TOKENS):
            return True
        return False

    def run_env_precheck(
        self, *, auto_install: bool = True, propose_next: bool = True
    ) -> str:
        """Verify (and install when possible) dbt + agent runtime before Phase 0."""
        from tools.env_precheck import ensure_runtime_ready

        res = ensure_runtime_ready(self.project_dir, auto_install=auto_install)
        self.state.env_precheck_ok = bool(res.ok)
        self.state.last_artifacts["env_precheck"] = res.data or {}
        self._save()
        if res.ok:
            msg = res.output
            if propose_next:
                msg += "\n\n" + self.propose_next_step()
            return msg
        return (
            res.output
            + "\n\nFix the FAIL items (or say check env after installing), "
            "then we can set up the dbt project."
        )

    def run_all(self) -> str:
        parts = []
        for phase in range(0, 9):
            if phase == 8 and not self.state.phase8_relationships_done:
                parts.append(self.run_phase(8, visuals=False))
                if not self.state.wireframe_approved:
                    parts.append("Stopped for wireframe approval.")
                    break
                parts.append(self.run_phase(8, visuals=True))
            else:
                out = self.run_phase(phase)
                parts.append(out)
                if self.state.intake_field in ("PATH_CLARIFY", "TABLE_SCOPE"):
                    break
                if "Human gate" in out or "blocked" in out.lower() or "STOP" in out:
                    if phase == 1 and self.state.brief_status != "approved":
                        break
                    if phase == 7 and not self.state.desktop_import_ok:
                        break
            # stop if quality needs human
            if self.state.quality.status == "needs_human_waiver":
                parts.append("Stopped: quality needs human waiver.")
                break
        return "\n\n---\n\n".join(parts)

    def run_phase(self, phase: int, *, visuals: bool = False) -> str:
        self.state = sync_brief_status(self.project_dir, self.state)

        if phase < 0 or phase > 8:
            return "Phase must be 0..8"

        # Environment must be ready before writing / running dbt
        if phase == 0 and not self.state.env_precheck_ok:
            pre = self.run_env_precheck(auto_install=True, propose_next=False)
            if not self.state.env_precheck_ok:
                return (
                    "Blocked dbt setup until the environment precheck passes.\n\n" + pre
                )

        # Gates
        if phase >= 2 and self.state.brief_status != "approved":
            return (
                f"Blocked Phase {phase}: design_brief.md Status must be `approved` "
                f"(currently: {self.state.brief_status})."
            )

        if phase == 8 and not visuals and not self.state.desktop_import_ok:
            return (
                "Blocked Phase 8 until Desktop import is done.\n\n"
                + self.desktop_import_instructions()
            )

        if phase == 8 and visuals:
            if not self.state.phase8_relationships_done:
                return "Blocked visuals: run Phase 8 relationships first and pass Q8a."
            if self.state.quality.last_checkpoint == "Q8a" and self.state.quality.status != "pass":
                return "Blocked visuals: Q8a has not passed."
            if not self.state.wireframe_approved:
                return "Blocked visuals: say `Wireframe approved` after reviewing WIREFRAME.md."

        log_event("INFO", "run_phase_start", phase=phase, visuals=visuals)
        from activity_log import push_activity

        push_activity(
            f"Starting work step {phase}" + (" (visuals)" if visuals else ""),
            kind="info",
            agent="orchestrator",
        )
        try:
            if phase in (0, 1):
                ctx: dict[str, Any] = {}
                if phase == 1:
                    ctx["path_confidence_threshold"] = self._path_confidence_threshold()
                    # Resume mid-flight if scope already confirmed in state
                    ts = self.state.table_scope or {}
                    if ts.get("confirmed") and ts.get("in_scope"):
                        ctx["mode"] = "after_scope"
                        ctx["table_scope"] = ts
                result = self.discovery.run(phase, ctx)
                self.state.last_specialist = "discovery"
                self.state.current_phase = max(self.state.current_phase, phase)
                self.state.last_artifacts.update(result.artifacts or {})
                if phase == 1:
                    scope = (result.artifacts or {}).get("table_scope")
                    if scope and not (result.artifacts or {}).get("design_brief"):
                        # Waiting for scope confirm (not yet confirmed)
                        if not scope.get("confirmed"):
                            self.state.table_scope = scope
                            self.state.intake_field = "TABLE_SCOPE"
                            self.state.pending_approval = None
                            self._save()
                            return result.reply
                        self.state.table_scope = scope
                    clarify = (result.artifacts or {}).get("path_clarify")
                    if clarify:
                        self.state.path_clarify = clarify
                        self.state.intake_field = "PATH_CLARIFY"
                        self.state.pending_approval = None
                        self._save()
                        return result.reply
                    self.state.path_clarify = None
                    if self.state.intake_field in ("PATH_CLARIFY", "TABLE_SCOPE"):
                        self.state.intake_field = None
                    self.state = sync_brief_status(self.project_dir, self.state)
                self._save()
                return result.reply

            if phase in (2, 3, 4, 5):
                result = self.modeling.run(phase, {})
                self.state.last_specialist = "modeling"
                self.state.current_phase = max(self.state.current_phase, phase)
                self._save()
                msg = result.reply
                if phase == 5:
                    msg += "\n\n" + self._run_quality("Q5")
                return msg

            if phase == 6:
                result = self.semantic.run(6, {})
                self.state.last_specialist = "semantic"
                self.state.current_phase = 6
                self._save()
                return result.reply

            if phase == 7:
                ctx = {}
                if self._pending_confirm:
                    ctx.update(self._pending_confirm)
                    self._pending_confirm = None
                # Q7 owns build validation
                msg = self._run_quality("Q7", ctx)
                self.state.current_phase = 7
                self._save()
                if self.state.quality.status == "pass" and not self.state.desktop_import_ok:
                    msg += "\n\n" + self.desktop_import_instructions()
                    self.state.pending_approval = {
                        "command": "Desktop import OK",
                        "label": ACTION_LABELS["Desktop import OK"],
                    }
                    self._save()
                return msg

            if phase == 8:
                if visuals:
                    result = self.bi.run(8, {"bi_mode": "visuals"})
                    self.state.last_specialist = "bi"
                    self.state.phase8_visuals_done = True
                    self._save()
                    msg = result.reply + "\n\n" + self._run_quality("Q8b")
                    if self.state.quality.status == "pass":
                        msg += "\n\nHuman gate: verify PBIP, then say `Phase 8 OK`."
                    return msg

                result = self.bi.run(8, {"bi_mode": "relationships"})
                self.state.last_specialist = "bi"
                self.state.phase8_relationships_done = True
                self.state.current_phase = 8
                self._save()
                msg = result.reply + "\n\n" + self._run_quality("Q8a")
                if self.state.quality.status == "pass":
                    msg += "\n\nHuman gate: review WIREFRAME.md, then say `Wireframe approved`."
                return msg

        except Exception as exc:  # noqa: BLE001
            self.state.last_error = str(exc)
            self._save()
            log_event("ERROR", "run_phase_failed", phase=phase, error=str(exc))
            return f"Phase {phase} failed: {exc}"

        return f"Unhandled phase {phase}"

    def _run_quality(self, checkpoint: str, ctx: dict | None = None) -> str:
        context = {"checkpoint": checkpoint, **(ctx or {})}
        result = self.quality.run(0, context)
        status = (result.artifacts or {}).get("status", "needs_rework")
        rework_target = (result.artifacts or {}).get("rework_target")
        self.state.quality.last_checkpoint = checkpoint
        self.state.quality.status = status
        self.state.quality.findings_path = "QUALITY_FINDINGS.md"
        self.state.quality.rework_target = rework_target
        self._save()

        parts = [result.reply]

        if status == "pass":
            self.state.quality.rework_count = 0
            self._save()
            return parts[0]

        # Rework loop
        while status == "needs_rework" and self.state.quality.rework_count < MAX_REWORK:
            self.state.quality.rework_count += 1
            self._save()
            parts.append(
                f"Rework cycle {self.state.quality.rework_count}/{MAX_REWORK} → {rework_target}"
            )
            if rework_target == "modeling":
                self.modeling.run(0, {"rework": True})
                self.state.last_specialist = "modeling"
            elif rework_target == "semantic":
                self.semantic.run(6, {"rework": True})
                self.state.last_specialist = "semantic"
            elif rework_target == "bi":
                self.bi.run(8, {"bi_mode": "rework"})
                self.state.last_specialist = "bi"
            else:
                break

            # Re-audit
            result = self.quality.run(0, context)
            status = (result.artifacts or {}).get("status", "needs_rework")
            rework_target = (result.artifacts or {}).get("rework_target")
            self.state.quality.status = status
            self.state.quality.rework_target = rework_target
            self._save()
            parts.append(result.reply)
            if status == "pass":
                self.state.quality.rework_count = 0
                self._save()
                break

        if status != "pass":
            self.state.quality.status = "needs_human_waiver"
            self._save()
            parts.append(
                "Quality still failing after max rework. "
                "Human waiver required, or fix findings and re-run the phase."
            )
            # Q8a must block visuals — already gated by status
        return "\n".join(parts)


def resolve_project_dir(cli_dir: str | None = None) -> Path:
    from security_util import validate_engagement_dir

    raw = (cli_dir or "").strip()
    if not raw:
        raise SystemExit("Pass --project <path> to the engagement folder.")
    return validate_engagement_dir(Path(raw))
