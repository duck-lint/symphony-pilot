#!/usr/bin/env python3
"""Trusted lifecycle broker for one concrete role execution.

The dispatch marker is host-authored. Runtime writes an execution receipt only
after App Server has started a thread and when that thread reaches a terminal
state. This module binds the role packet to that receipt and only then writes
the SQLite ``role_runs`` row. A model claim cannot create an execution row.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import stat
import uuid

from control_db import ControlPlaneDatabase, ControlPlaneError, StateConflict
from execution_receipts import ReceiptError, capture_live_receipt, verify_persisted_receipt
from prepare_workspace import Profile, control_database_path, local_task_facts, require_physical_namespace
from workspace_boundary import atomic_metadata_write, physical_directory, run_git

RESULT_SCHEMA = "symphony-pilot-lifecycle-result/v1"
WORKPAD_MARKER = "<!-- symphony-workpad:v1 -->"
MAX_RESULT_BYTES = 128 * 1024
MAX_WORKPAD_BYTES = 64 * 1024
MAX_SUMMARY_BYTES = 12 * 1024
TASK_IDENTIFIER_RE = re.compile(r"^T-[0-9]{6}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SECRET_MARKER_RE = re.compile(r"(?:gh[pousr]_|github_pat_|\bsk-[A-Za-z0-9]|BEGIN [A-Z ]*PRIVATE KEY|Bearer\s+)", re.I)
ACTIVE_STATES = ("QUEUED", "PLANNED", "IMPLEMENTED", "REVIEW", "ADVERSARIAL_REVIEW", "FINAL_MECHANICAL_ACCEPTANCE")
ALL_ROLES = {"ARCHITECT", "PROJECT-MANAGER", "PLANNER", "IMPLEMENTER", "REVIEWER", "ADVERSARY", "ARCHIVIST"}
SPECIALIZED_ROLES = ALL_ROLES - {"ARCHITECT"}
OUTCOMES = {"role_requested", "role_complete", "planning_complete", "implementation_complete", "review_approved", "adversary_pass", "validation_pass", "archive_complete", "correction_required", "blocked"}
FINDING_CLASSES = {"licensed correction", "unresolved project decision", "infrastructure condition", "rejected"}
BLOCKER_KINDS = {None, "human", "project", "infrastructure"}
FINDING_FIELDS = {"role", "kind", "severity", "body", "classification", "blocker_kind"}
ROLE_PACKET_FIELDS = {"role", "verdict", "summary", "head_sha", "findings"}
RESULT_FIELDS = {"schema", "task_uuid", "identifier", "role_run_id", "role", "expected_state", "expected_workpad_version", "expected_starting_head", "workpad_body", "summary", "outcome", "packet", "findings", "requested_resolved_finding_ids", "authorized_write_paths"}
MAX_AUTHORIZED_WRITE_PATHS = 32


class LifecycleError(ControlPlaneError):
    """A lifecycle result or host lifecycle invariant is invalid."""


class AllocationConflict(LifecycleError):
    """A task already has a pending execution or is not dispatchable."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _bounded_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise LifecycleError(f"lifecycle {field} is invalid or exceeds its bound")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise LifecycleError(f"lifecycle {field} contains control characters")
    if SECRET_MARKER_RE.search(value):
        raise LifecycleError(f"lifecycle {field} contains a credential marker")
    return value


def lifecycle_root(profile: Profile, identifier: str, run_id: str) -> pathlib.Path:
    if not TASK_IDENTIFIER_RE.fullmatch(identifier) or not UUID_RE.fullmatch(run_id):
        raise LifecycleError("lifecycle namespace identity is invalid")
    state_root = require_physical_namespace(profile.state_root)
    if state_root.is_symlink() or (state_root.exists() and not state_root.is_dir()):
        raise LifecycleError("lifecycle state root is unsafe")
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_root.chmod(0o700)
    root = state_root / "lifecycle" / identifier / run_id
    for directory in (state_root / "lifecycle", state_root / "lifecycle" / identifier, root):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise LifecycleError("lifecycle namespace contains an unsafe path component")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    for name in ("inbox", "outbox", "host"):
        directory = root / name
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise LifecycleError("lifecycle namespace contains an unsafe directory")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    return root


def _initial_workpad(task: dict[str, object]) -> str:
    return "\n".join((WORKPAD_MARKER, "## Symphony Workpad", "", f"- Task: {task['identifier']}", f"- Objective: {task['objective']}", f"- Base: {task['base_ref']} @ {task['base_sha']}", f"- Branch: {task['branch']}", f"- Lifecycle state: {task['state']}", ""))


def _read_host_json(path: pathlib.Path, label: str) -> object:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LifecycleError(f"{label} is not a readable regular file") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise LifecycleError(f"{label} is not a regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(MAX_RESULT_BYTES + 1)
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if len(raw) > MAX_RESULT_BYTES:
        raise LifecycleError(f"{label} exceeds the byte bound")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise LifecycleError(f"{label} is malformed UTF-8 JSON") from exc


def _write_host_json(path: pathlib.Path, value: object) -> None:
    atomic_metadata_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _claim_dispatch(workspace: pathlib.Path) -> pathlib.Path:
    claim = workspace / ".git" / "symphony-dispatch.claim"
    try:
        descriptor = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(descriptor)
    except FileExistsError as exc:
        raise AllocationConflict("a role dispatch is already pending") from exc
    return claim


def _release_dispatch_claim(workspace: pathlib.Path) -> None:
    try:
        (workspace / ".git" / "symphony-dispatch.claim").unlink()
    except FileNotFoundError:
        pass


def _git(workspace: pathlib.Path, *args: str) -> str:
    result = run_git(workspace, *args)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        raise LifecycleError(f"Git verification failed: {detail[:240]}")
    return result.stdout.strip()


def verify_git_truth(profile: Profile, workspace: pathlib.Path, task: dict[str, object]) -> str:
    if _git(workspace, "remote", "get-url", "origin") != profile.git_remote:
        raise LifecycleError("workspace repository identity does not match the registered remote")
    if _git(workspace, "branch", "--show-current") != task["branch"]:
        raise LifecycleError("workspace branch is not the host-owned task branch")
    if _git(workspace, "status", "--porcelain=v1", "--untracked-files=all"):
        raise LifecycleError("workspace is dirty after the role execution")
    head = _git(workspace, "rev-parse", "HEAD")
    if not SHA_RE.fullmatch(head):
        raise LifecycleError("workspace HEAD is not a commit SHA")
    if run_git(workspace, "merge-base", "--is-ancestor", str(task["base_sha"]), head).returncode:
        raise LifecycleError("workspace HEAD is not descended from the accepted base")
    return head


def _next_round(database: ControlPlaneDatabase, task_id: str, role: str) -> int:
    row = database.connection.execute("SELECT COALESCE(MAX(round), 0) + 1 FROM role_runs WHERE task_id = ? AND role = ?", (task_id, role)).fetchone()
    return int(row[0])


def _role_rows(database: ControlPlaneDatabase, task_id: str, role: str | None = None) -> list[dict[str, object]]:
    if role is None:
        rows = database.connection.execute("SELECT * FROM role_runs WHERE task_id = ? ORDER BY rowid", (task_id,)).fetchall()
    else:
        rows = database.connection.execute("SELECT * FROM role_runs WHERE task_id = ? AND role = ? ORDER BY round", (task_id, role)).fetchall()
    return [dict(row) for row in rows]


def _latest_role(database: ControlPlaneDatabase, task_id: str, role: str) -> dict[str, object] | None:
    rows = _role_rows(database, task_id, role)
    return rows[-1] if rows else None


def _latest_execution(database: ControlPlaneDatabase, task_id: str) -> dict[str, object] | None:
    rows = _role_rows(database, task_id)
    return rows[-1] if rows else None


def _latest_architect_outcome(database: ControlPlaneDatabase, task_id: str) -> str | None:
    row = database.connection.execute("SELECT payload_json FROM task_events WHERE task_id = ? AND event_type = 'role_finished' AND role_run_id IN (SELECT id FROM role_runs WHERE role = 'ARCHITECT') ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return str(payload.get("outcome")) if isinstance(payload, dict) and payload.get("outcome") else None


def _latest_role_verdict(database: ControlPlaneDatabase, task_id: str, role: str) -> str | None:
    row = database.connection.execute(
        "SELECT payload_json FROM task_events WHERE task_id = ? AND event_type = 'role_finished' "
        "AND role_run_id IN (SELECT id FROM role_runs WHERE role = ?) ORDER BY rowid DESC LIMIT 1",
        (task_id, role),
    ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return str(payload.get("verdict")) if isinstance(payload, dict) and payload.get("verdict") else None


def _next_dispatch_role(database: ControlPlaneDatabase, task: dict[str, object]) -> str | None:
    task_id = str(task["id"])
    latest = _latest_execution(database, task_id)
    if latest is None:
        return "ARCHITECT"
    if latest["role"] == "ARCHIVIST":
        return None
    if latest["role"] != "ARCHITECT":
        return "ARCHITECT"
    state = str(task["state"])
    if state == "QUEUED":
        if _latest_architect_outcome(database, task_id) == "correction_required":
            return "PROJECT-MANAGER"
        pm = _latest_role(database, task_id, "PROJECT-MANAGER")
        if pm is None:
            return "PROJECT-MANAGER"
        planner = _latest_role(database, task_id, "PLANNER")
        if planner is None or int(planner["round"]) < int(pm["round"]):
            return "PLANNER"
        return None
    return {"PLANNED": "IMPLEMENTER", "IMPLEMENTED": "REVIEWER", "REVIEW": "ADVERSARY", "ADVERSARIAL_REVIEW": "ARCHITECT", "FINAL_MECHANICAL_ACCEPTANCE": "ARCHIVIST"}.get(state)


def _packet(database: ControlPlaneDatabase, task: dict[str, object], run_id: str, role: str) -> dict[str, object]:
    workpad = database.read_workpad(str(task["id"]))
    if workpad is None:
        database.connection.execute(
            "INSERT INTO workpads(task_id, body, version, updated_at) VALUES (?, ?, 1, ?)",
            (task["id"], _initial_workpad(task), _now()),
        )
        workpad = database.read_workpad(str(task["id"]))
    open_findings = database.connection.execute("SELECT id, kind, severity, body, status, licensed_correction_round FROM findings WHERE task_id = ? AND status IN ('open', 'accepted', 'licensed') ORDER BY rowid", (task["id"],)).fetchall()
    blockers = database.connection.execute("SELECT kind, body FROM blockers WHERE task_id = ? AND status = 'open' ORDER BY created_at, id", (task["id"],)).fetchall()
    return {"schema": "symphony-pilot-lifecycle-input/v2", "task_uuid": task["id"], "identifier": task["identifier"], "title": task["title"], "objective": task["objective"], "current_state": task["state"], "next_expected_action": role, "base_ref": task["base_ref"], "base_sha": task["base_sha"], "branch": task["branch"], "selected_head": task["current_head"] or task["base_sha"], "workpad": {"body": workpad["body"], "version": workpad["version"]}, "open_findings": [dict(row) for row in open_findings], "licensed_finding_ids": [str(row["id"]) for row in open_findings if row["status"] == "licensed"], "open_blockers": [dict(row) for row in blockers], "role": role, "role_run_id": run_id}


def _latest_architect_authorized_paths(database: ControlPlaneDatabase, task_id: str) -> list[str]:
    row = database.connection.execute(
        "SELECT payload_json FROM task_events WHERE task_id = ? AND event_type = 'role_finished' "
        "AND role_run_id IN (SELECT id FROM role_runs WHERE role = 'ARCHITECT') "
        "ORDER BY rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row:
        raise LifecycleError("Implementer dispatch has no Architect-authorized write seam")
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LifecycleError("latest Architect routing evidence is malformed") from exc
    paths = payload.get("authorized_write_paths") if isinstance(payload, dict) else None
    if not isinstance(paths, list) or not paths:
        raise LifecycleError("Implementer dispatch has no Architect-authorized write seam")
    return paths


def _resolve_authorized_write_roots(workspace: pathlib.Path, paths: object) -> list[str]:
    if not isinstance(paths, list) or not 0 < len(paths) <= MAX_AUTHORIZED_WRITE_PATHS:
        raise LifecycleError("authorized implementation seam is invalid")
    root = physical_directory(workspace).resolve()
    resolved: list[str] = []
    for value in paths:
        if not isinstance(value, str) or not value.strip():
            raise LifecycleError("authorized implementation seam contains an invalid path")
        candidate_text = value.replace("\\", "/")
        candidate_path = pathlib.PurePath(value)
        windows_path = pathlib.PureWindowsPath(value)
        if candidate_path.is_absolute() or windows_path.is_absolute() or ":" in candidate_text:
            raise LifecycleError("authorized implementation seam must be repository-relative")
        parts = pathlib.PurePosixPath(candidate_text).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise LifecycleError("authorized implementation seam contains traversal")
        candidate = (root / pathlib.Path(*parts)).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise LifecycleError("authorized implementation seam escapes the task checkout") from exc
        if candidate == root or not candidate.exists() or not candidate.is_dir():
            raise LifecycleError("authorized implementation seam must name an existing directory below the task checkout")
        resolved.append(str(candidate))
    return list(dict.fromkeys(resolved))


def _record_infrastructure_blocker(profile: Profile, task_id: str, detail: str) -> None:
    with ControlPlaneDatabase.open(control_database_path(profile)) as database:
        with database._transaction():
            blocker_id = str(uuid.uuid4())
            timestamp = _now()
            database.connection.execute("INSERT INTO blockers(id, task_id, kind, body, status, created_at, resolved_at) VALUES (?, ?, 'infrastructure', ?, 'open', ?, NULL)", (blocker_id, task_id, detail[:MAX_SUMMARY_BYTES], timestamp))
            database._insert_event(task_id, "infrastructure_blocked", {"blocker_id": blocker_id, "kind": "infrastructure"})


def prepare_attempt(profile: Profile, workspace: pathlib.Path) -> dict[str, object]:
    """Prepare one host-selected dispatch without creating a role_run."""
    workspace = physical_directory(workspace)
    facts, _ = local_task_facts(profile, workspace)
    if facts.branch != _git(workspace, "branch", "--show-current"):
        raise LifecycleError("workspace is not on the host-owned task branch")
    claim = _claim_dispatch(workspace)
    marker_path = workspace / ".git" / "symphony-preparation.json"
    try:
        old_marker = _read_host_json(marker_path, "preparation marker")
    except LifecycleError:
        old_marker = {}
    try:
        with ControlPlaneDatabase.open(control_database_path(profile)) as database:
            with database._transaction():
                task = database.read_task(facts.task_uuid)
                if task["project_slug"] != profile.slug or task["identifier"] != facts.identifier:
                    raise AllocationConflict("task identity is not project-scoped")
                if task["state"] not in ACTIVE_STATES:
                    raise AllocationConflict(f"task state is not active: {task['state']}")
                if database.connection.execute("SELECT 1 FROM blockers WHERE task_id = ? AND status = 'open' LIMIT 1", (facts.task_uuid,)).fetchone():
                    raise AllocationConflict("task has an open blocker")
                role = _next_dispatch_role(database, task)
                if role is None:
                    raise AllocationConflict("task has no eligible role dispatch")
                run_id = str(uuid.uuid4())
                packet = _packet(database, task, run_id, role)
                round_number = _next_round(database, facts.task_uuid, role)
                authorized_write_paths = (
                    _latest_architect_authorized_paths(database, facts.task_uuid)
                    if role == "IMPLEMENTER" else []
                )
        namespace = lifecycle_root(profile, facts.identifier, run_id)
        outbox = namespace / "outbox"
        result_writable_root = str(outbox)
        target_writable_roots: list[str] = []
        if role == "IMPLEMENTER":
            target_writable_roots = _resolve_authorized_write_roots(workspace, authorized_write_paths)
        packet["authorized_write_paths"] = authorized_write_paths
        packet["dispatch"] = {
            "role": role,
            "role_run_id": run_id,
            "round": round_number,
            "lifecycle_namespace": str(namespace),
            "result_path": str(outbox / "result.json"),
            "result_writable_root": result_writable_root,
            "target_writable_roots": target_writable_roots,
        }
        _write_host_json(namespace / "inbox" / "lifecycle.json", packet)
        marker = old_marker if isinstance(old_marker, dict) else {}
        marker.update({
            "dispatch_role": role,
            "role_run_id": run_id,
            "role_round": round_number,
            "lifecycle_namespace": str(namespace),
            "lifecycle_packet": "inbox/lifecycle.json",
            "dispatch_state": "pending",
            "task_uuid": facts.task_uuid,
            "identifier": facts.identifier,
            "selected_head": facts.selected_head,
            "result_writable_root": result_writable_root,
            "target_writable_roots": target_writable_roots,
            "authorized_write_paths": authorized_write_paths,
        })
        _write_host_json(marker_path, marker)
        return {"task": task, "run": {"id": run_id, "role": role, "round": round_number, "status": "pending"}, "packet": packet, "namespace": str(namespace)}
    except Exception:
        _release_dispatch_claim(workspace)
        raise


def read_result(path: pathlib.Path) -> dict[str, object]:
    value = _read_host_json(path, "lifecycle result")
    if not isinstance(value, dict) or set(value) != RESULT_FIELDS:
        raise LifecycleError("lifecycle result fields are invalid")
    if value["schema"] != RESULT_SCHEMA:
        raise LifecycleError("lifecycle result schema is invalid")
    for field in ("task_uuid", "role_run_id"):
        if not isinstance(value[field], str) or not UUID_RE.fullmatch(value[field]):
            raise LifecycleError(f"lifecycle {field} is invalid")
    if not isinstance(value["identifier"], str) or not TASK_IDENTIFIER_RE.fullmatch(value["identifier"]):
        raise LifecycleError("lifecycle identifier is invalid")
    if value["role"] not in ALL_ROLES or value["expected_state"] not in ACTIVE_STATES:
        raise LifecycleError("lifecycle role or expected state is invalid")
    if not isinstance(value["expected_workpad_version"], int) or isinstance(value["expected_workpad_version"], bool) or value["expected_workpad_version"] < 1:
        raise LifecycleError("lifecycle workpad version is invalid")
    if not isinstance(value["expected_starting_head"], str) or not SHA_RE.fullmatch(value["expected_starting_head"]):
        raise LifecycleError("lifecycle starting HEAD is invalid")
    _bounded_text(value["workpad_body"], "workpad_body", MAX_WORKPAD_BYTES)
    _bounded_text(value["summary"], "summary", MAX_SUMMARY_BYTES)
    authorized_write_paths = value["authorized_write_paths"]
    if not isinstance(authorized_write_paths, list) or len(authorized_write_paths) > MAX_AUTHORIZED_WRITE_PATHS:
        raise LifecycleError("authorized implementation seam is invalid")
    if any(not isinstance(path, str) or not path.strip() for path in authorized_write_paths):
        raise LifecycleError("authorized implementation seam contains an invalid path")
    if value["outcome"] not in OUTCOMES or not isinstance(value["findings"], list) or not isinstance(value["requested_resolved_finding_ids"], list):
        raise LifecycleError("lifecycle outcome or lists are invalid")
    for finding_id in value["requested_resolved_finding_ids"]:
        if not isinstance(finding_id, str) or not UUID_RE.fullmatch(finding_id):
            raise LifecycleError("lifecycle requested finding identity is invalid")

    def validate_finding(finding: object, expected_role: str) -> None:
        if not isinstance(finding, dict) or set(finding) != FINDING_FIELDS or finding["role"] != expected_role:
            raise LifecycleError("lifecycle finding provenance is invalid")
        if finding["severity"] not in {"info", "low", "medium", "high", "critical"} or finding["classification"] not in FINDING_CLASSES or finding["blocker_kind"] not in BLOCKER_KINDS:
            raise LifecycleError("lifecycle finding fields are invalid")
        expected = {"licensed correction": None, "rejected": None, "unresolved project decision": {"human", "project"}, "infrastructure condition": {"infrastructure"}}[finding["classification"]]
        if (expected is None and finding["blocker_kind"] is not None) or (isinstance(expected, set) and finding["blocker_kind"] not in expected):
            raise LifecycleError("lifecycle finding blocker kind is invalid")
        _bounded_text(finding["kind"], "finding kind", 256)
        _bounded_text(finding["body"], "finding body", MAX_SUMMARY_BYTES)

    if value["role"] == "ARCHITECT":
        if value["packet"] is not None:
            raise LifecycleError("Architect cannot author a specialized role packet")
        if value["outcome"] == "planning_complete" and not authorized_write_paths:
            raise LifecycleError("planning completion requires an explicit implementation seam")
        if value["outcome"] != "planning_complete" and authorized_write_paths:
            raise LifecycleError("only planning completion may authorize an implementation seam")
        for finding in value["findings"]:
            validate_finding(finding, "ARCHITECT")
    else:
        if authorized_write_paths:
            raise LifecycleError("only Architect may authorize an implementation seam")
        packet = value["packet"]
        if not isinstance(packet, dict) or set(packet) != ROLE_PACKET_FIELDS or packet["role"] != value["role"]:
            raise LifecycleError("specialized packet does not match dispatched role")
        if value["findings"]:
            raise LifecycleError("specialized role cannot author Architect findings")
        _bounded_text(packet["summary"], "role summary", MAX_SUMMARY_BYTES)
        if packet["head_sha"] is not None and (not isinstance(packet["head_sha"], str) or not SHA_RE.fullmatch(packet["head_sha"])):
            raise LifecycleError("specialized role HEAD is invalid")
        if packet["verdict"] not in {"APPROVE", "PASS", "COMPLETE", "FINDINGS", "BLOCKED"} or not isinstance(packet["findings"], list):
            raise LifecycleError("specialized packet fields are invalid")
        for finding in packet["findings"]:
            validate_finding(finding, value["role"])
    return value


def _materialize_started_run(database: ControlPlaneDatabase, task: dict[str, object], marker: dict[str, object], receipt: dict[str, object]) -> dict[str, object]:
    run_id, role, round_number = str(marker["role_run_id"]), str(marker["dispatch_role"]), int(marker["role_round"])
    existing = database.connection.execute("SELECT * FROM role_runs WHERE id = ?", (run_id,)).fetchone()
    if existing is not None:
        run = dict(existing)
        if run["task_id"] != task["id"] or run["role"] != role or run["round"] != round_number:
            raise StateConflict("execution receipt is bound to a different role run")
        return run
    started_at = receipt.get("started_at")
    if not isinstance(started_at, str) or not started_at:
        raise LifecycleError("execution receipt has no successful start time")
    database.connection.execute("INSERT INTO role_runs(id, task_id, role, round, head_sha, status, started_at, finished_at, result_summary) VALUES (?, ?, ?, ?, ?, 'started', ?, NULL, NULL)", (run_id, task["id"], role, round_number, task["current_head"] or task["base_sha"], started_at))
    database._insert_event(task["id"], "role_started", {"role": role, "round": round_number, "session_id": receipt.get("session_id"), "thread_id": receipt.get("thread_id"), "turn_id": receipt.get("turn_id")}, role_run_id=run_id, occurred_at=started_at)
    return database.read_role_run(run_id)


def _finish_run(database: ControlPlaneDatabase, run: dict[str, object], receipt: dict[str, object], status: str, summary: str, head: str, outcome: str | None = None, verdict: str | None = None, authorized_write_paths: list[str] | None = None) -> None:
    finished_at = receipt.get("finished_at") or _now()
    database.connection.execute("UPDATE role_runs SET status = ?, finished_at = ?, result_summary = ?, head_sha = ? WHERE id = ? AND status = 'started'", (status, finished_at, summary, head, run["id"]))
    if database.connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise StateConflict("role run is no longer started")
    payload = {"role": run["role"], "round": run["round"], "status": status}
    if outcome is not None:
        payload["outcome"] = outcome
    if verdict is not None:
        payload["verdict"] = verdict
    if authorized_write_paths is not None:
        payload["authorized_write_paths"] = authorized_write_paths
    database._insert_event(run["task_id"], "role_finished", payload, role_run_id=run["id"], occurred_at=finished_at)


def _insert_finding(database: ControlPlaneDatabase, task: dict[str, object], run: dict[str, object], finding: dict[str, object]) -> None:
    classification = finding["classification"]
    status = "licensed" if classification == "licensed correction" else ("rejected" if classification == "rejected" else "open")
    correction_round = _next_round(database, str(task["id"]), "IMPLEMENTER") if status == "licensed" else None
    finding_id = str(uuid.uuid4())
    database.connection.execute("INSERT INTO findings(id, task_id, role_run_id, kind, severity, body, status, licensed_correction_round) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (finding_id, task["id"], run["id"], finding["kind"], finding["severity"], finding["body"], status, correction_round))
    database._insert_event(task["id"], "finding_recorded", {"finding_id": finding_id, "role": run["role"], "classification": classification}, role_run_id=run["id"])
    if finding["blocker_kind"] in {"human", "project", "infrastructure"}:
        blocker_id = str(uuid.uuid4())
        database.connection.execute("INSERT INTO blockers(id, task_id, kind, body, status, created_at, resolved_at) VALUES (?, ?, ?, ?, 'open', ?, NULL)", (blocker_id, task["id"], finding["blocker_kind"], finding["body"], _now()))
        database._insert_event(task["id"], "infrastructure_blocked" if finding["blocker_kind"] == "infrastructure" else "human_blocked", {"blocker_id": blocker_id, "kind": finding["blocker_kind"]})


def _expected_specialized_role(database: ControlPlaneDatabase, task: dict[str, object], current_run: dict[str, object] | None = None) -> str | None:
    task_id = str(task["id"])
    if task["state"] == "QUEUED":
        pm, planner = _latest_role(database, task_id, "PROJECT-MANAGER"), _latest_role(database, task_id, "PLANNER")
        if current_run is not None:
            if pm is not None and pm["id"] == current_run["id"]:
                pm = _role_rows(database, task_id, "PROJECT-MANAGER")[-2] if len(_role_rows(database, task_id, "PROJECT-MANAGER")) > 1 else None
            if planner is not None and planner["id"] == current_run["id"]:
                planner = _role_rows(database, task_id, "PLANNER")[-2] if len(_role_rows(database, task_id, "PLANNER")) > 1 else None
            if current_run["role"] == "PROJECT-MANAGER" and int(current_run["round"]) > int(pm["round"] if pm else 0):
                return "PROJECT-MANAGER"
        if pm is None:
            return "PROJECT-MANAGER"
        if planner is None or int(planner["round"]) < int(pm["round"]):
            return "PLANNER"
        return None
    return {"PLANNED": "IMPLEMENTER", "IMPLEMENTED": "REVIEWER", "REVIEW": "ADVERSARY", "FINAL_MECHANICAL_ACCEPTANCE": "ARCHIVIST"}.get(str(task["state"]))


def _transition(database: ControlPlaneDatabase, task: dict[str, object], new_state: str) -> None:
    database.connection.execute("UPDATE tasks SET state = ?, updated_at = ? WHERE id = ? AND state = ?", (new_state, _now(), task["id"], task["state"]))
    if database.connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise StateConflict("task state changed during lifecycle reconciliation")


def _reconcile(database: ControlPlaneDatabase, task: dict[str, object], result: dict[str, object], run: dict[str, object], receipt: dict[str, object], actual_head: str, profile: Profile) -> dict[str, object]:
    if task["identifier"] != result["identifier"] or task["state"] != result["expected_state"]:
        raise StateConflict("lifecycle result identity or expected state is stale")
    workpad = database.read_workpad(str(task["id"]))
    if workpad is None or workpad["version"] != result["expected_workpad_version"]:
        raise StateConflict("lifecycle workpad version is stale")
    selected_head = str(task["current_head"] or task["base_sha"])
    if selected_head != result["expected_starting_head"]:
        raise StateConflict("lifecycle starting HEAD is stale")
    role = str(result["role"])
    if role != run["role"] or str(result["role_run_id"]) != run["id"]:
        raise StateConflict("lifecycle result is not bound to the dispatched execution")
    state = str(task["state"])
    packet = result["packet"]
    if role == "ARCHITECT":
        if actual_head != selected_head:
            raise LifecycleError("read-only Architect execution changed Git HEAD")
        allowed = {"QUEUED": {"role_requested", "planning_complete", "correction_required", "blocked"}, "PLANNED": {"implementation_complete", "blocked"}, "IMPLEMENTED": {"review_approved", "correction_required", "blocked"}, "REVIEW": {"adversary_pass", "correction_required", "blocked"}, "ADVERSARIAL_REVIEW": {"validation_pass", "correction_required", "blocked"}}.get(state, set())
        if result["outcome"] not in allowed:
            raise LifecycleError("Architect outcome is impossible for the current state")
        if result["outcome"] == "role_requested" and _next_dispatch_role(database, task) not in {"PROJECT-MANAGER", "PLANNER"}:
            raise LifecycleError("Architect requested a role that is not eligible")
        if result["outcome"] == "planning_complete":
            pm, planner = _latest_role(database, str(task["id"]), "PROJECT-MANAGER"), _latest_role(database, str(task["id"]), "PLANNER")
            if pm is None or planner is None or int(pm["round"]) != int(planner["round"]):
                raise LifecycleError("planning cannot advance without one real PM and Planner round")
            _transition(database, task, "PLANNED")
        elif result["outcome"] == "implementation_complete":
            if _latest_role(database, str(task["id"]), "IMPLEMENTER") is None:
                raise LifecycleError("implementation cannot advance without a real Implementer run")
            _transition(database, task, "IMPLEMENTED")
        elif result["outcome"] == "review_approved":
            reviewer = _latest_role(database, str(task["id"]), "REVIEWER")
            if reviewer is None or _latest_role_verdict(database, str(task["id"]), "REVIEWER") != "APPROVE":
                raise LifecycleError("review approval has no real Reviewer execution")
            _transition(database, task, "REVIEW")
            database._insert_event(task["id"], "review_accepted", {"head_sha": actual_head}, role_run_id=reviewer["id"])
        elif result["outcome"] == "adversary_pass":
            adversary = _latest_role(database, str(task["id"]), "ADVERSARY")
            if adversary is None or _latest_role_verdict(database, str(task["id"]), "ADVERSARY") != "PASS":
                raise LifecycleError("adversary approval has no real Adversary execution")
            _transition(database, task, "ADVERSARIAL_REVIEW")
            database._insert_event(task["id"], "adversary_accepted", {"head_sha": actual_head}, role_run_id=adversary["id"])
        elif result["outcome"] == "validation_pass":
            live_receipt = capture_live_receipt(profile, task)
            event_id = database._insert_event(task["id"], "validation_passed", {"head_sha": actual_head, "execution_receipt": live_receipt}, role_run_id=run["id"])
            verify_persisted_receipt(database, str(task["id"]), event_id, live_receipt)
            _transition(database, task, "FINAL_MECHANICAL_ACCEPTANCE")
        elif result["outcome"] == "correction_required":
            specialized_evidence = database.connection.execute("SELECT 1 FROM findings WHERE task_id = ? AND status = 'licensed' LIMIT 1", (task["id"],)).fetchone()
            if state == "ADVERSARIAL_REVIEW" and not any(f["classification"] == "licensed correction" for f in result["findings"]):
                specialized_evidence = None
            if not specialized_evidence and not any(f["classification"] == "licensed correction" for f in result["findings"]):
                raise LifecycleError("correction requires a licensed finding from the actual review or Architect execution")
            _transition(database, task, "QUEUED")
        for finding in result["findings"]:
            _insert_finding(database, task, run, finding)
    else:
        if _expected_specialized_role(database, task, run) != role:
            raise LifecycleError("specialized role is not eligible in the current lifecycle state")
        expected_verdict = {"PROJECT-MANAGER": "APPROVE", "PLANNER": "COMPLETE", "IMPLEMENTER": "COMPLETE", "REVIEWER": "APPROVE", "ADVERSARY": "PASS", "ARCHIVIST": "COMPLETE"}[role]
        if result["outcome"] not in {"role_complete", "archive_complete", "blocked"} or (result["outcome"] == "archive_complete" and role != "ARCHIVIST"):
            raise LifecycleError("specialized role returned an invalid lifecycle outcome")
        if result["outcome"] == "blocked" and packet["verdict"] != "BLOCKED":
            raise LifecycleError("blocked specialized execution must return a BLOCKED packet")
        allowed_verdicts = {expected_verdict}
        if role in {"REVIEWER", "ADVERSARY"}:
            allowed_verdicts.add("FINDINGS")
        if result["outcome"] != "blocked" and packet["verdict"] not in allowed_verdicts:
            raise LifecycleError("specialized role verdict is not licensed")
        if role == "IMPLEMENTER" and result["outcome"] != "blocked" and actual_head == selected_head:
            raise LifecycleError("Implementer did not produce a new committed HEAD")
        if role != "IMPLEMENTER" and actual_head != selected_head:
            raise LifecycleError("read-only specialized role changed Git HEAD")
        if packet["head_sha"] not in (None, actual_head):
            raise LifecycleError("specialized packet HEAD does not match trusted Git HEAD")
        if result["workpad_body"] != workpad["body"]:
            raise LifecycleError("specialized role attempted to author the lifecycle workpad")
        for finding in packet["findings"]:
            _insert_finding(database, task, run, finding)
        if result["requested_resolved_finding_ids"] and role != "IMPLEMENTER":
            raise LifecycleError("only Implementer may request correction resolution")
        for finding_id in result["requested_resolved_finding_ids"]:
            database.connection.execute("UPDATE findings SET status = 'resolved', licensed_correction_round = NULL WHERE id = ? AND task_id = ? AND status = 'licensed'", (finding_id, task["id"]))
            if database.connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LifecycleError("requested finding resolution is not licensed")
        if role == "IMPLEMENTER" and result["outcome"] != "blocked":
            database.connection.execute("UPDATE tasks SET current_head = ?, updated_at = ? WHERE id = ?", (actual_head, _now(), task["id"]))
        if role == "ARCHIVIST" and result["outcome"] == "archive_complete" and not database.connection.execute("SELECT 1 FROM task_events WHERE task_id = ? AND event_type = 'validation_passed' LIMIT 1", (task["id"],)).fetchone():
            raise LifecycleError("Archivist cannot run before final mechanical acceptance")
    status = "blocked" if result["outcome"] == "blocked" else "finished"
    _finish_run(
        database, run, receipt, status, str(result["summary"]), actual_head,
        str(result["outcome"]), packet.get("verdict") if isinstance(packet, dict) else None,
        result["authorized_write_paths"],
    )
    if role == "ARCHITECT" and result["workpad_body"] != workpad["body"]:
        if not str(result["workpad_body"]).startswith(WORKPAD_MARKER):
            raise LifecycleError("workpad body must preserve its marker")
        database.connection.execute("UPDATE workpads SET body = ?, version = version + 1, updated_at = ? WHERE task_id = ? AND version = ?", (result["workpad_body"], _now(), task["id"], workpad["version"]))
        if database.connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise StateConflict("workpad compare-and-set failed")
    return database.read_task(str(task["id"]))


def _dispatch_receipt(profile: Profile, marker: dict[str, object]) -> dict[str, object]:
    namespace = lifecycle_root(profile, str(marker["identifier"]), str(marker["role_run_id"]))
    receipt = _read_host_json(namespace / "host" / "execution.json", "execution receipt")
    if (
        not isinstance(receipt, dict)
        or receipt.get("role") != marker.get("dispatch_role")
        or receipt.get("role_run_id") != marker.get("role_run_id")
        or receipt.get("status") not in {"started", "finished", "failed"}
        or not isinstance(receipt.get("started_at"), str)
    ):
        raise LifecycleError("execution receipt is not bound to the host dispatch")
    return receipt


def _observed_head(workspace: pathlib.Path, task: dict[str, object]) -> str:
    try:
        result = run_git(workspace, "rev-parse", "HEAD")
    except Exception:
        return str(task["current_head"] or task["base_sha"])
    head = result.stdout.strip()
    if result.returncode == 0 and SHA_RE.fullmatch(head):
        return head
    return str(task["current_head"] or task["base_sha"])


def _record_failed_execution(
    database: ControlPlaneDatabase,
    task: dict[str, object],
    marker: dict[str, object],
    receipt: dict[str, object],
    detail: str,
    head: str,
) -> None:
    run = _materialize_started_run(database, task, marker, receipt)
    if run["status"] == "started":
        _finish_run(database, run, receipt, "failed", detail[:MAX_SUMMARY_BYTES], head)
    blocker_id = str(uuid.uuid4())
    database.connection.execute(
        "INSERT INTO blockers(id, task_id, kind, body, status, created_at, resolved_at) VALUES (?, ?, 'infrastructure', ?, 'open', ?, NULL)",
        (blocker_id, task["id"], detail[:MAX_SUMMARY_BYTES], _now()),
    )
    database._insert_event(task["id"], "infrastructure_blocked", {"blocker_id": blocker_id, "kind": "infrastructure"})


def reconcile(profile: Profile, workspace: pathlib.Path) -> dict[str, object]:
    workspace = physical_directory(workspace)
    physical_directory(workspace / ".git")
    facts, _ = local_task_facts(profile, workspace)
    marker = _read_host_json(workspace / ".git" / "symphony-preparation.json", "preparation marker")
    if not isinstance(marker, dict) or marker.get("role_run_id") is None or marker.get("dispatch_role") is None:
        raise LifecycleError("preparation marker has no role dispatch identity")
    receipt = _dispatch_receipt(profile, marker)
    namespace = lifecycle_root(profile, facts.identifier, str(marker["role_run_id"]))
    with ControlPlaneDatabase.open(control_database_path(profile)) as database:
        task = database.read_task(facts.task_uuid)
        if receipt["status"] != "finished":
            with database._transaction():
                _record_failed_execution(
                    database, task, marker, receipt,
                    "Codex execution did not reach a successful terminal result",
                    _observed_head(workspace, task),
                )
            _release_dispatch_claim(workspace)
            return task
        with database._transaction():
            run = _materialize_started_run(database, task, marker, receipt)
        try:
            result = read_result(namespace / "outbox" / "result.json")
            actual_head = verify_git_truth(profile, workspace, task)
            if result["role"] == "ARCHITECT" and result["outcome"] == "planning_complete":
                _resolve_authorized_write_roots(workspace, result["authorized_write_paths"])
            with database._transaction():
                reconciled = _reconcile(database, task, result, run, receipt, actual_head, profile)
                _release_dispatch_claim(workspace)
                return reconciled
        except Exception:
            with database._transaction():
                current = database.read_role_run(str(marker["role_run_id"]))
                if current["status"] == "started":
                    _finish_run(
                        database, current, receipt, "failed", "invalid lifecycle packet",
                        _observed_head(workspace, task),
                    )
                blocker_id = str(uuid.uuid4())
                database.connection.execute("INSERT INTO blockers(id, task_id, kind, body, status, created_at, resolved_at) VALUES (?, ?, 'infrastructure', ?, 'open', ?, NULL)", (blocker_id, task["id"], "role packet rejected", _now()))
                database._insert_event(task["id"], "infrastructure_blocked", {"blocker_id": blocker_id, "kind": "infrastructure"})
            _release_dispatch_claim(workspace)
            raise


def fail_attempt_for_workspace(profile: Profile, workspace: pathlib.Path, detail: str) -> None:
    workspace = physical_directory(workspace)
    marker_path = workspace / ".git" / "symphony-preparation.json"
    try:
        marker = _read_host_json(marker_path, "preparation marker")
    except LifecycleError:
        facts, _ = local_task_facts(profile, workspace)
        _record_infrastructure_blocker(profile, facts.task_uuid, detail)
        _release_dispatch_claim(workspace)
        return
    if not isinstance(marker, dict) or not marker.get("role_run_id"):
        facts, _ = local_task_facts(profile, workspace)
        _record_infrastructure_blocker(profile, facts.task_uuid, detail)
        _release_dispatch_claim(workspace)
        return
    task_id = marker.get("task_uuid")
    if not isinstance(task_id, str) or not UUID_RE.fullmatch(task_id):
        facts, _ = local_task_facts(profile, workspace)
        task_id = facts.task_uuid
    try:
        receipt = _dispatch_receipt(profile, marker)
    except LifecycleError:
        receipt = None
    with ControlPlaneDatabase.open(control_database_path(profile)) as database:
        task = database.read_task(task_id)
        with database._transaction():
            if receipt is not None and isinstance(receipt.get("started_at"), str):
                _record_failed_execution(
                    database, task, marker, receipt, detail,
                    _observed_head(workspace, task),
                )
            else:
                blocker_id = str(uuid.uuid4())
                database.connection.execute("INSERT INTO blockers(id, task_id, kind, body, status, created_at, resolved_at) VALUES (?, ?, 'infrastructure', ?, 'open', ?, NULL)", (blocker_id, task["id"], detail[:MAX_SUMMARY_BYTES], _now()))
                database._insert_event(task["id"], "infrastructure_blocked", {"blocker_id": blocker_id, "kind": "infrastructure"})
    _release_dispatch_claim(workspace)


def reconcile_orphaned_architect_attempts(profile: Profile, *, managed_runtime_stopped: bool) -> list[dict[str, object]]:
    if managed_runtime_stopped is not True:
        raise LifecycleError("orphan reconciliation requires a stopped managed Runtime")
    repaired = []
    with ControlPlaneDatabase.open(control_database_path(profile)) as database:
        rows = database.connection.execute("SELECT role_runs.*, tasks.current_head, tasks.base_sha FROM role_runs JOIN tasks ON tasks.id = role_runs.task_id WHERE tasks.project_slug = ? AND role_runs.status = 'started' ORDER BY role_runs.started_at, role_runs.id", (profile.slug,)).fetchall()
        with database._transaction():
            for row in rows:
                run = dict(row)
                _finish_run(database, run, {"finished_at": _now()}, "failed", "Managed Runtime stopped before role attempt reconciled.", str(run["current_head"] or run["base_sha"]))
                repaired.append({"task_id": str(run["task_id"]), "role_run_id": str(run["id"]), "role": run["role"], "round": int(run["round"])})
    return repaired
