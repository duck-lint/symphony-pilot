#!/usr/bin/env python3
"""Pilot-owned lifecycle routing and evidence reconciliation.

Pilot decides eligibility and issues exact grants. Runtime supplies retained
execution evidence; a role name, packet, or expected position never creates a
role run. This module contains no external scheduler ontology or
legacy role topology.
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
from prepare_workspace import Profile, control_database_path, local_task_facts, require_physical_namespace
from workspace_boundary import atomic_metadata_write, run_git

RESULT_SCHEMA = "symphony-pilot-execution-result/v2"
TASK_IDENTIFIER_RE = re.compile(r"^T-[0-9]{6}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SECRET_MARKER_RE = re.compile(r"(?:gh[pousr]_|github_pat_|\bsk-[A-Za-z0-9]|BEGIN [A-Z ]*PRIVATE KEY|Bearer\s+)", re.I)
ROLES = frozenset({"PROJECT-MANAGER", "PLANNER", "IMPLEMENTER", "REVIEWER", "ADVERSARY", "ARCHIVIST"})
WRITER_ROLES = frozenset({"PLANNER", "IMPLEMENTER", "ARCHIVIST"})
MAX_RESULT_BYTES = 128 * 1024
MAX_SUMMARY_BYTES = 12 * 1024


class LifecycleError(ControlPlaneError):
    """A lifecycle packet, grant, or host invariant is invalid."""


class AllocationConflict(LifecycleError):
    """A task already has a pending dispatch or is not eligible."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _bounded_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise LifecycleError(f"{field} is invalid or exceeds its bound")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise LifecycleError(f"{field} contains control characters")
    if SECRET_MARKER_RE.search(value):
        raise LifecycleError(f"{field} contains a credential marker")
    return value


def lifecycle_root(profile: Profile, identifier: str, dispatch_id: str) -> pathlib.Path:
    if not TASK_IDENTIFIER_RE.fullmatch(identifier) or not UUID_RE.fullmatch(dispatch_id):
        raise LifecycleError("dispatch namespace identity is invalid")
    state_root = require_physical_namespace(profile.state_root)
    if state_root.is_symlink() or (state_root.exists() and not state_root.is_dir()):
        raise LifecycleError("lifecycle state root is unsafe")
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_root.chmod(0o700)
    root = state_root / "lifecycle" / identifier / dispatch_id
    for directory in (state_root / "lifecycle", state_root / "lifecycle" / identifier, root):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise LifecycleError("dispatch namespace contains an unsafe path component")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    for name in ("inbox", "outbox", "host"):
        directory = root / name
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise LifecycleError("dispatch namespace contains an unsafe directory")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    return root


def _read_json(path: pathlib.Path, label: str) -> object:
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


def _write_json(path: pathlib.Path, value: object) -> None:
    atomic_metadata_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _git(workspace: pathlib.Path, *args: str) -> str:
    result = run_git(workspace, *args)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        raise LifecycleError(f"Git verification failed: {detail[:240]}")
    return result.stdout.strip()


def _working_tree_paths(workspace: pathlib.Path) -> list[str]:
    """Return the exact host-observed changed paths, including untracked files."""
    result = run_git(workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        raise LifecycleError(f"Git status verification failed: {detail[:240]}")
    records = result.stdout.split("\x00")
    paths: list[str] = []
    index = 0
    while index < len(records) - 1:
        record = records[index]
        if len(record) < 4:
            raise LifecycleError("Git status output is malformed")
        paths.append(record[3:])
        # With -z, rename/copy records carry source and destination as two
        # consecutive NUL-delimited pathnames. Both are authority-relevant.
        if record[0] in {"R", "C"} or record[1] in {"R", "C"}:
            if index + 1 >= len(records) - 1 or not records[index + 1]:
                raise LifecycleError("Git rename/copy status is incomplete")
            paths.append(records[index + 1])
            index += 2
        else:
            index += 1
    return sorted(set(paths))


def broker_writer_delta(
    workspace: pathlib.Path,
    role: str,
    changed_paths: object,
    write_scopes: object,
    *,
    expected_starting_head: str,
) -> dict[str, object]:
    """Stage and commit only the exact delta admitted by the Pilot grant.

    This is host-side mechanics. It does not accept a model-supplied commit,
    write Git metadata through the role sandbox, or expand a capability grant.
    """
    if role not in WRITER_ROLES or not isinstance(changed_paths, list) or not isinstance(write_scopes, list):
        raise LifecycleError("writer brokerage requires a canonical writer and exact lists")
    observed = _working_tree_paths(workspace)
    supplied = sorted(set(str(path) for path in changed_paths))
    if supplied != observed:
        raise LifecycleError("Runtime changed-path report differs from host observation")
    if any(path == ".git" or path.startswith(".git/") for path in observed):
        raise LifecycleError("writer delta includes Git metadata")
    unauthorized = [path for path in observed if not any(path == scope or path.startswith(str(scope).rstrip("/") + "/") for scope in write_scopes)]
    if unauthorized:
        raise LifecycleError("writer delta is outside the Pilot-issued capability grant")
    before = _git(workspace, "rev-parse", "HEAD")
    if before != expected_starting_head:
        raise LifecycleError("writer workspace HEAD differs from the Pilot-issued starting HEAD")
    if observed:
        result = run_git(workspace, "add", "--all", "--", *observed)
        if result.returncode:
            raise LifecycleError("host Git broker could not stage the exact writer delta")
        result = run_git(
            workspace,
            "-c", "user.name=Symphony Agent",
            "-c", "user.email=symphony@localhost",
            "commit", "-m", f"Symphony: {role.lower()} authorized delta",
        )
        if result.returncode:
            raise LifecycleError("host Git broker could not create the writer commit")
    after = _git(workspace, "rev-parse", "HEAD")
    if observed and after == before:
        raise LifecycleError("host writer brokerage did not create a new commit")
    if observed:
        identity = _git(workspace, "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce", after).split("\x00")
        if identity != ["Symphony Agent", "symphony@localhost", "Symphony Agent", "symphony@localhost"]:
            raise LifecycleError("host writer commit identity is not the deterministic Symphony Agent")
    if _working_tree_paths(workspace):
        raise LifecycleError("workspace remains dirty after host writer brokerage")
    return {"changed_paths": observed, "workspace_head": after, "commit_sha": after if observed else None, "previous_head": before, "dirty": False}


def verify_git_truth(profile: Profile, workspace: pathlib.Path, task: dict[str, object]) -> str:
    """Verify repository identity and return workspace HEAD; dirty is observed, not hidden."""
    if _git(workspace, "remote", "get-url", "origin") != profile.git_remote:
        raise LifecycleError("workspace repository identity does not match the registered remote")
    if _git(workspace, "branch", "--show-current") != task["branch"]:
        raise LifecycleError("workspace branch is not the host-owned task branch")
    head = _git(workspace, "rev-parse", "HEAD")
    if not SHA_RE.fullmatch(head):
        raise LifecycleError("workspace HEAD is not a commit SHA")
    if run_git(workspace, "merge-base", "--is-ancestor", str(task["base_sha"]), head).returncode:
        raise LifecycleError("workspace HEAD is not descended from the accepted base")
    return head


def perform_mechanical_validation(
    profile: Profile,
    workspace: pathlib.Path,
    *,
    lifecycle_id: str | None = None,
) -> dict[str, object]:
    """Compute Pilot mechanical acceptance from retained state and host facts.

    This operation does not run project tests or interpret role prose. The
    lower-level database method is intentionally private so callers cannot
    promote an arbitrary ``passed=True`` value into final acceptance.
    """
    facts, _ = local_task_facts(profile, workspace)
    with ControlPlaneDatabase.open(control_database_path(profile)) as database:
        task = database.read_task(facts.task_uuid)
        lifecycle = database.connection.execute(
            "SELECT * FROM lifecycles WHERE id = ?" if lifecycle_id else
            "SELECT * FROM lifecycles WHERE task_id = ? ORDER BY ordinal DESC LIMIT 1",
            (lifecycle_id,) if lifecycle_id else (task["id"],),
        ).fetchone()
        if lifecycle is None or lifecycle["task_id"] != task["id"]:
            raise LifecycleError("mechanical validation lifecycle is not owned by the task")
        lifecycle = dict(lifecycle)
        if lifecycle["convergence_status"] != "RECORDED":
            raise LifecycleError("mechanical validation requires recorded PM convergence")

        workspace_head: str | None = None
        try:
            workspace_head = _git(workspace, "rev-parse", "HEAD")
        except LifecycleError:
            pass
        try:
            changed_paths = _working_tree_paths(workspace)
            clean_worktree = not changed_paths
        except LifecycleError:
            changed_paths = []
            clean_worktree = False
        try:
            repository_head = verify_git_truth(profile, workspace, task)
            repository_authority = repository_head == workspace_head
        except LifecycleError:
            repository_head = None
            repository_authority = False

        active_dispatches = database.connection.execute(
            "SELECT id, status, role FROM role_dispatches "
            "WHERE lifecycle_id = ? AND status IN ('AUTHORIZED', 'RUNNING')",
            (lifecycle["id"],),
        ).fetchall()
        active_runs = database.connection.execute(
            "SELECT id, role, status FROM role_runs WHERE lifecycle_id = ? AND status = 'running'",
            (lifecycle["id"],),
        ).fetchall()
        runs = [dict(row) for row in database.connection.execute(
            "SELECT * FROM role_runs WHERE lifecycle_id = ? ORDER BY started_at, id",
            (lifecycle["id"],),
        ).fetchall()]
        required_roles = {"PROJECT-MANAGER", "PLANNER", "REVIEWER", "IMPLEMENTER", "ADVERSARY"}
        retained_roles: set[str] = set()
        bound_execution = True
        no_failed_execution = True
        accepted_writer_deltas = True
        run_facts: list[dict[str, object]] = []
        for run in runs:
            evidence = database.connection.execute(
                "SELECT * FROM execution_evidence WHERE id = ? AND task_id = ?",
                (run["execution_evidence_id"], task["id"]),
            ).fetchone() if run.get("execution_evidence_id") else None
            dispatch = database.connection.execute(
                "SELECT * FROM role_dispatches WHERE id = ? AND task_id = ?",
                (run["dispatch_id"], task["id"]),
            ).fetchone() if run.get("dispatch_id") else None
            bound = bool(
                evidence and dispatch and evidence["role_run_id"] == run["id"] and
                evidence["dispatch_id"] == run["dispatch_id"] and
                evidence["observed_role"] == run["role"] and
                dispatch["role"] == run["role"] and evidence["status"] == "finished"
            )
            bound_execution = bound_execution and bound
            no_failed_execution = no_failed_execution and run["status"] == "finished" and bound
            retained_roles.add(str(run["role"])) if bound else None
            writer_delta = None
            if run["role"] in WRITER_ROLES:
                writer_delta = database.connection.execute(
                    "SELECT * FROM writer_deltas WHERE role_run_id = ? "
                    "AND authorization_status = 'ACCEPTED' ORDER BY observed_at DESC, id DESC LIMIT 1",
                    (run["id"],),
                ).fetchone()
                accepted_writer_deltas = accepted_writer_deltas and writer_delta is not None
            run_facts.append({
                "role_run_id": run["id"], "role": run["role"],
                "status": run["status"], "evidence_id": evidence["id"] if evidence else None,
                "dispatch_id": dispatch["id"] if dispatch else None,
                "writer_delta_id": writer_delta["id"] if writer_delta else None,
                "bound": bound,
            })
        open_blockers = database.connection.execute(
            "SELECT id FROM blockers WHERE task_id = ? AND status = 'open'", (task["id"],)
        ).fetchall()
        latest_round = database.connection.execute(
            "SELECT * FROM working_rounds WHERE lifecycle_id = ? ORDER BY ordinal DESC LIMIT 1",
            (lifecycle["id"],),
        ).fetchone()
        checks = {
            "lifecycle_convergence_recorded": lifecycle["convergence_status"] == "RECORDED",
            "exact_convergence_head": bool(
                workspace_head and lifecycle["convergence_head"] == task["current_head"] == workspace_head
            ),
            "repository_authority": repository_authority,
            "clean_worktree": clean_worktree,
            "no_active_dispatch_or_execution": not active_dispatches and not active_runs,
            "required_lifecycle_executions_retained": required_roles <= retained_roles,
            "bound_execution_evidence": bound_execution,
            "writer_deltas_accepted": accepted_writer_deltas,
            "no_failed_execution": no_failed_execution,
            "no_open_mechanical_blocker": not open_blockers,
            "converged_working_round": bool(latest_round and latest_round["state"] == "CONVERGED"),
        }
        evidence = {
            "operation": "pilot-mechanical-validation/v1",
            "lifecycle_id": lifecycle["id"], "task_id": task["id"],
            "checks": checks,
            "observed": {
                "workspace_head": workspace_head,
                "repository_head": repository_head,
                "changed_paths": changed_paths,
                "active_dispatches": [dict(row) for row in active_dispatches],
                "active_executions": [dict(row) for row in active_runs],
                "runs": run_facts,
                "open_blocker_ids": [row["id"] for row in open_blockers],
            },
        }
        passed = all(checks.values())
        stored = database._record_mechanical_validation(
            lifecycle["id"], head_sha=str(lifecycle["convergence_head"]),
            passed=passed, evidence=evidence,
        )
        validation = {"lifecycle": stored, "passed": passed, "evidence": evidence}
    if passed:
        validation["next_dispatch"] = issue_next_dispatch(profile, str(task["id"]))
    return validation


def _latest_run(database: ControlPlaneDatabase, task_id: str, role: str | None = None) -> dict[str, object] | None:
    query = "SELECT * FROM role_runs WHERE task_id = ?"
    params: list[object] = [task_id]
    if role:
        query += " AND role = ?"
        params.append(role)
    query += " ORDER BY started_at DESC, id DESC LIMIT 1"
    row = database.connection.execute(query, params).fetchone()
    return dict(row) if row else None


def _run_packet(database: ControlPlaneDatabase, run: dict[str, object] | None) -> dict[str, object]:
    if not run or not run.get("execution_evidence_id"):
        return {}
    row = database.connection.execute(
        "SELECT receipt_json FROM execution_evidence WHERE id = ?",
        (run["execution_evidence_id"],),
    ).fetchone()
    if not row:
        return {}
    try:
        evidence = json.loads(str(row[0]))
        return evidence.get("result", {}) if isinstance(evidence, dict) and isinstance(evidence.get("result"), dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _licensed_run(database: ControlPlaneDatabase, run: object) -> dict[str, object] | None:
    if not isinstance(run, dict):
        return None
    if run.get("status") != "finished" or not run.get("execution_evidence_id"):
        raise AllocationConflict("a non-finished execution cannot license the next lifecycle transition")
    if run.get("role") in WRITER_ROLES:
        delta = database.connection.execute(
            "SELECT authorization_status FROM writer_deltas WHERE role_run_id = ? ORDER BY observed_at DESC, id DESC LIMIT 1",
            (run["id"],),
        ).fetchone()
        if delta is None or delta["authorization_status"] != "ACCEPTED":
            raise AllocationConflict("a writer execution without an accepted host delta cannot license the next transition")
    return run


def _current_context(database: ControlPlaneDatabase, task_id: str) -> tuple[dict[str, object] | None, dict[str, object] | None, dict[str, object] | None]:
    lifecycle = database.connection.execute("SELECT * FROM lifecycles WHERE task_id = ? ORDER BY ordinal DESC LIMIT 1", (task_id,)).fetchone()
    if not lifecycle:
        return None, None, None
    working = database.connection.execute("SELECT * FROM working_rounds WHERE lifecycle_id = ? ORDER BY ordinal DESC LIMIT 1", (lifecycle["id"],)).fetchone()
    attempt = database.connection.execute("SELECT * FROM planning_attempts WHERE working_round_id = ? ORDER BY ordinal DESC LIMIT 1", (working["id"],)).fetchone() if working else None
    return dict(lifecycle), dict(working) if working else None, dict(attempt) if attempt else None


def _terminalize_non_converged_lifecycle(
    database: ControlPlaneDatabase,
    task: dict[str, object],
    lifecycle: dict[str, object],
    actual_head: str,
) -> None:
    ended_at = _now()
    database.connection.execute(
        "UPDATE lifecycles SET state = 'NON_CONVERGED', terminal_outcome = 'NON_CONVERGED', ended_at = ? "
        "WHERE id = ? AND state = 'RUNNING'",
        (ended_at, lifecycle["id"]),
    )
    database.connection.execute(
        "UPDATE tasks SET state = 'TERMINATED', current_head = ?, updated_at = ? WHERE id = ?",
        (actual_head, ended_at, task["id"]),
    )
    database._insert_event(
        str(task["id"]), "lifecycle_terminated",
        {"lifecycle_id": lifecycle["id"], "terminal_outcome": "NON_CONVERGED", "head_sha": actual_head},
        occurred_at=ended_at,
    )


def _next_role(database: ControlPlaneDatabase, task: dict[str, object]) -> tuple[str | None, dict[str, object] | None, dict[str, object] | None, dict[str, object] | None]:
    lifecycle, working, attempt = _current_context(database, str(task["id"]))
    if lifecycle is None:
        return "PROJECT-MANAGER", None, None, None
    if lifecycle["state"] == "NON_CONVERGED":
        archivist = database.connection.execute(
            "SELECT 1 FROM role_runs WHERE lifecycle_id = ? AND role = 'ARCHIVIST' LIMIT 1",
            (lifecycle["id"],),
        ).fetchone()
        if archivist is None:
            return "ARCHIVIST", lifecycle, working, attempt
        return None, lifecycle, working, attempt
    if lifecycle["state"] != "RUNNING":
        return None, lifecycle, working, attempt
    if working is None:
        pm = database.connection.execute("SELECT * FROM role_runs WHERE task_id = ? AND role = 'PROJECT-MANAGER' ORDER BY started_at DESC, id DESC LIMIT 1", (task["id"],)).fetchone()
        _licensed_run(database, dict(pm) if pm else None)
        return "PLANNER", lifecycle, None, None
    if working["state"] == "PLANNING":
        if attempt is None:
            return "PLANNER", lifecycle, working, None
        planner = database.connection.execute("SELECT * FROM role_runs WHERE planning_attempt_id = ? AND role = 'PLANNER' ORDER BY started_at DESC LIMIT 1", (attempt["id"],)).fetchone()
        if planner is None:
            return "PLANNER", lifecycle, working, attempt
        _licensed_run(database, dict(planner))
        reviewer = database.connection.execute("SELECT * FROM role_runs WHERE planning_attempt_id = ? AND role = 'REVIEWER' ORDER BY started_at DESC LIMIT 1", (attempt["id"],)).fetchone()
        if reviewer is None:
            return "REVIEWER", lifecycle, working, attempt
        _licensed_run(database, dict(reviewer))
        packet = _run_packet(database, dict(reviewer))
        if packet.get("verdict") == "correction_required":
            if int(attempt["ordinal"]) >= 3:
                if working["state"] != "NON_CONVERGED":
                    raise AllocationConflict("planning-attempt exhaustion must be reconciled before the next dispatch")
                if int(working["ordinal"]) >= 8:
                    return "ARCHIVIST", lifecycle, working, attempt
                return "PROJECT-MANAGER", lifecycle, working, attempt
            return "PLANNER", lifecycle, working, None
        if packet.get("verdict") not in {"accepted", "pass", "approved"}:
            raise AllocationConflict("Reviewer evidence does not license implementation")
        return "IMPLEMENTER", lifecycle, working, attempt
    if working["state"] == "IMPLEMENTING":
        implementer = database.connection.execute("SELECT * FROM role_runs WHERE working_round_id = ? AND role = 'IMPLEMENTER' ORDER BY started_at DESC LIMIT 1", (working["id"],)).fetchone()
        if implementer is None:
            return "IMPLEMENTER", lifecycle, working, attempt
        _licensed_run(database, dict(implementer))
        return "ADVERSARY", lifecycle, working, attempt
    if working["state"] == "ADVERSARIAL":
        return "PROJECT-MANAGER", lifecycle, working, attempt
    if working["state"] == "CONVERGED":
        if lifecycle["mechanical_acceptance"] != "ACCEPTED":
            raise AllocationConflict("PM convergence is recorded; Pilot mechanical validation is still required")
        return "ARCHIVIST", lifecycle, working, attempt
    if working["state"] == "NON_CONVERGED":
        if int(working["ordinal"]) >= 8:
            raise AllocationConflict("round-8 non-convergence must be terminalized before Archivist dispatch")
        pm = database.connection.execute(
            "SELECT * FROM role_runs WHERE lifecycle_id = ? AND working_round_id = ? "
            "AND role = 'PROJECT-MANAGER' ORDER BY started_at DESC, id DESC LIMIT 1",
            (lifecycle["id"], working["id"]),
        ).fetchone()
        if pm is None:
            return "PROJECT-MANAGER", lifecycle, working, attempt
        _licensed_run(database, dict(pm))
        if _run_packet(database, dict(pm)).get("outcome") == "next_working_round":
            return "PLANNER", lifecycle, None, None
        return "PROJECT-MANAGER", lifecycle, working, attempt
    return None, lifecycle, working, attempt


def _scopes_within_roots(paths: object, roots: tuple[str, ...], label: str) -> list[str]:
    proposed = _validate_relative_paths(paths)
    if any(
        not any(path == root or path.startswith(root + "/") for root in roots)
        for path in proposed
    ):
        raise LifecycleError(f"{label} exceeds registered implementation roots")
    return proposed


def _writer_scopes(profile: Profile, role: str, packet: dict[str, object] | None) -> list[str]:
    if role == "PLANNER":
        return list(profile.harness_artifacts.planner)
    if role == "ARCHIVIST":
        return list(profile.harness_artifacts.archivist)
    if role == "IMPLEMENTER":
        paths = packet.get("proposed_implementation_paths") if isinstance(packet, dict) else None
        return _scopes_within_roots(
            paths, profile.harness_artifacts.implementation_roots,
            "Planner implementation proposal",
        )
    return []


def _validate_relative_paths(paths: object) -> list[str]:
    if not isinstance(paths, list) or not paths or len(paths) > 32:
        raise LifecycleError("write scope must be a non-empty bounded list")
    result: list[str] = []
    for value in paths:
        if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
            raise LifecycleError("write scope contains an unsafe path")
        parts = pathlib.PurePosixPath(value).parts
        if not parts or any(part in {"", ".", ".."} for part in parts) or value == ".git" or value.startswith(".git/"):
            raise LifecycleError("write scope contains Git metadata or traversal")
        if SECRET_MARKER_RE.search(value):
            raise LifecycleError("write scope contains secret-shaped text")
        result.append(value.rstrip("/"))
    if len(set(result)) != len(result):
        raise LifecycleError("write scope contains duplicates")
    return result


def issue_next_dispatch(profile: Profile, task_id: str | None = None) -> dict[str, object]:
    """Issue the next dispatch from Pilot state, without a workspace precondition."""
    with ControlPlaneDatabase.open(control_database_path(profile)) as database:
        task = database.read_task(task_id) if task_id else None
        if task is None:
            row = database.connection.execute(
                "SELECT * FROM tasks WHERE project_slug = ? AND state IN ('QUEUED', 'ACTIVE', 'TERMINATED') "
                "ORDER BY updated_at, id LIMIT 1", (profile.slug,),
            ).fetchone()
            if row is None:
                raise AllocationConflict("project has no queued task eligible for dispatch")
            task = dict(row)
        if task["project_slug"] != profile.slug:
            raise LifecycleError("task is not registered to the selected project")
        if database.connection.execute(
            "SELECT 1 FROM role_dispatches WHERE task_id = ? AND status IN ('AUTHORIZED', 'RUNNING') LIMIT 1",
            (task["id"],),
        ).fetchone():
            raise AllocationConflict("an authorized dispatch is awaiting Runtime evidence")
        role, lifecycle, working, attempt = _next_role(database, task)
        if role is None and lifecycle and lifecycle["state"] == "NON_CONVERGED":
            disposition = database.connection.execute(
                "SELECT decision FROM human_dispositions WHERE lifecycle_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (lifecycle["id"],),
            ).fetchone()
            if disposition and disposition["decision"] == "START_ANOTHER_LIFECYCLE":
                lifecycle = database.create_lifecycle(str(task["id"]))
                working = None
                attempt = None
                role = "PROJECT-MANAGER"
        if role is None:
            raise AllocationConflict("task has no eligible dispatch")
        if lifecycle is None:
            lifecycle = database.create_lifecycle(str(task["id"]))
        elif working is None:
            prior = database.connection.execute("SELECT * FROM working_rounds WHERE lifecycle_id = ? ORDER BY ordinal DESC LIMIT 1", (lifecycle["id"],)).fetchone()
            if prior is not None and prior["state"] == "PLANNING":
                database.terminate_working_round_non_converged(str(prior["id"]))
            working = database.start_working_round(str(lifecycle["id"]))
            attempt = database.start_planning_attempt(str(working["id"]))
        elif role == "PLANNER" and attempt is None:
            attempt = database.start_planning_attempt(str(working["id"]))
        previous = _latest_run(database, str(task["id"]), "PLANNER")
        previous_packet = _run_packet(database, previous)
        writer_scopes = _writer_scopes(profile, role, previous_packet)
        registered_scopes = (
            list(profile.harness_artifacts.planner) if role == "PLANNER" else
            list(profile.harness_artifacts.archivist) if role == "ARCHIVIST" else []
        )
        grant = database.authorize_dispatch(
            str(task["id"]), lifecycle_id=str(lifecycle["id"]),
            working_round_id=str(working["id"]) if working else None,
            planning_attempt_id=str(attempt["id"]) if attempt else None,
            role=role, expected_starting_head=str(task["current_head"] or task["base_sha"]),
            read_scopes=["project", "registered_harness_artifacts"],
            write_scopes=writer_scopes,
            registered_artifact_scopes=registered_scopes,
            protected_artifact_scopes=(
                list(profile.harness_artifacts.planner) +
                list(profile.harness_artifacts.archivist)
            ),
            implementation_roots=list(profile.harness_artifacts.implementation_roots),
        )
        dispatch = grant["dispatch"]
        namespace = lifecycle_root(profile, str(task["identifier"]), str(dispatch["id"]))
        packet = {
            "schema": "symphony-pilot-dispatch/v1",
            "task_id": task["id"], "identifier": task["identifier"],
            "lifecycle_id": lifecycle["id"],
            "working_round_id": working["id"] if working else None,
            "planning_attempt_id": attempt["id"] if attempt else None,
            "dispatch_id": dispatch["id"], "role": role,
            "expected_starting_head": dispatch["expected_starting_head"],
            "handoff_inputs": _handoff_inputs(database, task, lifecycle, working, role),
            "capability_grant": grant["grant"],
        }
        _write_json(namespace / "inbox" / "dispatch.json", packet)
        marker = {**packet, "namespace": str(namespace), "result_path": str(namespace / "outbox" / "result.json"), "execution_path": str(namespace / "host" / "execution.json")}
        _write_json(namespace / "host" / "dispatch.json", marker)
        return marker


def _handoff_inputs(database: ControlPlaneDatabase, task: dict[str, object], lifecycle: dict[str, object], working: dict[str, object] | None, role: str) -> list[dict[str, object]]:
    """Select bounded accepted evidence; Runtime only transports this list."""
    sources: list[tuple[str, dict[str, object] | None]] = []
    if role == "PLANNER":
        prior = _latest_run(database, str(task["id"]), "PROJECT-MANAGER") if working is None else _latest_run(database, str(task["id"]), "REVIEWER")
        sources.append(("PM_HANDOFF" if working is None else "REVIEWER_CORRECTION", prior))
    elif role == "REVIEWER":
        sources.append(("PLANNER_RESULT", _latest_run(database, str(task["id"]), "PLANNER")))
    elif role == "IMPLEMENTER":
        sources.extend((("PLANNER_RESULT", _latest_run(database, str(task["id"]), "PLANNER")),
                        ("REVIEWER_RESULT", _latest_run(database, str(task["id"]), "REVIEWER"))))
    elif role == "ADVERSARY":
        sources.append(("IMPLEMENTER_RESULT", _latest_run(database, str(task["id"]), "IMPLEMENTER")))
    elif role == "PROJECT-MANAGER":
        if working and working.get("state") == "NON_CONVERGED":
            sources.append(("ROUND_EVIDENCE", _latest_run(database, str(task["id"]), "REVIEWER")))
        else:
            sources.append(("ADVERSARY_RESULT", _latest_run(database, str(task["id"]), "ADVERSARY")))
    elif role == "ARCHIVIST":
        sources.append(("TERMINAL_FACTS", None))
    result: list[dict[str, object]] = []
    for kind, run in sources:
        if kind == "TERMINAL_FACTS":
            result.append({"kind": kind, "source_role": "PILOT", "source_dispatch_id": None,
                           "input": {"lifecycle_id": lifecycle["id"], "terminal_outcome": lifecycle.get("terminal_outcome"),
                                     "task_id": task["id"], "current_head": task.get("current_head")}})
            continue
        if not run or run.get("status") != "finished" or not run.get("execution_evidence_id"):
            continue
        packet = _run_packet(database, run)
        if packet:
            result.append({"kind": kind, "source_role": run["role"], "source_dispatch_id": run["dispatch_id"], "input": packet})
    return result
def read_result(path: pathlib.Path) -> dict[str, object]:
    value = _read_json(path, "execution result")
    if not isinstance(value, dict) or set(value) - {"schema", "task_id", "dispatch_id", "role", "summary", "verdict", "proposed_implementation_paths", "findings", "outcome", "blocker_kind"}:
        raise LifecycleError("execution result fields are invalid")
    if value.get("schema") != RESULT_SCHEMA:
        raise LifecycleError("execution result schema is invalid")
    for field in ("task_id", "dispatch_id", "role"):
        if not isinstance(value.get(field), str) or not UUID_RE.fullmatch(str(value[field])):
            raise LifecycleError(f"execution result {field} is invalid")
    if value["role"] not in ROLES:
        raise LifecycleError("execution result role is invalid")
    if value.get("summary") is not None:
        _bounded_text(value["summary"], "summary", MAX_SUMMARY_BYTES)
    if "proposed_implementation_paths" in value:
        _validate_relative_paths(value["proposed_implementation_paths"])
    return value


def _read_execution(path: pathlib.Path) -> dict[str, object]:
    value = _read_json(path, "Runtime execution evidence")
    if not isinstance(value, dict):
        raise LifecycleError("Runtime execution evidence must be an object")
    required = {
        "phase", "runtime_execution_id", "task_id", "dispatch_id", "observed_role", "status",
        "started_at", "starting_head", "head_sha", "changed_paths", "dirty",
        "runtime_process_id", "app_server_thread_id",
    }
    if not required.issubset(value):
        raise LifecycleError("Runtime execution evidence is incomplete")
    if value["phase"] not in {"started", "terminated"}:
        raise LifecycleError("Runtime execution evidence phase is invalid")
    if value["phase"] == "started" and value["status"] != "running":
        raise LifecycleError("started Runtime evidence must have running status")
    if value["phase"] == "terminated" and value["status"] not in {"finished", "failed", "blocked", "cancelled"}:
        raise LifecycleError("Runtime execution status is invalid")
    if value["phase"] == "started" and value.get("finished_at") is not None:
        raise LifecycleError("started Runtime evidence cannot have terminal time")
    if value["phase"] == "terminated":
        if "finished_at" not in value:
            raise LifecycleError("terminated Runtime evidence requires terminal time")
        _timestamp(str(value["finished_at"]), "finished_at")
    _bounded_text(value["runtime_execution_id"], "runtime_execution_id", 256)
    _bounded_text(str(value["runtime_process_id"]), "runtime_process_id", 256)
    _bounded_text(value["app_server_thread_id"], "app_server_thread_id", 256)
    if value["observed_role"] not in ROLES:
        raise LifecycleError("Runtime execution role is invalid")
    _timestamp(str(value["started_at"]), "started_at")
    if value["phase"] == "terminated":
        _timestamp(str(value["finished_at"]), "finished_at")
    if not SHA_RE.fullmatch(str(value["starting_head"])) or not SHA_RE.fullmatch(str(value["head_sha"])):
        raise LifecycleError("Runtime execution HEAD evidence is invalid")
    if not isinstance(value["changed_paths"], list) or len(value["changed_paths"]) > 4096:
        raise LifecycleError("Runtime changed-path evidence is invalid")
    for changed_path in value["changed_paths"]:
        if (not isinstance(changed_path, str) or not changed_path or "\\" in changed_path or
                changed_path.startswith("/") or "\x00" in changed_path or
                any(part in {"", ".", ".."} for part in pathlib.PurePosixPath(changed_path).parts)):
            raise LifecycleError("Runtime changed-path evidence contains an unsafe path")
    if value["changed_paths"] != sorted(set(value["changed_paths"])):
        raise LifecycleError("Runtime changed-path evidence must be sorted and unique")
    if not isinstance(value["dirty"], bool):
        raise LifecycleError("Runtime dirty evidence is invalid")
    if value.get("commit_sha") is not None and not SHA_RE.fullmatch(str(value["commit_sha"])):
        raise LifecycleError("Runtime commit evidence is invalid")
    return value


def _record_lifecycle_outcome(
    database: ControlPlaneDatabase,
    task: dict[str, object],
    lifecycle: dict[str, object],
    working: dict[str, object],
    role: str,
    result: dict[str, object],
    actual_head: str,
) -> None:
    outcome = result.get("outcome")
    if role == "REVIEWER" and outcome == "correction_required":
        attempt = database.connection.execute("SELECT id FROM planning_attempts WHERE working_round_id = ? ORDER BY ordinal DESC LIMIT 1", (working["id"],)).fetchone()
        if attempt:
            attempt_row = database.connection.execute("SELECT ordinal FROM planning_attempts WHERE id = ?", (attempt[0],)).fetchone()
            database.connection.execute("UPDATE planning_attempts SET state = 'CORRECTION_REQUIRED', ended_at = ? WHERE id = ? AND state = 'OPEN'", (_now(), attempt[0]))
            if attempt_row and int(attempt_row["ordinal"]) >= 3:
                database.terminate_working_round_non_converged(str(working["id"]))
                if int(working["ordinal"]) >= 8:
                    _terminalize_non_converged_lifecycle(database, task, lifecycle, actual_head)
        database._insert_event(str(task["id"]), "planning_correction_required", {"working_round_id": working["id"]})
    elif role == "REVIEWER" and outcome in {"plan_accepted", "accepted"}:
        attempt = database.connection.execute("SELECT id FROM planning_attempts WHERE working_round_id = ? ORDER BY ordinal DESC LIMIT 1", (working["id"],)).fetchone()
        if attempt:
            database.connection.execute("UPDATE planning_attempts SET state = 'ACCEPTED', ended_at = ? WHERE id = ?", (_now(), attempt[0]))
        database.connection.execute("UPDATE working_rounds SET state = 'IMPLEMENTING' WHERE id = ?", (working["id"],))
        database._insert_event(str(task["id"]), "planning_accepted", {"working_round_id": working["id"], "head_sha": actual_head})
    elif role == "ADVERSARY" and outcome in {"adversary_pass", "adversary_passed"}:
        database.connection.execute("UPDATE working_rounds SET state = 'ADVERSARIAL' WHERE id = ?", (working["id"],))
    elif role == "PROJECT-MANAGER" and outcome == "converged":
        database.connection.execute("UPDATE working_rounds SET state = 'CONVERGED', ended_at = ? WHERE id = ?", (_now(), working["id"]))
        database.connection.execute("UPDATE lifecycles SET convergence_status = 'RECORDED', convergence_head = ?, convergence_at = ? WHERE id = ? AND state = 'RUNNING'", (actual_head, _now(), lifecycle["id"]))
        database._insert_event(str(task["id"]), "working_round_converged", {"working_round_id": working["id"], "head_sha": actual_head})
        database._insert_event(str(task["id"]), "lifecycle_converged", {"lifecycle_id": lifecycle["id"], "head_sha": actual_head})
    elif role == "ARCHIVIST" and outcome in {"archive_complete", "archived"}:
        non_converged = database.connection.execute("SELECT 1 FROM working_rounds WHERE lifecycle_id = ? AND state = 'NON_CONVERGED' AND ordinal = 8", (lifecycle["id"],)).fetchone()
        if non_converged and lifecycle["state"] == "NON_CONVERGED":
            # Round-8 terminal classification was already established before
            # this Archivist dispatch. Archivist documents it; it does not
            # manufacture or rewrite lifecycle meaning.
            database.connection.execute(
                "UPDATE tasks SET current_head = ?, updated_at = ? WHERE id = ?",
                (actual_head, _now(), task["id"]),
            )
        else:
            terminal_state = "NON_CONVERGED" if non_converged else "ACCEPTED"
            terminal_outcome = "NON_CONVERGED" if non_converged else "SUCCESSFUL"
            database.connection.execute("UPDATE lifecycles SET state = ?, terminal_outcome = ?, ended_at = ? WHERE id = ? AND state = 'RUNNING'", (terminal_state, terminal_outcome, _now(), lifecycle["id"]))
            database.connection.execute("UPDATE tasks SET state = 'TERMINATED', current_head = ?, updated_at = ? WHERE id = ?", (actual_head, _now(), task["id"]))
            database._insert_event(str(task["id"]), "lifecycle_terminated", {"lifecycle_id": lifecycle["id"], "terminal_outcome": terminal_outcome, "head_sha": actual_head})
    elif role == "PROJECT-MANAGER" and outcome in {"no_convergence", "non_converged"}:
        database.connection.execute("UPDATE working_rounds SET state = 'NON_CONVERGED', ended_at = ? WHERE id = ?", (_now(), working["id"]))
        database._insert_event(str(task["id"]), "lifecycle_non_converged", {"lifecycle_id": lifecycle["id"], "working_round_id": working["id"]})
        if int(working["ordinal"]) >= 8:
            _terminalize_non_converged_lifecycle(database, task, lifecycle, actual_head)
    elif role == "PROJECT-MANAGER" and outcome == "next_working_round":
        current_lifecycle = database.connection.execute(
            "SELECT state FROM lifecycles WHERE id = ?", (lifecycle["id"],)
        ).fetchone()
        current_working = database.connection.execute(
            "SELECT state, ordinal FROM working_rounds WHERE id = ?", (working["id"],)
        ).fetchone()
        if (current_lifecycle is None or current_lifecycle["state"] != "RUNNING" or
                current_working is None or current_working["state"] != "NON_CONVERGED" or
                int(current_working["ordinal"]) >= 8):
            raise StateConflict("next working round handoff is not eligible")
        database._insert_event(
            str(task["id"]), "working_round_handoff_requested",
            {"lifecycle_id": lifecycle["id"], "working_round_id": working["id"], "next_ordinal": int(current_working["ordinal"]) + 1},
        )


def reconcile(profile: Profile, workspace: pathlib.Path, task_id: str | None = None) -> dict[str, object]:
    """Reconcile one Runtime receipt and issue the next Pilot dispatch when eligible."""
    projection = _reconcile_once(profile, workspace, task_id=task_id)
    task = projection.get("task") if isinstance(projection, dict) else None
    if isinstance(task, dict) and task.get("id"):
        try:
            issue_next_dispatch(profile, str(task["id"]))
        except AllocationConflict:
            # No next dispatch is correct after a failed execution, while the
            # current lifecycle transition remains durably reconciled.
            pass
    return projection


def _reconcile_once(profile: Profile, workspace: pathlib.Path, *, task_id: str | None = None) -> dict[str, object]:
    """Bind retained Runtime evidence, then apply one accepted Pilot transition."""
    with ControlPlaneDatabase.open(control_database_path(profile)) as database:
        if task_id is not None:
            task = database.read_task(task_id)
            expected_workspace = pathlib.Path(profile.workspace_root) / str(task["identifier"])
            if workspace.resolve() != expected_workspace.resolve():
                raise LifecycleError("Runtime workspace does not match the Pilot task identity")
        else:
            facts, _ = local_task_facts(profile, workspace)
            task = database.read_task(facts.task_uuid)
        row = database.connection.execute("SELECT * FROM role_dispatches WHERE task_id = ? AND status IN ('AUTHORIZED', 'RUNNING') ORDER BY created_at DESC, id DESC LIMIT 1", (task["id"],)).fetchone()
        if row is None:
            raise LifecycleError("no authorized Pilot dispatch is awaiting evidence")
        dispatch = dict(row)
        namespace = lifecycle_root(profile, str(task["identifier"]), str(dispatch["id"]))
        execution = _read_execution(namespace / "host" / "execution.json")
        if execution.get("task_id") != task["id"] or execution.get("dispatch_id") != dispatch["id"] or execution.get("observed_role") != dispatch["role"]:
            raise LifecycleError("Runtime evidence is not bound to the Pilot dispatch")
        if execution["starting_head"] != dispatch["expected_starting_head"]:
            raise LifecycleError("Runtime execution started from a different HEAD than the Pilot grant")
        if execution["phase"] == "started":
            with_start = dict(execution)
            with_start["dispatch_id"] = dispatch["id"]
            database.record_execution_started(str(dispatch["id"]), with_start)
            return database.read_projection(str(task["id"]))
        result_path = namespace / "outbox" / "result.json"
        result = None
        if execution.get("status") == "finished" or result_path.exists():
            result = read_result(result_path)
            if result.get("task_id") != task["id"] or result.get("dispatch_id") != dispatch["id"] or result.get("role") != dispatch["role"]:
                raise LifecycleError("role-authored result is not bound to the Pilot dispatch")
        evidence = dict(execution)
        evidence["result"] = result
        if evidence.get("status") == "cancelled":
            evidence["status"] = "failed"
        run = database.record_execution_termination(str(dispatch["id"]), evidence)
        lifecycle, working, _ = _current_context(database, str(task["id"]))
        actual_head = str(execution.get("head_sha") or task["current_head"] or task["base_sha"])
        changed_paths = execution["changed_paths"]
        host_changed_paths = _working_tree_paths(workspace)
        if changed_paths != host_changed_paths:
            raise LifecycleError("Runtime changed-path evidence differs from host observation")
        if dispatch["role"] in WRITER_ROLES:
            grant = database.connection.execute("SELECT write_scopes_json FROM capability_grants WHERE id = ?", (run["capability_grant_id"],)).fetchone()
            scopes = json.loads(grant[0]) if grant else []
            if execution.get("commit_sha"):
                raise LifecycleError("Runtime must not create a role commit")
            if execution["status"] != "finished":
                database.record_writer_delta(str(run["id"]), changed_paths=changed_paths, workspace_head=_git(workspace, "rev-parse", "HEAD"), authorization_status="REJECTED", dirty=bool(host_changed_paths))
                if isinstance(result, dict) and result.get("outcome") == "blocked":
                    kind = result.get("blocker_kind", "project")
                    if kind not in {"human", "project", "infrastructure"}:
                        raise LifecycleError("blocked result has an invalid blocker kind")
                    database.record_blocker(task_id=str(task["id"]), kind=str(kind), body=str(result.get("summary") or "role reported a blocker"))
                return database.read_projection(str(task["id"]))
            try:
                brokered = broker_writer_delta(workspace, dispatch["role"], changed_paths, scopes, expected_starting_head=str(dispatch["expected_starting_head"]))
            except LifecycleError as exc:
                database.record_writer_delta(str(run["id"]), changed_paths=changed_paths, workspace_head=_git(workspace, "rev-parse", "HEAD"), authorization_status="REJECTED", dirty=bool(host_changed_paths))
                raise exc
            commit_sha = brokered.get("commit_sha")
            database.record_writer_delta(str(run["id"]), changed_paths=brokered["changed_paths"], workspace_head=str(brokered["workspace_head"]), authorization_status="ACCEPTED", dirty=False, commit_sha=commit_sha)
            if commit_sha:
                database.update_heads(str(task["id"]), current_head=str(commit_sha))
            actual_head = str(brokered["workspace_head"])
        elif host_changed_paths:
            raise LifecycleError("non-writing role produced a workspace delta")
        if execution["status"] != "finished":
            if isinstance(result, dict) and result.get("outcome") == "blocked":
                kind = result.get("blocker_kind", "project")
                if kind not in {"human", "project", "infrastructure"}:
                    raise LifecycleError("blocked result has an invalid blocker kind")
                database.record_blocker(task_id=str(task["id"]), kind=str(kind), body=str(result.get("summary") or "role reported a blocker"))
            return database.read_projection(str(task["id"]))
        if isinstance(result, dict) and result.get("outcome") == "blocked":
            kind = result.get("blocker_kind", "project")
            if kind not in {"human", "project", "infrastructure"}:
                raise LifecycleError("blocked result has an invalid blocker kind")
            database.record_blocker(task_id=str(task["id"]), kind=str(kind), body=str(result.get("summary") or "role reported a blocker"))
        if lifecycle and working:
            _record_lifecycle_outcome(database, task, lifecycle, working, str(dispatch["role"]), result, actual_head)
        database.record_event(str(task["id"]), "execution_reconciled", {"role_run_id": run["id"], "dispatch_id": dispatch["id"]}, role_run_id=str(run["id"]))
        return database.read_projection(str(task["id"]))


def reconcile_orphaned_executions(profile: Profile, *, managed_runtime_stopped: bool) -> list[dict[str, object]]:
    """Reject incomplete dispatches only after managed Runtime stop proof."""
    if not managed_runtime_stopped:
        raise LifecycleError("orphan reconciliation requires managed Runtime stop evidence")
    with ControlPlaneDatabase.open(control_database_path(profile)) as database:
        rows = database.connection.execute("SELECT * FROM role_dispatches WHERE status IN ('AUTHORIZED', 'RUNNING') AND task_id IN (SELECT id FROM tasks WHERE project_slug = ?)", (profile.slug,)).fetchall()
        result = []
        for row in rows:
            if row["status"] == "RUNNING":
                run = database.record_orphaned_execution(str(row["id"]))
                result.append({"dispatch_id": row["id"], "task_id": row["task_id"], "role": row["role"], "role_run_id": run["id"], "status": "failed"})
                continue
            with database._transaction():
                database.connection.execute("UPDATE role_dispatches SET status = 'REJECTED', consumed_at = ? WHERE id = ?", (_now(), row["id"]))
                result.append({"dispatch_id": row["id"], "task_id": row["task_id"], "role": row["role"], "status": "rejected"})
        return result
