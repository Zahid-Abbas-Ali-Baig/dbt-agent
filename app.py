"""Minimal Flask UI for the dbt engagement agent."""

from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session

from activity_log import activity_scope, begin_run, clear_activity, get_activity, push_activity
from orchestrator import Orchestrator
from security_util import redact_chat_message, validate_engagement_dir

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

app = Flask(__name__, template_folder="templates", static_folder="static")
_secret = (os.getenv("FLASK_SECRET_KEY") or "").strip()
if not _secret:
    # Ephemeral — sessions reset on restart; set FLASK_SECRET_KEY in .env for stability
    _secret = secrets.token_hex(24)
app.config["SECRET_KEY"] = _secret

INPUT_MAX_LENGTH = int(os.getenv("INPUT_MAX_LENGTH") or "100000")

# Per-engagement run lock — blocks human while a step is in flight
_run_locks: dict[str, float] = {}
_run_guard = threading.Lock()
BUSY_TTL_SEC = 3600


def _try_acquire_run(project_dir: Path) -> bool:
    key = str(project_dir.resolve())
    now = time.time()
    with _run_guard:
        started = _run_locks.get(key)
        if started and (now - started) < BUSY_TTL_SEC:
            return False
        _run_locks[key] = now
        return True


def _release_run(project_dir: Path) -> None:
    key = str(project_dir.resolve())
    with _run_guard:
        _run_locks.pop(key, None)


def _is_busy(project_dir: Path) -> bool:
    key = str(project_dir.resolve())
    now = time.time()
    with _run_guard:
        started = _run_locks.get(key)
        if not started:
            return False
        if (now - started) >= BUSY_TTL_SEC:
            _run_locks.pop(key, None)
            return False
        return True


def _project_dir_from_request() -> Path:
    payload = request.get_json(silent=True) or {}
    raw = (payload.get("project_dir") or session.get("project_dir") or "").strip()
    if not raw:
        raise ValueError("Open a project folder first.")
    p = validate_engagement_dir(Path(raw)).resolve()
    session["project_dir"] = str(p)
    return p


def _history() -> list[dict[str, str]]:
    if "history" not in session:
        session["history"] = []
    return session["history"]


@app.get("/")
def index():
    # Folder is typed by the operator — never prefilled from .env or defaults
    return render_template("index.html", project_dir="")


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.post("/api/project")
def set_project():
    payload = request.get_json(silent=True) or {}
    raw = (payload.get("project_dir") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "project_dir is required"}), 400
    try:
        p = validate_engagement_dir(Path(raw)).resolve()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    p.mkdir(parents=True, exist_ok=True)
    session["project_dir"] = str(p)
    # Fresh UI session for this Open — do not resurrect prior chat turns
    session["history"] = []
    clear_activity(str(p))
    begin_run("Opened engagement", project_dir=str(p), detail=str(p))
    try:
        from tools.env_precheck import ensure_runtime_ready
        from activity_log import push_activity

        chk = ensure_runtime_ready(p, auto_install=True)
        push_activity(
            "Environment precheck " + ("passed" if chk.ok else "needs attention"),
            kind="tool" if chk.ok else "error",
            project_dir=str(p),
            detail=(chk.output or "")[:400],
        )
        from pipeline_state import load_state, save_state

        st = load_state(p)
        st.env_precheck_ok = bool(chk.ok)
        st.last_artifacts["env_precheck"] = chk.data or {}
        save_state(p, st)
    except Exception:  # noqa: BLE001
        pass
    # Every Open re-asks brain and path confidence. Re-ask warehouse type only if warehouse not yet confirmed.
    try:
        from pipeline_state import load_state, save_state
        from tools.files import _write_key_direct

        st = load_state(p)
        warehouse_was_confirmed = bool(st.warehouse_confirmed)
        st.brain_confirmed = False
        st.path_confidence_confirmed = False
        st.intake_field = None
        st.pending_approval = None
        if not warehouse_was_confirmed:
            st.warehouse_confirmed = False
            st.warehouse_fingerprint = None
            if (p / "config.md").exists():
                for key in ("SOURCE_WAREHOUSE_TYPE", "TARGET_WAREHOUSE_TYPE", "WAREHOUSE_TYPE"):
                    _write_key_direct(p, key, "{{WAREHOUSE_TYPE}}")
        save_state(p, st)
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"ok": True, "project_dir": str(p)})


@app.get("/api/status")
def api_status():
    try:
        raw = (session.get("project_dir") or "").strip()
        if not raw:
            return jsonify({"ok": False, "error": "Open a project folder first."}), 400
        p = validate_engagement_dir(Path(raw))
        orch = Orchestrator(p)
        return jsonify(
            {
                "ok": True,
                "project_dir": str(p),
                "status": orch.status_text(),
                "busy": _is_busy(p),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/history")
def api_history():
    return jsonify({"ok": True, "history": _history()})


def _append_turn(user_text: str, reply: str, prompt: dict | None) -> None:
    h = _history()
    h.append({"role": "user", "text": user_text})
    assistant_entry: dict = {"role": "assistant", "text": reply}
    if prompt:
        assistant_entry["prompt"] = prompt
    h.append(assistant_entry)
    session["history"] = h[-60:]


@app.get("/api/activity")
def api_activity():
    raw = (
        request.args.get("project_dir")
        or session.get("project_dir")
        or ""
    ).strip()
    since = request.args.get("since")
    since_f = float(since) if since else None
    try:
        if raw:
            p = validate_engagement_dir(Path(raw))
            key = str(p)
        else:
            key = ""
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "entries": get_activity(key, since=since_f)})


@app.post("/api/chat")
def api_chat():
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "message is required"}), 400
    if len(message) > INPUT_MAX_LENGTH:
        return jsonify(
            {
                "ok": False,
                "error": f"Message too long (max {INPUT_MAX_LENGTH} characters).",
            }
        ), 400

    try:
        project_dir = _project_dir_from_request()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400

    if not _try_acquire_run(project_dir):
        return jsonify(
            {
                "ok": False,
                "busy": True,
                "error": (
                    "A step is already running for this project. "
                    "Wait until it finishes — then you can intervene."
                ),
            }
        ), 409

    begin_run(
        "Chat",
        project_dir=str(project_dir),
        detail=(message or "")[:120],
    )
    push_activity("Started", kind="info", detail=message[:120], project_dir=str(project_dir))

    intake_field = None
    prompt = None
    try:
        with activity_scope(str(project_dir)):
            orch = Orchestrator(project_dir)
            intake_field = orch.state.intake_field
            push_activity("Orchestrator handling your message", kind="info")
            reply = orch.handle(message)
            prompt = orch.get_ui_prompt()
            push_activity("Done", kind="info")
    except Exception as exc:  # noqa: BLE001
        push_activity(f"Error: {exc}", kind="error", project_dir=str(project_dir))
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        _release_run(project_dir)

    safe_user = redact_chat_message(message, intake_field=intake_field)
    _append_turn(safe_user, reply, prompt)

    return jsonify(
        {
            "ok": True,
            "reply": reply,
            "prompt": prompt,
            "project_dir": str(project_dir),
            "history": session["history"],
            "busy": False,
            "activity": get_activity(str(project_dir)),
        }
    )


@app.post("/api/warehouse-type")
def api_warehouse_type():
    payload = request.get_json(silent=True) or {}
    try:
        project_dir = _project_dir_from_request()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400

    if not _try_acquire_run(project_dir):
        return jsonify({"ok": False, "busy": True, "error": "A step is already running."}), 409

    begin_run("Warehouse type", project_dir=str(project_dir))
    push_activity("Saving warehouse type…", kind="info", project_dir=str(project_dir))

    try:
        with activity_scope(str(project_dir)):
            orch = Orchestrator(project_dir)
            reply = orch.apply_warehouse_type_menu(payload)
            prompt = orch.get_ui_prompt()
            push_activity("Warehouse type saved", kind="info")
    except Exception as exc:  # noqa: BLE001
        push_activity(f"Error: {exc}", kind="error", project_dir=str(project_dir))
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        _release_run(project_dir)

    _append_turn("Chose warehouse type", reply, prompt)
    return jsonify(
        {
            "ok": True,
            "reply": reply,
            "prompt": prompt,
            "project_dir": str(project_dir),
            "history": session["history"],
            "busy": False,
            "activity": get_activity(str(project_dir)),
        }
    )


@app.post("/api/confidence")
def api_confidence():
    payload = request.get_json(silent=True) or {}
    try:
        project_dir = _project_dir_from_request()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400

    if not _try_acquire_run(project_dir):
        return jsonify({"ok": False, "busy": True, "error": "A step is already running."}), 409

    begin_run("Path confidence", project_dir=str(project_dir))
    push_activity("Saving path confidence threshold…", kind="info", project_dir=str(project_dir))

    try:
        with activity_scope(str(project_dir)):
            orch = Orchestrator(project_dir)
            reply = orch.apply_confidence_menu(payload)
            prompt = orch.get_ui_prompt()
            if orch.state.path_confidence_confirmed:
                push_activity("Path confidence saved", kind="info")
            else:
                push_activity("Path confidence not saved", kind="error", detail=(reply or "")[:200])
    except Exception as exc:  # noqa: BLE001
        push_activity(f"Error: {exc}", kind="error", project_dir=str(project_dir))
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        _release_run(project_dir)

    ok = True
    # Surface config rejection as HTTP/JSON failure so the UI doesn't look "saved"
    if not orch.state.path_confidence_confirmed and "Unknown config key" in (reply or ""):
        ok = False

    _append_turn("Set path confidence threshold", reply, prompt)
    return jsonify(
        {
            "ok": ok,
            "reply": reply,
            "prompt": prompt,
            "error": None if ok else reply,
            "project_dir": str(project_dir),
            "history": session["history"],
            "busy": False,
            "activity": get_activity(str(project_dir)),
        }
    )


@app.post("/api/connection")
def api_connection():
    payload = request.get_json(silent=True) or {}
    try:
        project_dir = _project_dir_from_request()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400

    if not _try_acquire_run(project_dir):
        return jsonify({"ok": False, "busy": True, "error": "A step is already running."}), 409

    begin_run("Connection", project_dir=str(project_dir))
    push_activity("Saving warehouse connection…", kind="info", project_dir=str(project_dir))

    try:
        with activity_scope(str(project_dir)):
            orch = Orchestrator(project_dir)
            reply = orch.apply_connection_menu(payload)
            prompt = orch.get_ui_prompt()
            push_activity("Connection save finished", kind="info")
    except Exception as exc:  # noqa: BLE001
        push_activity(f"Error: {exc}", kind="error", project_dir=str(project_dir))
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        _release_run(project_dir)

    _append_turn("Saved warehouse connection form", reply, prompt)
    return jsonify(
        {
            "ok": True,
            "reply": reply,
            "prompt": prompt,
            "project_dir": str(project_dir),
            "history": session["history"],
            "busy": False,
            "activity": get_activity(str(project_dir)),
        }
    )


@app.post("/api/brain")
def api_brain():
    payload = request.get_json(silent=True) or {}
    try:
        project_dir = _project_dir_from_request()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400

    if not _try_acquire_run(project_dir):
        return jsonify({"ok": False, "busy": True, "error": "A step is already running."}), 409

    begin_run("Brain", project_dir=str(project_dir))
    push_activity("Saving brain settings…", kind="info", project_dir=str(project_dir))

    try:
        with activity_scope(str(project_dir)):
            orch = Orchestrator(project_dir)
            reply = orch.apply_brain_menu(payload)
            prompt = orch.get_ui_prompt()
            push_activity("Brain save finished", kind="info")
    except Exception as exc:  # noqa: BLE001
        push_activity(f"Error: {exc}", kind="error", project_dir=str(project_dir))
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        _release_run(project_dir)

    _append_turn("Saved brain form", reply, prompt)
    return jsonify(
        {
            "ok": True,
            "reply": reply,
            "prompt": prompt,
            "project_dir": str(project_dir),
            "history": session["history"],
            "busy": False,
            "activity": get_activity(str(project_dir)),
        }
    )


@app.post("/api/reset")
def api_reset():
    session["history"] = []
    return jsonify({"ok": True})


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5050"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() in {"1", "true", "yes"}
    print(
        "Activity History is mirrored to disk as activity.log "
        "(engagement root + .dbt_agent/). Restart required after code changes.",
        flush=True,
    )
    app.run(host=host, port=port, debug=debug)
