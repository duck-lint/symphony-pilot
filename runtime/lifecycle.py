#!/usr/bin/env python3
"""Trusted Step-6 SQLite lifecycle broker.

The Architect result is hostile input.  This module derives every identity,
path, role round, Git fact, and next state from host-owned state, then applies
one accepted result in a single SQLite transaction.  It deliberately contains
no GitHub, publication, credential, or App Server execution path.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import stat
import subprocess
import uuid
from control_db import ControlPlaneDatabase, ControlPlaneError, StateConflict
from prepare_workspace import (Profile, control_database_path,
                               local_task_facts, require_physical_namespace)


RESULT_SCHEMA = "symphony-pilot-lifecycle-result/v1"
WORKPAD_MARKER = "<!-- symphony-workpad:v1 -->"
MAX_RESULT_BYTES = 128 * 1024
MAX_WORKPAD_BYTES = 64 * 1024
MAX_SUMMARY_BYTES = 12 * 1024
TASK_IDENTIFIER_RE = re.compile(r"^T-[0-9]{6}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SECRET_MARKER_RE = re.compile(
    r"(?:gh[pousr]_|github_pat_|sk-[A-Za-z0-9]|BEGIN [A-Z ]*PRIVATE KEY|Bearer\s+)", re.I
)
ACTIVE_STATES = (
    "QUEUED", "PLANNED", "IMPLEMENTED", "REVIEW",
    "ADVERSARIAL_REVIEW", "FINAL_MECHANICAL_ACCEPTANCE",
)
ROLE_NAMES = {"PROJECT-MANAGER", "PLANNER", "IMPLEMENTER", "REVIEWER", "ADVERSARY", "ARCHIVIST"}
OUTCOMES = {
    "planning_complete", "implementation_complete", "correction_complete",
    "review_approved", "adversary_pass", "validation_pass", "archive_complete",
    "correction_required", "blocked",
}
FINDING_CLASSES = {"licensed correction", "unresolved project decision", "infrastructure condition", "rejected"}
BLOCKER_KINDS = {None, "human", "project", "infrastructure"}
FINDING_FIELDS = {"role", "kind", "severity", "body", "classification", "blocker_kind"}
ROLE_PACKET_FIELDS = {"role", "verdict", "summary", "head_sha", "findings"}
RESULT_FIELDS = {
    "schema", "task_uuid", "identifier", "architect_role_run_id", "expected_state",
    "expected_workpad_version", "expected_starting_head", "workpad_body", "summary",
    "outcome", "role_results", "findings", "requested_resolved_finding_ids",
}


class LifecycleError(ControlPlaneError):
    """A lifecycle result or host lifecycle invariant is invalid."""


class AllocationConflict(LifecycleError):
    """A caller lost a host-owned allocation race or hit a normal precondition."""


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
    """Derive one run namespace from validated profile/task/role identities."""
    if not TASK_IDENTIFIER_RE.fullmatch(identifier) or not UUID_RE.fullmatch(run_id):
        raise LifecycleError("lifecycle namespace identity is invalid")
    state_root = require_physical_namespace(profile.state_root)
    if state_root.is_symlink() or (state_root.exists() and not state_root.is_dir()):
        raise LifecycleError("lifecycle state root is unsafe")
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_root.chmod(0o700)
    root = state_root / "lifecycle" / identifier / run_id
    # Check every derived component before creating the namespace. A check only
    # on the leaf would allow a pre-existing lifecycle/T-N symlink to redirect
    # an otherwise host-derived run into a sibling task or arbitrary storage.
    for directory in (state_root / "lifecycle", state_root / "lifecycle" / identifier, root):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise LifecycleError("lifecycle namespace contains an unsafe path component")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    for name in ("inbox", "outbox"):
        directory = root / name
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise LifecycleError("lifecycle namespace contains an unsafe directory")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    return root


def _initial_workpad(task: dict[str, object]) -> str:
    return "\n".join((
        WORKPAD_MARKER,
        "## Symphony Workpad",
        "",
        f"- Task: {task['identifier']}",
        f"- Objective: {task['objective']}",
        f"- Base: {task['base_ref']} @ {task['base_sha']}",
        f"- Branch: {task['branch']}",
        f"- Lifecycle state: {task['state']}",
        "",
    ))


def _open_started_architect(database: ControlPlaneDatabase, task_id: str) -> dict[str, object] | None:
    row = database.connection.execute(
        "SELECT * FROM role_runs WHERE task_id = ? AND role = 'ARCHITECT' AND status = 'started'",
        (task_id,),
    ).fetchall()
    if len(row) > 1:
        raise LifecycleError("more than one Architect attempt is started")
    return dict(row[0]) if row else None


def _next_round(database: ControlPlaneDatabase, task_id: str, role: str) -> int:
    row = database.connection.execute(
        "SELECT COALESCE(MAX(round), 0) + 1 FROM role_runs WHERE task_id = ? AND role = ?",
        (task_id, role),
    ).fetchone()
    return int(row[0])


def _insert_event(database: ControlPlaneDatabase, task_id: str, event_type: str,
                  payload: object, *, role_run_id: str | None = None) -> None:
    database._insert_event(task_id, event_type, payload, role_run_id=role_run_id, occurred_at=_now())


def _write_host_json(path: pathlib.Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _packet(database: ControlPlaneDatabase, task: dict[str, object], run_id: str) -> dict[str, object]:
    workpad = database.read_workpad(str(task["id"]))
    if workpad is None:
        raise LifecycleError("active task has no canonical workpad")
    open_findings = database.connection.execute(
        "SELECT id, kind, severity, body, status, licensed_correction_round FROM findings "
        "WHERE task_id = ? AND status IN ('open', 'accepted', 'licensed') ORDER BY rowid",
        (task["id"],),
    ).fetchall()
    blockers = database.connection.execute(
        "SELECT kind, body FROM blockers WHERE task_id = ? AND status = 'open' ORDER BY created_at, id",
        (task["id"],),
    ).fetchall()
    state = str(task["state"])
    if state in {"IMPLEMENTED", "REVIEW", "ADVERSARIAL_REVIEW"} and any(
        row["status"] == "licensed" for row in open_findings
    ):
        next_action = "IMPLEMENTER_CORRECTION"
    else:
        next_action = {
            "QUEUED": "AUTHORITY_AND_PLANNING",
            "PLANNED": "IMPLEMENTATION",
            "IMPLEMENTED": "REVIEW",
            "REVIEW": "ADVERSARIAL_REVIEW",
            "ADVERSARIAL_REVIEW": "FINAL_MECHANICAL_VALIDATION",
            "FINAL_MECHANICAL_ACCEPTANCE": "ARCHIVIST_CLOSEOUT",
        }.get(state)
    return {
        "schema": "symphony-pilot-lifecycle-input/v1",
        "task_uuid": task["id"], "identifier": task["identifier"],
        "title": task["title"], "objective": task["objective"],
        "current_state": state, "next_expected_action": next_action,
        "base_ref": task["base_ref"], "base_sha": task["base_sha"],
        "branch": task["branch"],
        "selected_head": task["current_head"] or task["base_sha"],
        "workpad": {"body": workpad["body"], "version": workpad["version"]},
        "open_findings": [dict(row) for row in open_findings],
        "licensed_finding_ids": [str(row["id"]) for row in open_findings if row["status"] == "licensed"],
        "open_blockers": [dict(row) for row in blockers],
        "architect_role_run_id": run_id,
    }


def _record_task_infrastructure_blocker(profile: Profile, task_id: str, detail: str) -> None:
    """Persist a task blocker when allocation fails before a role row exists."""
    with ControlPlaneDatabase.open(control_database_path(profile)) as database:
        task = database.read_task(task_id)
        with database._transaction():
            _insert_blocker(database, task, "infrastructure", detail)


def _allocate_architect_attempt(profile: Profile, facts):
    """Atomically allocate one Architect attempt, including its input packet."""
    task: dict[str, object]
    packet: dict[str, object]
    run_id: str | None = None
    task_identity_known = False
    try:
        with ControlPlaneDatabase.open(control_database_path(profile)) as database:
            # The check, round allocation, UUID allocation, role row, and start
            # event share one BEGIN IMMEDIATE transaction. A concurrent loser sees
            # the committed started row and exits without adding a blocker.
            with database._transaction():
                task = database.read_task(facts.task_uuid)
                task_identity_known = True
                if task["project_slug"] != profile.slug or task["identifier"] != facts.identifier:
                    raise AllocationConflict("task identity is not project-scoped")
                if task["state"] not in ACTIVE_STATES:
                    raise AllocationConflict(f"task state is not Step-6 active: {task['state']}")
                if database.connection.execute(
                    "SELECT 1 FROM blockers WHERE task_id = ? AND status = 'open' LIMIT 1", (facts.task_uuid,)
                ).fetchone():
                    raise AllocationConflict("task has an open blocker")
                if _open_started_architect(database, facts.task_uuid) is not None:
                    raise AllocationConflict("an Architect attempt is already started")
                workpad = database.read_workpad(facts.task_uuid)
                if workpad is None:
                    database.connection.execute(
                        "INSERT INTO workpads(task_id, body, version, updated_at) VALUES (?, ?, 1, ?)",
                        (facts.task_uuid, _initial_workpad(task), _now()),
                    )
                    workpad = database.read_workpad(facts.task_uuid)
                run_id = str(uuid.uuid4())
                round_number = _next_round(database, facts.task_uuid, "ARCHITECT")
                packet = _packet(database, task, run_id)
                timestamp = _now()
                database.connection.execute(
                    "INSERT INTO role_runs(id, task_id, role, round, head_sha, status, started_at, finished_at, result_summary) "
                    "VALUES (?, ?, 'ARCHITECT', ?, ?, 'started', ?, NULL, NULL)",
                    (run_id, facts.task_uuid, round_number, facts.selected_head, timestamp),
                )
                database._insert_event(
                    facts.task_uuid, "architect_started",
                    {"task_state": task["state"], "selected_head": facts.selected_head,
                     "workpad_version": workpad["version"],
                     "expected_next_action": packet["next_expected_action"],
                     "task_identifier": facts.identifier},
                    role_run_id=run_id, occurred_at=timestamp,
                )
            run = database.read_role_run(run_id)
    except AllocationConflict:
        raise
    except LifecycleError as exc:
        if task_identity_known:
            detail = f"Architect allocation failed: {type(exc).__name__}"
            try:
                _record_task_infrastructure_blocker(profile, facts.task_uuid, detail)
            except Exception as compensation_error:
                raise LifecycleError("Architect allocation failed and task compensation failed") from compensation_error
            raise LifecycleError(detail) from exc
        raise
    except Exception as exc:
        detail = f"Architect allocation failed: {type(exc).__name__}"
        try:
            _record_task_infrastructure_blocker(profile, facts.task_uuid, detail)
        except Exception as compensation_error:
            raise LifecycleError("Architect allocation failed and task compensation failed") from compensation_error
        raise LifecycleError(detail) from exc
    return task, packet, run_id, run


def prepare_attempt(profile: Profile, workspace: pathlib.Path) -> dict[str, object]:
    """Allocate the sole Architect attempt and its fixed lifecycle mounts."""
    workspace = require_physical_namespace(workspace.resolve())
    facts, _ = local_task_facts(profile, workspace)
    if facts.branch != _git(workspace, "branch", "--show-current"):
        raise LifecycleError("workspace is not on the host-owned task branch")
    task, packet, run_id, run = _allocate_architect_attempt(profile, facts)
    try:
        namespace = lifecycle_root(profile, facts.identifier, run_id)
        _write_host_json(namespace / "inbox" / "lifecycle.json", packet)
        marker_path = workspace / ".git" / "symphony-preparation.json"
        marker = _read_host_json(marker_path, "preparation marker")
        if not isinstance(marker, dict):
            raise LifecycleError("preparation marker is malformed")
        marker.update({"architect_role_run_id": run_id, "lifecycle_namespace": str(namespace),
                       "lifecycle_packet": "inbox/lifecycle.json"})
        _write_host_json(marker_path, marker)
    except Exception as exc:
        detail = f"lifecycle staging failed: {type(exc).__name__}"
        try:
            _fail_started_attempt(profile, facts.task_uuid, run_id, detail)
        except Exception as compensation_error:
            raise LifecycleError("lifecycle staging failed and compensation failed") from compensation_error
        raise LifecycleError(detail) from exc
    return {"task": task, "run": run, "packet": packet, "namespace": str(namespace)}


def _read_host_json(path: pathlib.Path, label: str) -> object:
    flags = (
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
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
    except OSError as exc:
        raise LifecycleError(f"{label} cannot be read") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if len(raw) > MAX_RESULT_BYTES:
        raise LifecycleError(f"{label} exceeds the byte bound")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise LifecycleError(f"{label} is malformed UTF-8 JSON") from exc


def read_result(path: pathlib.Path) -> dict[str, object]:
    value = _read_host_json(path, "lifecycle result")
    if not isinstance(value, dict) or set(value) != RESULT_FIELDS:
        raise LifecycleError("lifecycle result fields are invalid")
    if value["schema"] != RESULT_SCHEMA:
        raise LifecycleError("lifecycle result schema is invalid")
    for field in ("task_uuid", "architect_role_run_id"):
        if not isinstance(value[field], str) or not UUID_RE.fullmatch(value[field]):
            raise LifecycleError(f"lifecycle {field} is invalid")
    if not isinstance(value["identifier"], str) or not TASK_IDENTIFIER_RE.fullmatch(value["identifier"]):
        raise LifecycleError("lifecycle identifier is invalid")
    if value["expected_state"] not in ACTIVE_STATES:
        raise LifecycleError("lifecycle expected state is invalid")
    if (not isinstance(value["expected_workpad_version"], int) or isinstance(value["expected_workpad_version"], bool)
            or value["expected_workpad_version"] < 1):
        raise LifecycleError("lifecycle expected workpad version is invalid")
    if not isinstance(value["expected_starting_head"], str) or not SHA_RE.fullmatch(value["expected_starting_head"]):
        raise LifecycleError("lifecycle expected starting HEAD is invalid")
    _bounded_text(value["workpad_body"], "workpad_body", MAX_WORKPAD_BYTES)
    _bounded_text(value["summary"], "summary", MAX_SUMMARY_BYTES)
    if value["outcome"] not in OUTCOMES:
        raise LifecycleError("lifecycle outcome is not licensed")
    if not isinstance(value["role_results"], list) or not isinstance(value["findings"], list):
        raise LifecycleError("lifecycle role results/findings must be lists")
    if not isinstance(value["requested_resolved_finding_ids"], list):
        raise LifecycleError("lifecycle requested resolutions must be a list")
    for finding_id in value["requested_resolved_finding_ids"]:
        if not isinstance(finding_id, str) or not UUID_RE.fullmatch(finding_id):
            raise LifecycleError("lifecycle requested finding identity is invalid")
    seen_roles: set[str] = set()
    def validate_finding(finding: object, expected_role: str | None = None) -> None:
        if not isinstance(finding, dict) or set(finding) != FINDING_FIELDS:
            raise LifecycleError("lifecycle finding fields are invalid")
        if finding["role"] not in seen_roles and finding["role"] != "ARCHITECT":
            raise LifecycleError("lifecycle finding role is not licensed")
        if expected_role is not None and finding["role"] != expected_role:
            raise LifecycleError("lifecycle finding provenance does not match its role packet")
        if finding["severity"] not in {"info", "low", "medium", "high", "critical"}:
            raise LifecycleError("lifecycle finding severity is invalid")
        if finding["classification"] not in FINDING_CLASSES:
            raise LifecycleError("lifecycle finding classification is invalid")
        if finding["blocker_kind"] not in BLOCKER_KINDS:
            raise LifecycleError("lifecycle blocker kind is invalid")
        expected_blocker_kind = {
            "licensed correction": None,
            "rejected": None,
            "unresolved project decision": {"human", "project"},
            "infrastructure condition": {"infrastructure"},
        }[finding["classification"]]
        if expected_blocker_kind is None and finding["blocker_kind"] is not None:
            raise LifecycleError("lifecycle blocker kind is not valid for this finding")
        if isinstance(expected_blocker_kind, set) and finding["blocker_kind"] not in expected_blocker_kind:
            raise LifecycleError("lifecycle blocker kind does not match finding classification")
        _bounded_text(finding["kind"], "finding kind", 256)
        _bounded_text(finding["body"], "finding body", MAX_SUMMARY_BYTES)

    for packet in value["role_results"]:
        if not isinstance(packet, dict) or set(packet) != ROLE_PACKET_FIELDS:
            raise LifecycleError("lifecycle role packet fields are invalid")
        role = packet["role"]
        if role not in ROLE_NAMES or role in seen_roles:
            raise LifecycleError("lifecycle role is not licensed or is duplicated")
        seen_roles.add(role)
        _bounded_text(packet["summary"], "role summary", MAX_SUMMARY_BYTES)
        if packet["head_sha"] is not None and (not isinstance(packet["head_sha"], str) or not SHA_RE.fullmatch(packet["head_sha"])):
            raise LifecycleError("lifecycle role HEAD is invalid")
        if not isinstance(packet["verdict"], str) or packet["verdict"] not in {
            "APPROVE", "PASS", "COMPLETE", "FINDINGS", "BLOCKED"
        }:
            raise LifecycleError("lifecycle role verdict is invalid")
        if not isinstance(packet["findings"], list):
            raise LifecycleError("lifecycle role findings must be a list")
        for finding in packet["findings"]:
            validate_finding(finding, role)
    for finding in value["findings"]:
        validate_finding(finding)
    return value


def _git(workspace: pathlib.Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        raise LifecycleError(f"Git verification failed: {detail[:240]}")
    return result.stdout.strip()


def verify_git_truth(profile: Profile, workspace: pathlib.Path, task: dict[str, object]) -> str:
    """Establish repository, branch, cleanliness, HEAD, and base ancestry."""
    remote = _git(workspace, "remote", "get-url", "origin")
    if remote != profile.git_remote:
        raise LifecycleError("workspace repository identity does not match the registered remote")
    if _git(workspace, "branch", "--show-current") != task["branch"]:
        raise LifecycleError("workspace branch is not the host-owned task branch")
    if _git(workspace, "status", "--porcelain=v1", "--untracked-files=all"):
        raise LifecycleError("workspace is dirty after the Architect attempt")
    head = _git(workspace, "rev-parse", "HEAD")
    if not SHA_RE.fullmatch(head):
        raise LifecycleError("workspace HEAD is not a commit SHA")
    if subprocess.run(["git", "merge-base", "--is-ancestor", str(task["base_sha"]), head],
                      cwd=workspace, check=False).returncode:
        raise LifecycleError("workspace HEAD is not descended from the accepted base")
    return head


def _state_transition(database: ControlPlaneDatabase, task: dict[str, object], new_state: str) -> None:
    timestamp = _now()
    database.connection.execute("UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?", (new_state, timestamp, task["id"]))


def _insert_role_result(database: ControlPlaneDatabase, task: dict[str, object], packet: dict[str, object], head: str) -> str:
    run_id = str(uuid.uuid4())
    role = str(packet["role"])
    round_number = _next_round(database, str(task["id"]), role)
    timestamp = _now()
    database.connection.execute(
        "INSERT INTO role_runs(id, task_id, role, round, head_sha, status, started_at, finished_at, result_summary) "
        "VALUES (?, ?, ?, ?, ?, 'started', ?, NULL, NULL)",
        (run_id, task["id"], role, round_number, packet["head_sha"] or head, timestamp),
    )
    database._insert_event(task["id"], "role_started", {"role": role, "round": round_number}, role_run_id=run_id, occurred_at=timestamp)
    database.connection.execute(
        "UPDATE role_runs SET status = 'finished', finished_at = ?, result_summary = ? WHERE id = ?",
        (timestamp, packet["summary"], run_id),
    )
    database._insert_event(task["id"], "role_finished", {"role": role, "round": round_number, "status": "finished"}, role_run_id=run_id, occurred_at=timestamp)
    return run_id


def _insert_finding(database: ControlPlaneDatabase, task: dict[str, object], role_run_id: str,
                    finding: dict[str, object], correction_round: int | None) -> str:
    finding_id = str(uuid.uuid4())
    classification = finding["classification"]
    status = {"licensed correction": "licensed", "rejected": "rejected"}.get(classification, "accepted")
    database.connection.execute(
        "INSERT INTO findings(id, task_id, role_run_id, kind, severity, body, status, licensed_correction_round) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (finding_id, task["id"], role_run_id, finding["kind"], finding["severity"], finding["body"], status,
         correction_round if status == "licensed" else None),
    )
    _insert_event(database, task["id"], "finding_recorded",
                  {"finding_id": finding_id, "role_run_id": role_run_id, "status": status}, role_run_id=role_run_id)
    if classification == "infrastructure condition":
        _insert_blocker(database, task, "infrastructure", str(finding["body"]))
    elif classification == "unresolved project decision":
        # The finite blocker_kind field, not hostile descriptive prose, carries
        # the escalation authority distinction.
        _insert_blocker(database, task, str(finding["blocker_kind"]), str(finding["body"]))
    return finding_id


def _insert_blocker(database: ControlPlaneDatabase, task: dict[str, object], kind: str, body: str) -> str:
    blocker_id = str(uuid.uuid4())
    timestamp = _now()
    database.connection.execute(
        "INSERT INTO blockers(id, task_id, kind, body, status, created_at, resolved_at) VALUES (?, ?, ?, ?, 'open', ?, NULL)",
        (blocker_id, task["id"], kind, body, timestamp),
    )
    event_type = {"human": "human_blocked", "infrastructure": "infrastructure_blocked"}.get(kind)
    if event_type:
        _insert_event(database, task["id"], event_type, {"blocker_id": blocker_id, "kind": kind})
    return blocker_id


def _finish_architect(database: ControlPlaneDatabase, task: dict[str, object], run_id: str, summary: str, head: str, status: str = "finished") -> None:
    timestamp = _now()
    run = database.read_role_run(run_id)
    database.connection.execute(
        "UPDATE role_runs SET status = ?, finished_at = ?, result_summary = ?, head_sha = ? "
        "WHERE id = ? AND task_id = ? AND status = 'started'",
        (status, timestamp, summary, head, run_id, task["id"]),
    )
    if database.connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise StateConflict("Architect attempt is no longer started")
    _insert_event(database, task["id"], "role_finished",
                  {"role": "ARCHITECT", "round": run["round"], "status": status}, role_run_id=run_id)


def _fail_started_attempt(profile: Profile, task_id: str, run_id: str, detail: str) -> None:
    """Compensate a post-allocation staging failure using the exact run id."""
    with ControlPlaneDatabase.open(control_database_path(profile)) as database:
        task = database.read_task(task_id)
        run = database.read_role_run(run_id)
        if run["task_id"] != task["id"]:
            raise StateConflict("staging failure run does not belong to its task")
        with database._transaction():
            if run["status"] == "started":
                _finish_architect(database, task, run_id, detail,
                                   str(task["current_head"] or task["base_sha"]), "failed")
                _insert_blocker(database, task, "infrastructure", detail)


def _current_acceptance(database: ControlPlaneDatabase, task_id: str, event_type: str, head: str) -> bool:
    events = database.connection.execute(
        "SELECT rowid, event_type, payload_json FROM task_events WHERE task_id = ? ORDER BY rowid", (task_id,)
    ).fetchall()
    latest_head_change = max((int(row[0]) for row in events if row[1] == "head_changed"), default=0)
    for row in reversed(events):
        if int(row[0]) <= latest_head_change:
            break
        if row[1] == event_type:
            try:
                payload = json.loads(row[2])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("head_sha") == head:
                return True
    return False


def _reconcile(database: ControlPlaneDatabase, result: dict[str, object], actual_head: str) -> dict[str, object]:
    task = database.read_task(str(result["task_uuid"]))
    run_id = str(result["architect_role_run_id"])
    if task["identifier"] != result["identifier"]:
        raise LifecycleError("lifecycle task identifier does not match UUID")
    if task["state"] != result["expected_state"]:
        raise StateConflict("lifecycle result expected state is stale")
    run = database.read_role_run(run_id)
    if run["task_id"] != task["id"] or run["role"] != "ARCHITECT" or run["status"] != "started":
        raise StateConflict("lifecycle Architect attempt is stale or mismatched")
    workpad = database.read_workpad(task["id"])
    if workpad is None or workpad["version"] != result["expected_workpad_version"]:
        raise StateConflict("lifecycle workpad version is stale")
    selected_head = task["current_head"] or task["base_sha"]
    if selected_head != result["expected_starting_head"]:
        raise StateConflict("lifecycle starting HEAD is stale")
    state = str(task["state"])
    has_licensed = database.connection.execute(
        "SELECT 1 FROM findings WHERE task_id = ? AND status = 'licensed' LIMIT 1", (task["id"],)
    ).fetchone() is not None
    if state in {"IMPLEMENTED", "REVIEW", "ADVERSARIAL_REVIEW"} and has_licensed:
        expected_roles = {"IMPLEMENTER"}
        allowed_outcomes = {"correction_complete", "blocked"}
    else:
        expected_roles, allowed_outcomes = {
            "QUEUED": ({"PROJECT-MANAGER", "PLANNER"}, {"planning_complete", "blocked"}),
            "PLANNED": ({"IMPLEMENTER"}, {"implementation_complete", "blocked"}),
            "IMPLEMENTED": ({"REVIEWER"}, {"review_approved", "correction_required", "blocked"}),
            "REVIEW": ({"ADVERSARY"}, {"adversary_pass", "correction_required", "blocked"}),
            "ADVERSARIAL_REVIEW": (set(), {"validation_pass", "correction_required", "blocked"}),
            "FINAL_MECHANICAL_ACCEPTANCE": ({"ARCHIVIST"}, {"archive_complete", "blocked"}),
        }[state]
    packets = {str(packet["role"]): packet for packet in result["role_results"]}
    outcome = str(result["outcome"])
    if outcome not in allowed_outcomes:
        raise LifecycleError("lifecycle outcome is impossible for the current next action")
    if outcome == "blocked":
        if not set(packets).issubset(expected_roles):
            raise LifecycleError("blocked lifecycle result contains an unlicensed role")
    elif set(packets) != expected_roles:
        raise LifecycleError("lifecycle outcome is impossible for the current next action")
    expected_verdicts = {
        "PROJECT-MANAGER": "APPROVE", "PLANNER": "COMPLETE", "IMPLEMENTER": "COMPLETE",
        "REVIEWER": "APPROVE", "ADVERSARY": "PASS", "ARCHIVIST": "COMPLETE",
    }
    for role, packet in packets.items():
        allowed_verdict = {expected_verdicts[role]}
        if outcome == "correction_required" and role in {"REVIEWER", "ADVERSARY"}:
            allowed_verdict.add("FINDINGS")
        if outcome == "blocked":
            allowed_verdict.add("BLOCKED")
        if packet["verdict"] not in allowed_verdict:
            raise LifecycleError("lifecycle role verdict is not licensed for that role")
    if outcome == "correction_required":
        if state in {"IMPLEMENTED", "REVIEW"}:
            required_role = "REVIEWER" if state == "IMPLEMENTED" else "ADVERSARY"
            if packets[required_role]["verdict"] != "FINDINGS":
                raise LifecycleError("phase correction requires the specialized FINDINGS verdict")
        elif state == "ADVERSARIAL_REVIEW" and packets:
            raise LifecycleError("mechanical-validation correction has no specialized role packet")
    implementation_phase = state == "PLANNED" or (
        state in {"IMPLEMENTED", "REVIEW", "ADVERSARIAL_REVIEW"} and has_licensed
    )
    readonly_phase = not implementation_phase
    if readonly_phase and actual_head != selected_head:
        raise LifecycleError("read-only lifecycle phase changed Git HEAD")
    if implementation_phase and actual_head == selected_head:
        raise LifecycleError("IMPLEMENTER did not produce a new committed HEAD")
    correction_round = _next_round(database, task["id"], "IMPLEMENTER") if outcome == "correction_required" else None
    specialized_runs: dict[str, str] = {}
    for role, packet in packets.items():
        if packet["head_sha"] is not None and packet["head_sha"] != actual_head:
            raise LifecycleError("role packet HEAD does not match trusted Git HEAD")
        specialized_runs[role] = _insert_role_result(database, task, packet, actual_head)
    all_findings = list(result["findings"])
    for packet in packets.values():
        all_findings.extend(packet["findings"])
    classifications = {finding["classification"] for finding in all_findings}
    if "licensed correction" in classifications and outcome != "correction_required":
        raise LifecycleError("a licensed correction must be the explicit lifecycle outcome")
    if classifications & {"unresolved project decision", "infrastructure condition"} and outcome != "blocked":
        raise LifecycleError("an unresolved or infrastructure finding must block lifecycle advancement")
    if outcome == "correction_required":
        licensed_findings = [finding for finding in all_findings if finding["classification"] == "licensed correction"]
        if state in {"IMPLEMENTED", "REVIEW"}:
            required_role = "REVIEWER" if state == "IMPLEMENTED" else "ADVERSARY"
            if not any(finding["role"] == required_role for finding in licensed_findings):
                raise LifecycleError("phase correction requires a licensed finding from its specialized role")
        elif state == "ADVERSARIAL_REVIEW":
            if not any(
                finding["role"] == "ARCHITECT" and finding in result["findings"]
                for finding in licensed_findings
            ):
                raise LifecycleError("mechanical-validation correction requires a top-level Architect finding")
    if outcome == "blocked" and not (
        classifications & {"unresolved project decision", "infrastructure condition"}
        or any(packet["verdict"] == "BLOCKED" for packet in packets.values())
    ):
        raise LifecycleError("blocked lifecycle result has no blocker-producing evidence")
    for finding in all_findings:
        role = str(finding["role"])
        role_run_id = specialized_runs.get(role, run_id)
        _insert_finding(database, task, role_run_id, finding, correction_round)
    if outcome == "review_approved":
        if database.connection.execute(
            "SELECT 1 FROM findings WHERE task_id = ? AND status = 'licensed' LIMIT 1", (task["id"],)
        ).fetchone():
            raise LifecycleError("review approval has unresolved licensed findings")
    if outcome in {"adversary_pass", "validation_pass", "archive_complete"}:
        if not _current_acceptance(database, task["id"], "review_accepted", actual_head):
            raise LifecycleError("current-head reviewer acceptance is missing")
    if outcome in {"validation_pass", "archive_complete"}:
        if not _current_acceptance(database, task["id"], "adversary_accepted", actual_head):
            raise LifecycleError("current-head adversary acceptance is missing")
    if outcome == "correction_required" and not any(
        finding["classification"] == "licensed correction" for finding in all_findings
    ):
        raise LifecycleError("correction outcome has no licensed correction finding")
    if outcome == "correction_complete":
        impl_run = specialized_runs["IMPLEMENTER"]
        impl_round = database.read_role_run(impl_run)["round"]
        licensed_ids = {
            str(row[0]) for row in database.connection.execute(
                "SELECT id FROM findings WHERE task_id = ? AND status = 'licensed'", (task["id"],)
            ).fetchall()
        }
        requested_ids = {str(finding_id) for finding_id in result["requested_resolved_finding_ids"]}
        if requested_ids != licensed_ids:
            raise LifecycleError(
                "correction completion must resolve exactly the task's currently licensed findings"
            )
        for finding_id in result["requested_resolved_finding_ids"]:
            finding = database.read_finding(finding_id)
            if (finding["task_id"] != task["id"] or finding["status"] != "licensed"
                    or finding["licensed_correction_round"] != impl_round):
                raise LifecycleError("finding resolution is not licensed for this correction round")
            database.connection.execute(
                "UPDATE findings SET status = 'resolved', licensed_correction_round = NULL WHERE id = ?",
                (finding_id,),
            )
    elif result["requested_resolved_finding_ids"]:
        raise LifecycleError("finding resolutions are only licensed for an IMPLEMENTER correction")
    if outcome == "blocked" and not database.connection.execute(
        "SELECT 1 FROM blockers WHERE task_id = ? AND status = 'open' LIMIT 1", (task["id"],)
    ).fetchone():
        blocked_packets = [packet for packet in packets.values() if packet["verdict"] == "BLOCKED"]
        evidence = blocked_packets[0]["summary"] if blocked_packets else str(result["summary"])
        _insert_blocker(database, task, "infrastructure", evidence)
    if outcome == "blocked" and not database.connection.execute(
        "SELECT 1 FROM blockers WHERE task_id = ? AND status = 'open' LIMIT 1", (task["id"],)
    ).fetchone():
        raise LifecycleError("blocked lifecycle result did not produce an open blocker")
    if implementation_phase:
        old_head = task["current_head"]
        if old_head != actual_head:
            database.connection.execute(
                "UPDATE tasks SET current_head = ?, updated_at = ? WHERE id = ?",
                (actual_head, _now(), task["id"]),
            )
            _insert_event(database, task["id"], "head_changed", {"current_head": actual_head, "published_head": task["published_head"]}, role_run_id=run_id)
    next_state = {
        "planning_complete": "PLANNED", "implementation_complete": "IMPLEMENTED",
        "review_approved": "REVIEW", "adversary_pass": "ADVERSARIAL_REVIEW",
        "validation_pass": "FINAL_MECHANICAL_ACCEPTANCE", "archive_complete": "ARCHIVIST",
        "correction_complete": "IMPLEMENTED", "correction_required": state,
        "blocked": state,
    }[outcome]
    if outcome not in {"blocked", "correction_required"}:
        _state_transition(database, task, next_state)
    if outcome == "review_approved":
        _insert_event(database, task["id"], "review_accepted", {"head_sha": actual_head}, role_run_id=specialized_runs["REVIEWER"])
    elif outcome == "adversary_pass":
        _insert_event(database, task["id"], "adversary_accepted", {"head_sha": actual_head}, role_run_id=specialized_runs["ADVERSARY"])
    elif outcome == "validation_pass":
        _insert_event(database, task["id"], "validation_passed", {"head_sha": actual_head}, role_run_id=run_id)
    _finish_architect(database, task, run_id, str(result["summary"]), actual_head)
    body = str(result["workpad_body"])
    if not body.startswith(WORKPAD_MARKER):
        raise LifecycleError("workpad body must preserve the v1 marker")
    database.connection.execute(
        "UPDATE workpads SET body = ?, version = version + 1, updated_at = ? WHERE task_id = ? AND version = ?",
        (body, _now(), task["id"], result["expected_workpad_version"]),
    )
    if database.connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise StateConflict("workpad compare-and-set failed")
    return database.read_task(task["id"])


def fail_attempt(profile: Profile, task_id: str, *, detail: str) -> bool:
    """Finish exactly one stale Architect attempt and retain an infrastructure blocker."""
    with ControlPlaneDatabase.open(control_database_path(profile)) as database:
        task = database.read_task(task_id)
        run = _open_started_architect(database, task_id)
        if run is None:
            return False
        with database._transaction():
            _finish_architect(database, task, str(run["id"]), detail, str(task["current_head"] or task["base_sha"]), "failed")
            _insert_blocker(database, task, "infrastructure", _bounded_text(detail, "blocker", MAX_SUMMARY_BYTES))
        return True


def reconcile(profile: Profile, workspace: pathlib.Path) -> dict[str, object]:
    workspace = require_physical_namespace(workspace.resolve())
    facts, _ = local_task_facts(profile, workspace)
    with ControlPlaneDatabase.open(control_database_path(profile)) as database:
        task = database.read_task(facts.task_uuid)
        marker = _read_host_json(workspace / ".git" / "symphony-preparation.json", "preparation marker")
        if not isinstance(marker, dict) or marker.get("architect_role_run_id") is None:
            raise LifecycleError("preparation marker has no Architect attempt identity")
        run_id = str(marker["architect_role_run_id"])
        run = database.read_role_run(run_id)
        if run["task_id"] != task["id"] or run["status"] != "started":
            raise LifecycleError("preparation marker does not identify the started Architect attempt")
        namespace = lifecycle_root(profile, str(task["identifier"]), run_id)
        result = read_result(namespace / "outbox" / "result.json")
        actual_head = verify_git_truth(profile, workspace, task)
        with database._transaction():
            return _reconcile(database, result, actual_head)


def fail_attempt_for_workspace(profile: Profile, workspace: pathlib.Path, detail: str) -> None:
    try:
        facts, _ = local_task_facts(profile, workspace)
        fail_attempt(profile, facts.task_uuid, detail=detail)
    except Exception:
        # A missing/corrupt database cannot safely receive a blocker. Runtime's
        # read-only tracker will fail closed; do not invent local state here.
        raise
