#!/usr/bin/env python3
"""Host-owned SQLite contract for local Symphony task lifecycle state.

This module defines persistence authority only. Project registration remains in
``projects/<slug>/profile.toml``; SQLite stores the task and lifecycle state
that refers to that externally registered slug. The runtime and browser open
the database read-only; trusted host code is its writer.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import functools
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator, Sequence

from prepare_workspace import resolve_host_root


CONTROL_DB_FILENAME = "control.sqlite3"
CONTROL_DB_DIRECTORY = pathlib.Path(".local") / "state" / "symphony-pilot"
CURRENT_SCHEMA_VERSION = 3
BUSY_TIMEOUT_MS = 5_000
MIGRATION_ID = "control-plane-v1"
STORAGE_MIGRATION_ID = "control-plane-v2-storage-reservations"
HARNESS_MIGRATION_ID = "control-plane-v3-harness-lifecycle"

TASK_STATES = frozenset({
    "PREPARED",
    "QUEUED",
    "ACTIVE",
    "TERMINATED",
    "READY_FOR_HUMAN_MERGE",
})
ROLE_NAMES = frozenset({
    "PROJECT-MANAGER",
    "PLANNER",
    "IMPLEMENTER",
    "REVIEWER",
    "ADVERSARY",
    "ARCHIVIST",
})
ROLE_RUN_STATUSES = frozenset({"running", "finished", "failed", "blocked"})
FINDING_SEVERITIES = frozenset({"info", "low", "medium", "high", "critical"})
FINDING_STATUSES = frozenset({"open", "accepted", "rejected", "resolved"})
BLOCKER_KINDS = frozenset({"human", "project", "infrastructure"})
BLOCKER_STATUSES = frozenset({"open", "resolved"})
PUBLICATION_STATUSES = frozenset({"not_started", "started", "published", "failed"})
EVENT_TYPES = frozenset({
    "task_created",
    "queued",
    "lifecycle_started",
    "dispatch_authorized",
    "execution_started",
    "execution_terminated",
    "execution_observed",
    "execution_reconciled",
    "finding_recorded",
    "planning_correction_required",
    "planning_accepted",
    "working_round_started",
    "working_round_converged",
    "working_round_non_converged",
    "working_round_handoff_requested",
    "lifecycle_converged",
    "lifecycle_non_converged",
    "mechanical_validation_passed",
    "mechanical_validation_failed",
    "lifecycle_terminated",
    "human_disposition_recorded",
    "head_changed",
    "writer_delta_validated",
    "host_commit_created",
    "publication_started",
    "publication_finished",
    "blocker_recorded",
    "blocker_resolved",
    "ready_for_human_merge",
})

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
IDENTIFIER_RE = re.compile(r"^T-[0-9]{6}$")
TASK_BRANCH_PREFIX = "codex/"
TASK_ID_PREFIX_LENGTH = 12
PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_UNSET = object()
_OPEN_DATABASE_PATHS: dict[pathlib.Path, int] = {}


class ControlPlaneError(RuntimeError):
    """Base error for invalid or unsafe control-plane operations."""


class SchemaError(ControlPlaneError):
    """The database is absent, partial, corrupt, or not supported."""


class UnsupportedSchemaVersion(SchemaError):
    """The database was written by a newer pilot than this code understands."""


class StateConflict(ControlPlaneError):
    """A caller's expected current state or version no longer matches."""


@dataclasses.dataclass(frozen=True)
class Migration:
    version: int
    identity: str
    statements: tuple[str, ...]


def default_database_path() -> pathlib.Path:
    """Return the one physical host-side control database path.

    ``resolve_host_root`` deliberately refuses native Windows callers because
    the host state authority is a WSL/Linux namespace, not a fabricated path.
    """
    return resolve_host_root() / CONTROL_DB_DIRECTORY / CONTROL_DB_FILENAME


def _uuid(value: str | uuid.UUID | None, field: str) -> str:
    if value is None:
        return str(uuid.uuid4())
    candidate = str(value)
    if not UUID_RE.fullmatch(candidate):
        raise ValueError(f"{field} must be a lowercase canonical UUID")
    return candidate


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


def _scope(value: str, field: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or value.startswith("/") or
            "\x00" in value or any(part in {"", ".", ".."} for part in pathlib.PurePosixPath(value).parts)):
        raise ValueError(f"{field} must be a safe relative POSIX path")
    return value.rstrip("/")


def _project_slug(value: str) -> str:
    if not isinstance(value, str) or not PROJECT_SLUG_RE.fullmatch(value):
        raise ValueError("project_slug is invalid")
    return value


def derive_task_branch(identifier: str, task_id: str | uuid.UUID) -> str:
    """Derive the host-owned branch from the allocated local task identity.

    The identifier makes the branch operator-readable and the UUID prefix
    preserves the collision-resistance of the former host-derived branch
    mechanism. Neither title/objective prose nor an operator-supplied branch
    participates in this identity.
    """
    if not isinstance(identifier, str) or not IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError("identifier must match T-000042")
    canonical_task_id = _uuid(task_id, "task_id")
    task_id_prefix = canonical_task_id.replace("-", "")[:TASK_ID_PREFIX_LENGTH]
    return f"{TASK_BRANCH_PREFIX}{identifier.lower()}-{task_id_prefix}"


def _state(value: str) -> str:
    if value not in TASK_STATES:
        raise ValueError(f"task state is not supported: {value}")
    return value


def _sha(value: str | None, field: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase commit SHA")
    return value


def _timestamp(value: str | None, field: str) -> str:
    if value is None:
        return dt.datetime.now(dt.timezone.utc).isoformat()
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return value


def _payload(value: object | None) -> str:
    if value is None:
        value = {}
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("event payload must be JSON-serializable") from exc


def _row(row: sqlite3.Row | None) -> dict[str, object] | None:
    return dict(row) if row is not None else None


def _is_database_file(path: pathlib.Path) -> bool:
    return path.exists() and not path.is_file()


def _absolute_path(value: pathlib.Path | str) -> pathlib.Path:
    """Make a path absolute without resolving away a final symlink."""
    path = pathlib.Path(value).expanduser()
    if path.is_symlink():
        raise ControlPlaneError(f"control database path must not be a symlink: {path}")
    return path.absolute()


MIGRATIONS = (
    Migration(
        version=1,
        identity=MIGRATION_ID,
        statements=(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                identity TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL CHECK (length(applied_at) > 0)
            )
            """,
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY
                    CHECK (
                        length(id) = 36 AND lower(id) = id AND
                        id NOT GLOB '*[^0-9a-f-]*' AND
                        substr(id, 9, 1) = '-' AND substr(id, 14, 1) = '-' AND
                        substr(id, 19, 1) = '-' AND substr(id, 24, 1) = '-'
                    ),
                identifier TEXT NOT NULL UNIQUE
                    CHECK (identifier GLOB 'T-[0-9][0-9][0-9][0-9][0-9][0-9]'),
                project_slug TEXT NOT NULL
                    CHECK (
                        length(project_slug) BETWEEN 1 AND 64 AND
                        project_slug NOT GLOB '*[^a-z0-9-]*' AND
                        project_slug GLOB '[a-z0-9]*'
                    ),
                title TEXT NOT NULL CHECK (length(trim(title)) > 0),
                objective TEXT NOT NULL CHECK (length(trim(objective)) > 0),
                state TEXT NOT NULL CHECK (state IN (
                    'PREPARED', 'QUEUED', 'ACTIVE', 'TERMINATED',
                    'READY_FOR_HUMAN_MERGE'
                )),
                base_ref TEXT NOT NULL CHECK (length(trim(base_ref)) > 0),
                base_sha TEXT NOT NULL CHECK (
                    length(base_sha) = 40 AND base_sha NOT GLOB '*[^0-9a-f]*'
                ),
                branch TEXT NOT NULL CHECK (length(trim(branch)) > 0),
                current_head TEXT CHECK (
                    current_head IS NULL OR
                    (length(current_head) = 40 AND current_head NOT GLOB '*[^0-9a-f]*')
                ),
                published_head TEXT CHECK (
                    published_head IS NULL OR
                    (length(published_head) = 40 AND published_head NOT GLOB '*[^0-9a-f]*')
                ),
                created_at TEXT NOT NULL CHECK (length(created_at) > 0),
                updated_at TEXT NOT NULL CHECK (length(updated_at) > 0)
            )
            """,
            """
            CREATE TABLE workpads (
                task_id TEXT PRIMARY KEY,
                body TEXT NOT NULL,
                version INTEGER NOT NULL CHECK (version >= 1),
                updated_at TEXT NOT NULL CHECK (length(updated_at) > 0),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE role_runs (
                id TEXT PRIMARY KEY
                    CHECK (
                        length(id) = 36 AND lower(id) = id AND
                        id NOT GLOB '*[^0-9a-f-]*' AND
                        substr(id, 9, 1) = '-' AND substr(id, 14, 1) = '-' AND
                        substr(id, 19, 1) = '-' AND substr(id, 24, 1) = '-'
                    ),
                task_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN (
                    'PROJECT-MANAGER', 'PLANNER', 'IMPLEMENTER',
                    'REVIEWER', 'ADVERSARY', 'ARCHIVIST'
                )),
                working_round_number INTEGER NOT NULL CHECK (working_round_number >= 1),
                lifecycle_id TEXT,
                working_round_id TEXT,
                planning_attempt_id TEXT,
                dispatch_id TEXT,
                execution_evidence_id TEXT,
                capability_grant_id TEXT,
                head_sha TEXT CHECK (
                    head_sha IS NULL OR
                    (length(head_sha) = 40 AND head_sha NOT GLOB '*[^0-9a-f]*')
                ),
                status TEXT NOT NULL CHECK (status IN ('running', 'finished', 'failed', 'blocked')),
                started_at TEXT NOT NULL CHECK (length(started_at) > 0),
                finished_at TEXT,
                result_summary TEXT,
                UNIQUE (id, task_id),
                UNIQUE (task_id, role, working_round_number, planning_attempt_id),
                CHECK ((status = 'running' AND finished_at IS NULL) OR
                       (status IN ('finished', 'failed', 'blocked') AND finished_at IS NOT NULL)),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE findings (
                id TEXT PRIMARY KEY
                    CHECK (
                        length(id) = 36 AND lower(id) = id AND
                        id NOT GLOB '*[^0-9a-f-]*' AND
                        substr(id, 9, 1) = '-' AND substr(id, 14, 1) = '-' AND
                        substr(id, 19, 1) = '-' AND substr(id, 24, 1) = '-'
                    ),
                task_id TEXT NOT NULL,
                role_run_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (length(trim(kind)) > 0),
                severity TEXT NOT NULL CHECK (severity IN ('info', 'low', 'medium', 'high', 'critical')),
                body TEXT NOT NULL CHECK (length(trim(body)) > 0),
                status TEXT NOT NULL CHECK (status IN ('open', 'accepted', 'rejected', 'resolved')),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT,
                FOREIGN KEY (role_run_id, task_id) REFERENCES role_runs(id, task_id) ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE blockers (
                id TEXT PRIMARY KEY
                    CHECK (
                        length(id) = 36 AND lower(id) = id AND
                        id NOT GLOB '*[^0-9a-f-]*' AND
                        substr(id, 9, 1) = '-' AND substr(id, 14, 1) = '-' AND
                        substr(id, 19, 1) = '-' AND substr(id, 24, 1) = '-'
                    ),
                task_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('human', 'project', 'infrastructure')),
                body TEXT NOT NULL CHECK (length(trim(body)) > 0),
                status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
                created_at TEXT NOT NULL CHECK (length(created_at) > 0),
                resolved_at TEXT,
                CHECK (
                    (status = 'open' AND resolved_at IS NULL) OR
                    (status = 'resolved' AND resolved_at IS NOT NULL)
                ),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE publications (
                task_id TEXT PRIMARY KEY,
                head_sha TEXT CHECK (
                    head_sha IS NULL OR
                    (length(head_sha) = 40 AND head_sha NOT GLOB '*[^0-9a-f]*')
                ),
                remote_branch TEXT,
                github_pr_number INTEGER CHECK (github_pr_number IS NULL OR github_pr_number >= 1),
                publication_status TEXT NOT NULL CHECK (
                    publication_status IN ('not_started', 'started', 'published', 'failed')
                ),
                published_at TEXT,
                CHECK (
                    (publication_status = 'published' AND head_sha IS NOT NULL AND published_at IS NOT NULL) OR
                    publication_status <> 'published'
                ),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE task_events (
                id TEXT PRIMARY KEY
                    CHECK (
                        length(id) = 36 AND lower(id) = id AND
                        id NOT GLOB '*[^0-9a-f-]*' AND
                        substr(id, 9, 1) = '-' AND substr(id, 14, 1) = '-' AND
                        substr(id, 19, 1) = '-' AND substr(id, 24, 1) = '-'
                    ),
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type IN (
                    'task_created', 'queued', 'lifecycle_started',
                    'dispatch_authorized', 'execution_started', 'execution_terminated',
                    'execution_observed',
                    'execution_reconciled', 'finding_recorded',
                    'planning_correction_required', 'planning_accepted',
                    'working_round_started', 'working_round_converged', 'working_round_non_converged',
                    'working_round_handoff_requested',
                    'lifecycle_converged', 'lifecycle_non_converged',
                    'mechanical_validation_passed', 'mechanical_validation_failed',
                    'lifecycle_terminated', 'human_disposition_recorded',
                    'head_changed', 'writer_delta_validated',
                    'host_commit_created', 'publication_started',
                    'publication_finished', 'blocker_recorded',
                    'blocker_resolved', 'ready_for_human_merge'
                )),
                role_run_id TEXT,
                payload_json TEXT NOT NULL CHECK (length(payload_json) > 0 AND json_valid(payload_json)),
                occurred_at TEXT NOT NULL CHECK (length(occurred_at) > 0),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT,
                FOREIGN KEY (role_run_id, task_id) REFERENCES role_runs(id, task_id) ON DELETE RESTRICT
            )
            """,
            "CREATE INDEX tasks_project_state_idx ON tasks(project_slug, state)",
            "CREATE INDEX tasks_updated_idx ON tasks(updated_at)",
            "CREATE INDEX role_runs_task_idx ON role_runs(task_id, role, working_round_number)",
            "CREATE INDEX findings_task_status_idx ON findings(task_id, status)",
            "CREATE INDEX blockers_task_status_idx ON blockers(task_id, status)",
            "CREATE INDEX task_events_task_time_idx ON task_events(task_id, occurred_at, id)",
            "CREATE INDEX task_events_type_idx ON task_events(event_type)",
        ),
    ),
    Migration(
        version=2,
        identity=STORAGE_MIGRATION_ID,
        statements=(
            """
            CREATE TABLE storage_domains (
                project_slug TEXT PRIMARY KEY
                    CHECK (
                        length(project_slug) BETWEEN 1 AND 64 AND
                        project_slug NOT GLOB '*[^a-z0-9-]*' AND
                        project_slug GLOB '[a-z0-9]*'
                    ),
                source TEXT NOT NULL CHECK (length(trim(source)) > 0),
                mount_target TEXT NOT NULL CHECK (length(trim(mount_target)) > 0),
                fstype TEXT NOT NULL CHECK (fstype = 'ext4'),
                mount_options TEXT NOT NULL CHECK (length(trim(mount_options)) > 0),
                pool_bytes INTEGER NOT NULL CHECK (pool_bytes > 0),
                pool_inodes INTEGER NOT NULL CHECK (pool_inodes > 0),
                free_bytes INTEGER NOT NULL CHECK (free_bytes >= 0 AND free_bytes <= pool_bytes),
                free_inodes INTEGER NOT NULL CHECK (free_inodes >= 0 AND free_inodes <= pool_inodes),
                verified_at TEXT NOT NULL CHECK (length(verified_at) > 0),
                evidence_json TEXT NOT NULL CHECK (length(evidence_json) > 0 AND json_valid(evidence_json))
            )
            """,
            """
            CREATE TABLE storage_reservations (
                task_id TEXT PRIMARY KEY,
                project_slug TEXT NOT NULL
                    CHECK (
                        length(project_slug) BETWEEN 1 AND 64 AND
                        project_slug NOT GLOB '*[^a-z0-9-]*' AND
                        project_slug GLOB '[a-z0-9]*'
                    ),
                quota_id INTEGER NOT NULL UNIQUE CHECK (quota_id >= 1),
                reserved_bytes INTEGER NOT NULL CHECK (reserved_bytes > 0),
                reserved_inodes INTEGER NOT NULL CHECK (reserved_inodes > 0),
                status TEXT NOT NULL CHECK (status IN ('reserved', 'released')),
                created_at TEXT NOT NULL CHECK (length(created_at) > 0),
                released_at TEXT,
                CHECK ((status = 'reserved' AND released_at IS NULL) OR
                       (status = 'released' AND released_at IS NOT NULL)),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT
            )
            """,
            "CREATE INDEX storage_reservations_project_status_idx ON storage_reservations(project_slug, status)",
        ),
    ),
    Migration(
        version=3,
        identity=HARNESS_MIGRATION_ID,
        statements=(
            """
            CREATE TABLE lifecycles (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
                state TEXT NOT NULL CHECK (state IN ('RUNNING', 'ACCEPTED', 'NON_CONVERGED')),
                terminal_outcome TEXT CHECK (terminal_outcome IS NULL OR terminal_outcome IN ('SUCCESSFUL', 'NON_CONVERGED')),
                convergence_status TEXT NOT NULL DEFAULT 'NOT_RECORDED' CHECK (convergence_status IN ('NOT_RECORDED', 'RECORDED')),
                convergence_head TEXT,
                convergence_at TEXT,
                mechanical_validation_status TEXT NOT NULL DEFAULT 'NOT_RUN' CHECK (mechanical_validation_status IN ('NOT_RUN', 'PASSED', 'FAILED')),
                mechanical_validation_head TEXT,
                mechanical_validation_at TEXT,
                mechanical_validation_evidence_json TEXT,
                mechanical_acceptance TEXT NOT NULL DEFAULT 'NOT_REACHED' CHECK (mechanical_acceptance IN ('NOT_REACHED', 'ACCEPTED')),
                mechanical_acceptance_head TEXT,
                mechanical_acceptance_at TEXT,
                max_working_rounds INTEGER NOT NULL DEFAULT 8 CHECK (max_working_rounds = 8),
                started_at TEXT NOT NULL,
                ended_at TEXT,
                UNIQUE (id, task_id),
                UNIQUE (task_id, ordinal),
                CHECK ((state = 'RUNNING' AND terminal_outcome IS NULL AND ended_at IS NULL) OR
                       (state <> 'RUNNING' AND terminal_outcome IS NOT NULL AND ended_at IS NOT NULL)),
                CHECK ((convergence_status = 'NOT_RECORDED' AND convergence_head IS NULL AND convergence_at IS NULL) OR
                       (convergence_status = 'RECORDED' AND convergence_head IS NOT NULL AND convergence_at IS NOT NULL)),
                CHECK ((mechanical_validation_status = 'NOT_RUN' AND mechanical_validation_head IS NULL AND mechanical_validation_at IS NULL AND mechanical_validation_evidence_json IS NULL) OR
                       (mechanical_validation_status IN ('PASSED', 'FAILED') AND mechanical_validation_head IS NOT NULL AND mechanical_validation_at IS NOT NULL AND mechanical_validation_evidence_json IS NOT NULL AND json_valid(mechanical_validation_evidence_json))),
                CHECK ((mechanical_acceptance = 'NOT_REACHED' AND mechanical_acceptance_head IS NULL AND mechanical_acceptance_at IS NULL) OR
                       (mechanical_acceptance = 'ACCEPTED' AND mechanical_acceptance_head IS NOT NULL AND mechanical_acceptance_at IS NOT NULL AND mechanical_validation_status = 'PASSED')),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE working_rounds (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                lifecycle_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL CHECK (ordinal >= 1 AND ordinal <= 8),
                state TEXT NOT NULL CHECK (state IN ('PLANNING', 'IMPLEMENTING', 'ADVERSARIAL', 'CONVERGED', 'NON_CONVERGED')),
                max_planning_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_planning_attempts = 3),
                started_at TEXT NOT NULL,
                ended_at TEXT,
                UNIQUE (id, task_id),
                UNIQUE (lifecycle_id, ordinal),
                FOREIGN KEY (lifecycle_id, task_id) REFERENCES lifecycles(id, task_id) ON DELETE RESTRICT,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE planning_attempts (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                lifecycle_id TEXT NOT NULL,
                working_round_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL CHECK (ordinal >= 1 AND ordinal <= 3),
                state TEXT NOT NULL CHECK (state IN ('OPEN', 'CORRECTION_REQUIRED', 'ACCEPTED')),
                started_at TEXT NOT NULL,
                ended_at TEXT,
                UNIQUE (id, task_id),
                UNIQUE (working_round_id, ordinal),
                FOREIGN KEY (working_round_id, task_id) REFERENCES working_rounds(id, task_id) ON DELETE RESTRICT,
                FOREIGN KEY (lifecycle_id, task_id) REFERENCES lifecycles(id, task_id) ON DELETE RESTRICT,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE role_dispatches (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                lifecycle_id TEXT NOT NULL,
                working_round_id TEXT,
                planning_attempt_id TEXT,
                role TEXT NOT NULL CHECK (role IN ('PROJECT-MANAGER', 'PLANNER', 'IMPLEMENTER', 'REVIEWER', 'ADVERSARY', 'ARCHIVIST')),
                status TEXT NOT NULL CHECK (status IN ('AUTHORIZED', 'RUNNING', 'CONSUMED', 'REJECTED')),
                expected_starting_head TEXT NOT NULL,
                created_at TEXT NOT NULL,
                consumed_at TEXT,
                UNIQUE (id, task_id),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT,
                FOREIGN KEY (lifecycle_id, task_id) REFERENCES lifecycles(id, task_id) ON DELETE RESTRICT,
                FOREIGN KEY (working_round_id, task_id) REFERENCES working_rounds(id, task_id) ON DELETE RESTRICT,
                FOREIGN KEY (planning_attempt_id, task_id) REFERENCES planning_attempts(id, task_id) ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE capability_grants (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                dispatch_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('PROJECT-MANAGER', 'PLANNER', 'IMPLEMENTER', 'REVIEWER', 'ADVERSARY', 'ARCHIVIST')),
                read_scopes_json TEXT NOT NULL CHECK (json_valid(read_scopes_json)),
                write_scopes_json TEXT NOT NULL CHECK (json_valid(write_scopes_json)),
                issued_at TEXT NOT NULL,
                UNIQUE (id, task_id),
                UNIQUE (dispatch_id, task_id),
                FOREIGN KEY (dispatch_id, task_id) REFERENCES role_dispatches(id, task_id) ON DELETE RESTRICT,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE execution_evidence (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                dispatch_id TEXT NOT NULL,
                role_run_id TEXT NOT NULL,
                runtime_execution_id TEXT NOT NULL,
                observed_role TEXT NOT NULL CHECK (observed_role IN ('PROJECT-MANAGER', 'PLANNER', 'IMPLEMENTER', 'REVIEWER', 'ADVERSARY', 'ARCHIVIST')),
                status TEXT NOT NULL CHECK (status IN ('running', 'finished', 'failed', 'blocked')),
                started_at TEXT NOT NULL,
                finished_at TEXT,
                receipt_json TEXT NOT NULL CHECK (json_valid(receipt_json)),
                UNIQUE (id, task_id),
                UNIQUE (runtime_execution_id, task_id),
                UNIQUE (role_run_id, task_id),
                CHECK ((status = 'running' AND finished_at IS NULL) OR
                       (status IN ('finished', 'failed', 'blocked') AND finished_at IS NOT NULL)),
                FOREIGN KEY (dispatch_id, task_id) REFERENCES role_dispatches(id, task_id) ON DELETE RESTRICT,
                FOREIGN KEY (role_run_id, task_id) REFERENCES role_runs(id, task_id) ON DELETE RESTRICT,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE writer_deltas (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                role_run_id TEXT NOT NULL,
                capability_grant_id TEXT NOT NULL,
                changed_paths_json TEXT NOT NULL CHECK (json_valid(changed_paths_json)),
                authorization_status TEXT NOT NULL CHECK (authorization_status IN ('ACCEPTED', 'REJECTED')),
                workspace_head TEXT NOT NULL,
                commit_sha TEXT,
                dirty INTEGER NOT NULL CHECK (dirty IN (0, 1)),
                observed_at TEXT NOT NULL,
                UNIQUE (id, task_id),
                FOREIGN KEY (role_run_id, task_id) REFERENCES role_runs(id, task_id) ON DELETE RESTRICT,
                FOREIGN KEY (capability_grant_id, task_id) REFERENCES capability_grants(id, task_id) ON DELETE RESTRICT,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE human_dispositions (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                lifecycle_id TEXT NOT NULL,
                decision TEXT NOT NULL CHECK (decision IN ('START_ANOTHER_LIFECYCLE', 'HOLD', 'CLOSE_TASK')),
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (lifecycle_id, task_id) REFERENCES lifecycles(id, task_id) ON DELETE RESTRICT,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT
            )
            """,
            "CREATE INDEX lifecycles_task_idx ON lifecycles(task_id, ordinal)",
            "CREATE INDEX working_rounds_lifecycle_idx ON working_rounds(lifecycle_id, ordinal)",
            "CREATE INDEX planning_attempts_round_idx ON planning_attempts(working_round_id, ordinal)",
            "CREATE INDEX role_dispatches_task_idx ON role_dispatches(task_id, created_at, id)",
            "CREATE INDEX capability_grants_task_idx ON capability_grants(task_id, issued_at, id)",
            "CREATE INDEX execution_evidence_task_idx ON execution_evidence(task_id, finished_at, id)",
            "CREATE INDEX writer_deltas_task_idx ON writer_deltas(task_id, observed_at, id)",
            "CREATE INDEX human_dispositions_task_idx ON human_dispositions(task_id, created_at, id)",
        ),
    ),
)


EXPECTED_TABLE_COLUMNS = {
    "schema_migrations": {"version", "identity", "applied_at"},
    "tasks": {
        "id", "identifier", "project_slug", "title", "objective", "state", "base_ref",
        "base_sha", "branch", "current_head", "published_head", "created_at", "updated_at",
    },
    "workpads": {"task_id", "body", "version", "updated_at"},
    "role_runs": {
        "id", "task_id", "role", "working_round_number", "lifecycle_id", "working_round_id",
        "planning_attempt_id", "dispatch_id", "execution_evidence_id", "capability_grant_id",
        "head_sha", "status", "started_at", "finished_at", "result_summary",
    },
    "findings": {
        "id", "task_id", "role_run_id", "kind", "severity", "body", "status",
    },
    "blockers": {"id", "task_id", "kind", "body", "status", "created_at", "resolved_at"},
    "publications": {"task_id", "head_sha", "remote_branch", "github_pr_number", "publication_status", "published_at"},
    "task_events": {"id", "task_id", "event_type", "role_run_id", "payload_json", "occurred_at"},
    "storage_domains": {
        "project_slug", "source", "mount_target", "fstype", "mount_options",
        "pool_bytes", "pool_inodes", "free_bytes", "free_inodes", "verified_at", "evidence_json",
    },
    "storage_reservations": {
        "task_id", "project_slug", "quota_id", "reserved_bytes", "reserved_inodes",
        "status", "created_at", "released_at",
    },
    "lifecycles": {"id", "task_id", "ordinal", "state", "terminal_outcome", "convergence_status", "convergence_head", "convergence_at", "mechanical_validation_status", "mechanical_validation_head", "mechanical_validation_at", "mechanical_validation_evidence_json", "mechanical_acceptance", "mechanical_acceptance_head", "mechanical_acceptance_at", "max_working_rounds", "started_at", "ended_at"},
    "working_rounds": {"id", "task_id", "lifecycle_id", "ordinal", "state", "max_planning_attempts", "started_at", "ended_at"},
    "planning_attempts": {"id", "task_id", "lifecycle_id", "working_round_id", "ordinal", "state", "started_at", "ended_at"},
    "role_dispatches": {"id", "task_id", "lifecycle_id", "working_round_id", "planning_attempt_id", "role", "status", "expected_starting_head", "created_at", "consumed_at"},
    "capability_grants": {"id", "task_id", "dispatch_id", "role", "read_scopes_json", "write_scopes_json", "issued_at"},
    "execution_evidence": {"id", "task_id", "dispatch_id", "role_run_id", "runtime_execution_id", "observed_role", "status", "started_at", "finished_at", "receipt_json"},
    "writer_deltas": {"id", "task_id", "role_run_id", "capability_grant_id", "changed_paths_json", "authorization_status", "workspace_head", "commit_sha", "dirty", "observed_at"},
    "human_dispositions": {"id", "task_id", "lifecycle_id", "decision", "detail", "created_at"},
}

EXPECTED_INDEXES = {
    "tasks_project_state_idx": ("tasks", False, ("project_slug", "state")),
    "tasks_updated_idx": ("tasks", False, ("updated_at",)),
    "role_runs_task_idx": ("role_runs", False, ("task_id", "role", "working_round_number")),
    "findings_task_status_idx": ("findings", False, ("task_id", "status")),
    "blockers_task_status_idx": ("blockers", False, ("task_id", "status")),
    "task_events_task_time_idx": ("task_events", False, ("task_id", "occurred_at", "id")),
    "task_events_type_idx": ("task_events", False, ("event_type",)),
    "storage_reservations_project_status_idx": (
        "storage_reservations", False, ("project_slug", "status")
    ),
    "lifecycles_task_idx": ("lifecycles", False, ("task_id", "ordinal")),
    "working_rounds_lifecycle_idx": ("working_rounds", False, ("lifecycle_id", "ordinal")),
    "planning_attempts_round_idx": ("planning_attempts", False, ("working_round_id", "ordinal")),
    "role_dispatches_task_idx": ("role_dispatches", False, ("task_id", "created_at", "id")),
    "capability_grants_task_idx": ("capability_grants", False, ("task_id", "issued_at", "id")),
    "execution_evidence_task_idx": ("execution_evidence", False, ("task_id", "finished_at", "id")),
    "writer_deltas_task_idx": ("writer_deltas", False, ("task_id", "observed_at", "id")),
    "human_dispositions_task_idx": ("human_dispositions", False, ("task_id", "created_at", "id")),
}

EXPECTED_UNIQUE_INDEX_COLUMNS = {
    # INTEGER PRIMARY KEY is rowid-backed and therefore has no index entry.
    "schema_migrations": {("identity",)},
    "tasks": {("id",), ("identifier",)},
    "workpads": {("task_id",)},
    "role_runs": {("id",), ("id", "task_id"), ("task_id", "role", "working_round_number", "planning_attempt_id")},
    "findings": {("id",)},
    "blockers": {("id",)},
    "publications": {("task_id",)},
    "task_events": {("id",)},
    "storage_domains": {("project_slug",)},
    "storage_reservations": {("task_id",), ("quota_id",)},
    "lifecycles": {("id",), ("id", "task_id"), ("task_id", "ordinal")},
    "working_rounds": {("id",), ("id", "task_id"), ("lifecycle_id", "ordinal")},
    "planning_attempts": {("id",), ("id", "task_id"), ("working_round_id", "ordinal")},
    "role_dispatches": {("id",), ("id", "task_id")},
    "capability_grants": {("id",), ("id", "task_id"), ("dispatch_id", "task_id")},
    "execution_evidence": {("id",), ("id", "task_id"), ("runtime_execution_id", "task_id"), ("role_run_id", "task_id")},
    "writer_deltas": {("id",), ("id", "task_id")},
    "human_dispositions": {("id",)},
}

EXPECTED_FOREIGN_KEYS = {
    "schema_migrations": set(),
    "tasks": set(),
    "workpads": {(('task_id',), "tasks", ("id",), "NO ACTION", "CASCADE", "NONE")},
    "role_runs": {(('task_id',), "tasks", ("id",), "NO ACTION", "RESTRICT", "NONE")},
    "findings": {
        (("task_id",), "tasks", ("id",), "NO ACTION", "RESTRICT", "NONE"),
        (("role_run_id", "task_id"), "role_runs", ("id", "task_id"), "NO ACTION", "RESTRICT", "NONE"),
    },
    "blockers": {(('task_id',), "tasks", ("id",), "NO ACTION", "RESTRICT", "NONE")},
    "publications": {(('task_id',), "tasks", ("id",), "NO ACTION", "RESTRICT", "NONE")},
    "task_events": {
        (("task_id",), "tasks", ("id",), "NO ACTION", "RESTRICT", "NONE"),
        (("role_run_id", "task_id"), "role_runs", ("id", "task_id"), "NO ACTION", "RESTRICT", "NONE"),
    },
    "storage_domains": set(),
    "storage_reservations": {
        (("task_id",), "tasks", ("id",), "NO ACTION", "RESTRICT", "NONE"),
    },
    "lifecycles": {(('task_id',), "tasks", ("id",), "NO ACTION", "RESTRICT", "NONE")},
    "working_rounds": {
        (('lifecycle_id', 'task_id'), "lifecycles", ('id', 'task_id'), "NO ACTION", "RESTRICT", "NONE"),
        (('task_id',), "tasks", ('id',), "NO ACTION", "RESTRICT", "NONE"),
    },
    "planning_attempts": {
        (('working_round_id', 'task_id'), "working_rounds", ('id', 'task_id'), "NO ACTION", "RESTRICT", "NONE"),
        (('lifecycle_id', 'task_id'), "lifecycles", ('id', 'task_id'), "NO ACTION", "RESTRICT", "NONE"),
        (('task_id',), "tasks", ('id',), "NO ACTION", "RESTRICT", "NONE"),
    },
    "role_dispatches": {
        (('task_id',), "tasks", ('id',), "NO ACTION", "RESTRICT", "NONE"),
        (('lifecycle_id', 'task_id'), "lifecycles", ('id', 'task_id'), "NO ACTION", "RESTRICT", "NONE"),
        (('working_round_id', 'task_id'), "working_rounds", ('id', 'task_id'), "NO ACTION", "RESTRICT", "NONE"),
        (('planning_attempt_id', 'task_id'), "planning_attempts", ('id', 'task_id'), "NO ACTION", "RESTRICT", "NONE"),
    },
    "capability_grants": {
        (('dispatch_id', 'task_id'), "role_dispatches", ('id', 'task_id'), "NO ACTION", "RESTRICT", "NONE"),
        (('task_id',), "tasks", ('id',), "NO ACTION", "RESTRICT", "NONE"),
    },
    "execution_evidence": {
        (('dispatch_id', 'task_id'), "role_dispatches", ('id', 'task_id'), "NO ACTION", "RESTRICT", "NONE"),
        (('role_run_id', 'task_id'), "role_runs", ('id', 'task_id'), "NO ACTION", "RESTRICT", "NONE"),
        (('task_id',), "tasks", ('id',), "NO ACTION", "RESTRICT", "NONE"),
    },
    "writer_deltas": {
        (('role_run_id', 'task_id'), "role_runs", ('id', 'task_id'), "NO ACTION", "RESTRICT", "NONE"),
        (('capability_grant_id', 'task_id'), "capability_grants", ('id', 'task_id'), "NO ACTION", "RESTRICT", "NONE"),
        (('task_id',), "tasks", ('id',), "NO ACTION", "RESTRICT", "NONE"),
    },
    "human_dispositions": {
        (('lifecycle_id', 'task_id'), "lifecycles", ('id', 'task_id'), "NO ACTION", "RESTRICT", "NONE"),
        (('task_id',), "tasks", ('id',), "NO ACTION", "RESTRICT", "NONE"),
    },
}


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower()
    if journal_mode != "wal":
        raise SchemaError(f"SQLite WAL mode was not accepted: {journal_mode}")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.row_factory = sqlite3.Row


def _schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _schema_signature(connection: sqlite3.Connection) -> str:
    """Hash the persistent schema objects, excluding SQLite's internal objects."""
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    canonical = json.dumps([tuple(row) for row in rows], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@functools.lru_cache(maxsize=1)
def _expected_schema_signature() -> str:
    """Generate the expected physical schema from the checked-in migration."""
    connection = sqlite3.connect(":memory:")
    try:
        for migration in MIGRATIONS:
            for statement in migration.statements:
                connection.execute(statement)
        return _schema_signature(connection)
    finally:
        connection.close()


def _foreign_key_signature(connection: sqlite3.Connection, table: str) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str, str, str]]:
    grouped: dict[int, list[sqlite3.Row | tuple]] = {}
    for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
        grouped.setdefault(int(row[0]), []).append(row)
    return {
        (
            tuple(str(row[3]) for row in sorted(rows, key=lambda value: int(value[1]))),
            str(rows[0][2]),
            tuple(str(row[4]) for row in sorted(rows, key=lambda value: int(value[1]))),
            str(rows[0][5]),
            str(rows[0][6]),
            str(rows[0][7]),
        )
        for rows in grouped.values()
    }


def _index_signature(connection: sqlite3.Connection, index_name: str) -> tuple[str, bool, tuple[str, ...]]:
    table = EXPECTED_INDEXES[index_name][0]
    row = next((row for row in connection.execute(f"PRAGMA index_list({table})") if row[1] == index_name), None)
    if row is None:
        raise SchemaError(f"SQLite expected index is missing: {index_name}")
    columns = tuple(
        str(info[2])
        for info in connection.execute("SELECT * FROM pragma_index_info(?) ORDER BY seqno", (index_name,))
    )
    return table, bool(row[2]), columns


def _validate_schema(connection: sqlite3.Connection) -> None:
    version = _schema_version(connection)
    if version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"SQLite schema version {version} is newer than supported version {CURRENT_SCHEMA_VERSION}"
        )
    if version != CURRENT_SCHEMA_VERSION:
        raise SchemaError(f"SQLite schema version is incomplete: {version}")

    objects = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    expected_objects = {
        **{name: "table" for name in EXPECTED_TABLE_COLUMNS},
        **{name: "index" for name in EXPECTED_INDEXES},
    }
    unexpected_objects = {
        (name, object_type) for name, object_type in objects.items()
        if expected_objects.get(name) != object_type
    }
    if unexpected_objects:
        raise SchemaError(f"SQLite contains unsupported persistent objects: {sorted(unexpected_objects)}")
    for table in EXPECTED_TABLE_COLUMNS:
        if objects.get(table) != "table":
            raise SchemaError(f"SQLite schema object is missing or invalid: {table}")
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns != EXPECTED_TABLE_COLUMNS[table]:
            raise SchemaError(f"SQLite table columns are invalid: {table}")

    migration_rows = connection.execute(
        "SELECT version, identity FROM schema_migrations ORDER BY version"
    ).fetchall()
    if [(row[0], row[1]) for row in migration_rows] != [
        (1, MIGRATION_ID), (2, STORAGE_MIGRATION_ID), (3, HARNESS_MIGRATION_ID)
    ]:
        raise SchemaError("SQLite migration history is invalid")

    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    if foreign_keys != 1:
        raise SchemaError("SQLite foreign-key enforcement is disabled")

    if _schema_signature(connection) != _expected_schema_signature():
        raise SchemaError("SQLite physical schema does not match the checked-in migration contract")

    for index_name, (table, unique, columns) in EXPECTED_INDEXES.items():
        actual_table, actual_unique, actual_columns = _index_signature(connection, index_name)
        if (actual_table, actual_unique, actual_columns) != (table, unique, columns):
            raise SchemaError(f"SQLite index semantics are invalid: {index_name}")

    for table, expected_unique in EXPECTED_UNIQUE_INDEX_COLUMNS.items():
        actual_unique = {
            tuple(
                str(info[2])
                for info in connection.execute(
                    "SELECT * FROM pragma_index_info(?) ORDER BY seqno", (row[1],)
                )
            )
            for row in connection.execute(f"PRAGMA index_list({table})")
            if bool(row[2])
        }
        if actual_unique != expected_unique:
            raise SchemaError(f"SQLite uniqueness semantics are invalid: {table}")

    for table, expected_foreign_keys in EXPECTED_FOREIGN_KEYS.items():
        if _foreign_key_signature(connection, table) != expected_foreign_keys:
            raise SchemaError(f"SQLite foreign-key definitions are invalid: {table}")

    try:
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as exc:
        # Some SQLite builds report corruption by raising here rather than by
        # returning a non-"ok" row. Preserve the public fail-closed contract.
        raise SchemaError("SQLite integrity_check could not read the database") from exc
    integrity_errors = [str(row[0]) for row in integrity_rows if str(row[0]).lower() != "ok"]
    if integrity_errors:
        raise SchemaError(f"SQLite integrity_check failed: {integrity_errors}")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise SchemaError(f"SQLite foreign_key_check failed: {foreign_key_errors}")


def _connect_raw(path: pathlib.Path) -> sqlite3.Connection:
    if path.exists() and path.is_symlink():
        raise ControlPlaneError(f"control database path must not be a symlink: {path}")
    if _is_database_file(path):
        raise ControlPlaneError(f"control database path is not a regular file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    connection = sqlite3.connect(
        path,
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    try:
        _configure_connection(connection)
        path.chmod(0o600)
        return connection
    except Exception:
        connection.close()
        raise


def _connect_readonly(path: pathlib.Path) -> sqlite3.Connection:
    """Open a database snapshot without changing its journal or permissions."""
    if path.is_symlink() or not path.is_file():
        raise ControlPlaneError("control database snapshot must be a regular file")
    connection = sqlite3.connect(
        path.as_uri() + "?mode=ro",
        uri=True,
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.row_factory = sqlite3.Row
        return connection
    except Exception:
        connection.close()
        raise


def _migrate(connection: sqlite3.Connection) -> None:
    current = _schema_version(connection)
    if current > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"SQLite schema version {current} is newer than supported version {CURRENT_SCHEMA_VERSION}"
        )
    if current < 0:
        raise SchemaError("SQLite schema version is negative")
    if current in {1, 2}:
        raise SchemaError("obsolete pre-harness Pilot schema must be replaced before opening")

    pending = [migration for migration in MIGRATIONS if migration.version > current]
    if not pending and current != CURRENT_SCHEMA_VERSION:
        raise SchemaError(f"no migration path from SQLite schema version {current}")
    if not pending:
        _validate_schema(connection)
        return

    connection.execute("BEGIN IMMEDIATE")
    try:
        for migration in pending:
            if migration.version != current + 1:
                raise SchemaError("SQLite migration order is not contiguous")
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, identity, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.identity, dt.datetime.now(dt.timezone.utc).isoformat()),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")
            current = migration.version
        connection.commit()
    except Exception as exc:
        connection.rollback()
        if isinstance(exc, ControlPlaneError):
            raise
        if isinstance(exc, sqlite3.DatabaseError):
            raise SchemaError("SQLite migration failed and was rolled back") from exc
        raise
    _validate_schema(connection)


class ControlPlaneDatabase:
    """Small transactional API over the host-owned control database."""

    def __init__(self, path: pathlib.Path, connection: sqlite3.Connection):
        self.path = path
        self.connection = connection
        self._closed = False

    @classmethod
    def open(cls, path: pathlib.Path | str | None = None) -> "ControlPlaneDatabase":
        database_path = _absolute_path(path) if path is not None else default_database_path()
        connection = _connect_raw(database_path)
        try:
            _migrate(connection)
            database = cls(database_path, connection)
            _OPEN_DATABASE_PATHS[database_path] = _OPEN_DATABASE_PATHS.get(database_path, 0) + 1
            return database
        except Exception:
            connection.close()
            raise

    @classmethod
    def open_readonly(cls, path: pathlib.Path | str | None = None) -> "ControlPlaneDatabase":
        """Open current authoritative state without creating or preparing it.

        This path performs no migration, permission change, or journal
        configuration. SQLite ``mode=ro`` mechanically denies writes, while
        full schema validation prevents reinterpretation of stale state.
        """
        database_path = _absolute_path(path) if path is not None else default_database_path()
        connection = _connect_readonly(database_path)
        try:
            current = _schema_version(connection)
            if current > CURRENT_SCHEMA_VERSION:
                raise UnsupportedSchemaVersion(
                    f"SQLite schema version {current} is newer than supported version "
                    f"{CURRENT_SCHEMA_VERSION}"
                )
            if current != CURRENT_SCHEMA_VERSION:
                raise SchemaError(
                    f"read-only open requires schema version {CURRENT_SCHEMA_VERSION}; found {current}"
                )
            _validate_schema(connection)
            database = cls(database_path, connection)
            # Restore requires the authority to be offline. Read handles must
            # therefore participate in the same accounting as write handles.
            _OPEN_DATABASE_PATHS[database_path] = _OPEN_DATABASE_PATHS.get(database_path, 0) + 1
            return database
        except Exception:
            connection.close()
            raise

    def close(self) -> None:
        if not self._closed:
            self.connection.close()
            open_count = _OPEN_DATABASE_PATHS.get(self.path, 0)
            if open_count <= 1:
                _OPEN_DATABASE_PATHS.pop(self.path, None)
            else:
                _OPEN_DATABASE_PATHS[self.path] = open_count - 1
            self._closed = True

    def __enter__(self) -> "ControlPlaneDatabase":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return _schema_version(self.connection)

    @contextlib.contextmanager
    def _transaction(self, *, immediate: bool = True) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _allocate_identifier(self) -> str:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(CAST(substr(identifier, 3) AS INTEGER)), 0) + 1 FROM tasks"
        ).fetchone()
        next_number = int(row[0])
        if next_number > 999_999:
            raise ControlPlaneError("task identifier allocation exhausted")
        return f"T-{next_number:06d}"

    def create_task(
        self,
        *,
        project_slug: str,
        title: str,
        objective: str,
        base_ref: str,
        base_sha: str,
        task_id: str | uuid.UUID | None = None,
        identifier: str | None = None,
        state: str = "PREPARED",
        current_head: str | None = None,
        published_head: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, object]:
        task_id = _uuid(task_id, "task_id")
        project_slug = _project_slug(project_slug)
        title = _text(title, "title")
        objective = _text(objective, "objective")
        base_ref = _text(base_ref, "base_ref")
        base_sha = _sha(base_sha, "base_sha", required=True)
        current_head = _sha(current_head, "current_head")
        published_head = _sha(published_head, "published_head")
        if published_head is not None:
            raise StateConflict(
                "published_head is managed by a successful publication and cannot be seeded directly"
            )
        state = _state(state)
        if identifier is not None and not IDENTIFIER_RE.fullmatch(identifier):
            raise ValueError("identifier must match T-000042")
        timestamp = _timestamp(created_at, "created_at")
        result: dict[str, object] | None = None
        with self._transaction():
            identifier = identifier or self._allocate_identifier()
            branch = derive_task_branch(identifier, task_id)
            self.connection.execute(
                """
                INSERT INTO tasks(
                    id, identifier, project_slug, title, objective, state, base_ref, base_sha,
                    branch, current_head, published_head, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, identifier, project_slug, title, objective, state, base_ref, base_sha,
                    branch, current_head, published_head, timestamp, timestamp,
                ),
            )
            self._insert_event(
                task_id,
                "task_created",
                {"identifier": identifier, "project_slug": project_slug},
                occurred_at=timestamp,
            )
        return self.read_task(task_id)

    def read_task(self, task_id: str | uuid.UUID) -> dict[str, object]:
        task_id = _uuid(task_id, "task_id")
        value = _row(self.connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        if value is None:
            raise ControlPlaneError(f"task does not exist: {task_id}")
        return value

    def record_storage_domain(self, domain: object) -> dict[str, object]:
        """Persist one host-verified observation of the shared pool."""
        from storage import StorageContractError, VerifiedStorageDomain

        if not isinstance(domain, VerifiedStorageDomain):
            raise StorageContractError("storage domain must be verified before persistence")
        timestamp = _timestamp(None, "verified_at")
        with self._transaction():
            existing = self.connection.execute(
                "SELECT * FROM storage_domains WHERE project_slug = ?", (domain.project,)
            ).fetchone()
            proposed = (domain.source, domain.target, domain.fstype, domain.options)
            for row in self.connection.execute(
                "SELECT source, mount_target, fstype, mount_options FROM storage_domains"
            ).fetchall():
                identity = (row["source"], row["mount_target"], row["fstype"], row["mount_options"])
                if identity != proposed:
                    raise StateConflict("shared storage pool identity changed")
            self.connection.execute(
                """
                INSERT INTO storage_domains(
                    project_slug, source, mount_target, fstype, mount_options,
                    pool_bytes, pool_inodes, free_bytes, free_inodes, verified_at, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_slug) DO UPDATE SET
                    source = excluded.source, mount_target = excluded.mount_target,
                    fstype = excluded.fstype, mount_options = excluded.mount_options,
                    pool_bytes = excluded.pool_bytes, pool_inodes = excluded.pool_inodes,
                    free_bytes = excluded.free_bytes, free_inodes = excluded.free_inodes,
                    verified_at = excluded.verified_at, evidence_json = excluded.evidence_json
                """,
                (domain.project, domain.source, domain.target, domain.fstype, domain.options,
                 domain.pool_bytes, domain.pool_inodes, domain.free_bytes, domain.free_inodes,
                 timestamp, domain.evidence_json),
            )
            result = self.connection.execute(
                "SELECT * FROM storage_domains WHERE project_slug = ?", (domain.project,)
            ).fetchone()
        assert result is not None
        return dict(result)

    def queue_task(self, task_id: str | uuid.UUID, *, project_slug: str) -> dict[str, object]:
        """Queue a prepared local task and create its initial workpad.

        This is the ordinary operator-controlled queue operation. Storage
        admission is an independent dormant hardening path; local development
        queueing only needs the canonical SQLite lifecycle transition.
        """
        task_id = _uuid(task_id, "task_id")
        project_slug = _project_slug(project_slug)
        timestamp = _timestamp(None, "occurred_at")
        with self._transaction():
            task = self.read_task(task_id)
            self._queue_task_and_workpad_locked(
                task,
                project_slug=project_slug,
                timestamp=timestamp,
                event_payload={"identifier": task["identifier"], "project_slug": project_slug},
            )
        return self.read_task(task_id)

    def _queue_task_and_workpad_locked(
        self,
        task: dict[str, object],
        *,
        project_slug: str,
        timestamp: str,
        event_payload: object,
    ) -> None:
        """Apply the shared QUEUED transition while a write transaction is open."""
        if task["project_slug"] != project_slug:
            raise StateConflict("task is not registered to the selected project")
        if task["state"] != "PREPARED":
            raise StateConflict("only PREPARED tasks may be queued")
        changed = self.connection.execute(
            """
            UPDATE tasks SET state = 'QUEUED', updated_at = ?
            WHERE id = ? AND state = 'PREPARED'
            """,
            (timestamp, task["id"]),
        ).rowcount
        if changed != 1:
            raise StateConflict("task state changed before it could be queued")
        self._insert_event(task["id"], "queued", event_payload, occurred_at=timestamp)
        body = "\n".join((
            "<!-- symphony-workpad:v1 -->", "## Symphony Workpad", "",
            f"- Task: {task['identifier']}", f"- Objective: {task['objective']}",
            f"- Base: {task['base_ref']} @ {task['base_sha']}",
            f"- Branch: {task['branch']}", "- Lifecycle state: QUEUED", "",
        ))
        self.connection.execute(
            "INSERT INTO workpads(task_id, body, version, updated_at) VALUES (?, ?, 1, ?)",
            (task["id"], body, timestamp),
        )

    def reserve_storage_capacity(
        self,
        task_id: str | uuid.UUID,
        *,
        project_slug: str,
        domain: object,
        policy: object,
    ) -> dict[str, object]:
        """Durably commit a full-task reservation before quota mutation.

        A reserved row while the task is PREPARED is an admission intent. It
        is deliberately retained across helper failures and process death so
        uncertain privileged filesystem state cannot free capacity early.
        """
        from storage import StorageContractError, StoragePolicy, VerifiedStorageDomain, derive_quota_id

        task_id = _uuid(task_id, "task_id")
        project_slug = _project_slug(project_slug)
        if not isinstance(domain, VerifiedStorageDomain):
            raise StorageContractError("storage admission requires verified host evidence")
        if not isinstance(policy, StoragePolicy):
            raise StorageContractError("storage admission requires a validated profile policy")
        policy.validate()
        if domain.project != project_slug:
            raise StateConflict("storage domain is not registered to the selected project")
        timestamp = _timestamp(None, "occurred_at")
        with self._transaction():
            task = self.read_task(task_id)
            if task["project_slug"] != project_slug:
                raise StateConflict("task is not registered to the selected project")
            if task["state"] != "PREPARED":
                raise StateConflict("only PREPARED tasks may reserve storage")
            proposed = (domain.source, domain.target, domain.fstype, domain.options)
            for row in self.connection.execute(
                "SELECT source, mount_target, fstype, mount_options FROM storage_domains"
            ).fetchall():
                identity = (row["source"], row["mount_target"], row["fstype"], row["mount_options"])
                if identity != proposed:
                    raise StateConflict("shared storage pool identity changed")
            self.connection.execute(
                """
                INSERT INTO storage_domains(
                    project_slug, source, mount_target, fstype, mount_options,
                    pool_bytes, pool_inodes, free_bytes, free_inodes, verified_at, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_slug) DO UPDATE SET
                    source = excluded.source, mount_target = excluded.mount_target,
                    fstype = excluded.fstype, mount_options = excluded.mount_options,
                    pool_bytes = excluded.pool_bytes, pool_inodes = excluded.pool_inodes,
                    free_bytes = excluded.free_bytes, free_inodes = excluded.free_inodes,
                    verified_at = excluded.verified_at, evidence_json = excluded.evidence_json
                """,
                (domain.project, domain.source, domain.target, domain.fstype, domain.options,
                 domain.pool_bytes, domain.pool_inodes, domain.free_bytes, domain.free_inodes,
                 timestamp, domain.evidence_json),
            )
            quota_id = derive_quota_id(str(task["identifier"]))
            existing = self.connection.execute(
                "SELECT * FROM storage_reservations WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing is not None:
                if (existing["status"] != "reserved" or existing["project_slug"] != project_slug or
                        existing["quota_id"] != quota_id or
                        existing["reserved_bytes"] != policy.task_bytes or
                        existing["reserved_inodes"] != policy.task_inodes):
                    raise StateConflict("conflicting storage admission retry")
                return dict(existing)
            reserved = self.connection.execute(
                """
                SELECT COALESCE(SUM(reserved_bytes), 0), COALESCE(SUM(reserved_inodes), 0)
                FROM storage_reservations WHERE status = 'reserved'
                """,
            ).fetchone()
            if domain.pool_bytes - domain.free_bytes > policy.allocatable_pool_bytes:
                raise StateConflict("observed task-usable storage exceeds allocatable pool capacity")
            available_bytes = min(domain.free_bytes, policy.allocatable_pool_bytes - int(reserved[0]))
            available_inodes = min(domain.free_inodes, domain.pool_inodes - int(reserved[1]))
            if (available_bytes - policy.task_bytes < policy.emergency_reserve_bytes or
                    available_inodes - policy.task_inodes < policy.emergency_reserve_inodes):
                raise StateConflict("storage admission would consume the emergency reserve")
            try:
                self.connection.execute(
                    """
                    INSERT INTO storage_reservations(
                        task_id, project_slug, quota_id, reserved_bytes, reserved_inodes,
                        status, created_at, released_at
                    ) VALUES (?, ?, ?, ?, ?, 'reserved', ?, NULL)
                    """,
                    (task_id, project_slug, quota_id, policy.task_bytes, policy.task_inodes, timestamp),
                )
            except sqlite3.IntegrityError as exc:
                raise StateConflict("task or host quota identity already has a storage reservation") from exc
            row = self.connection.execute(
                "SELECT * FROM storage_reservations WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def queue_task_with_storage(
        self,
        task_id: str | uuid.UUID,
        *,
        project_slug: str,
        domain: object,
        policy: object,
        assignment: object,
    ) -> dict[str, object]:
        """Finalize a durable reservation after exact quota proof."""
        from storage import (
            StorageContractError, StoragePolicy, VerifiedStorageDomain,
            derive_quota_id, validate_task_quota_binding,
        )

        task_id = _uuid(task_id, "task_id")
        project_slug = _project_slug(project_slug)
        if not isinstance(domain, VerifiedStorageDomain):
            raise StorageContractError("storage admission requires verified host evidence")
        if not isinstance(policy, StoragePolicy):
            raise StorageContractError("storage admission requires a validated profile policy")
        policy.validate()
        if domain.project != project_slug:
            raise StateConflict("storage domain is not registered to the selected project")
        timestamp = _timestamp(None, "occurred_at")
        with self._transaction():
            task = self.read_task(task_id)
            if task["project_slug"] != project_slug:
                raise StateConflict("task is not registered to the selected project")
            if task["state"] != "PREPARED":
                raise StateConflict("only PREPARED tasks may be queued")
            validate_task_quota_binding(
                assignment, project=project_slug, identifier=str(task["identifier"]), policy=policy,
            )
            quota_id = derive_quota_id(str(task["identifier"]))
            reservation = self.connection.execute(
                "SELECT * FROM storage_reservations WHERE task_id = ?", (task_id,)
            ).fetchone()
            if (reservation is None or reservation["status"] != "reserved" or
                    reservation["project_slug"] != project_slug or
                    reservation["quota_id"] != quota_id or
                    reservation["reserved_bytes"] != policy.task_bytes or
                    reservation["reserved_inodes"] != policy.task_inodes):
                raise StateConflict("exact durable storage reservation is required before QUEUED")
            proposed = (domain.source, domain.target, domain.fstype, domain.options)
            for row in self.connection.execute(
                "SELECT source, mount_target, fstype, mount_options FROM storage_domains"
            ).fetchall():
                identity = (row["source"], row["mount_target"], row["fstype"], row["mount_options"])
                if identity != proposed:
                    raise StateConflict("shared storage pool identity changed")
            self.connection.execute(
                """
                INSERT INTO storage_domains(
                    project_slug, source, mount_target, fstype, mount_options,
                    pool_bytes, pool_inodes, free_bytes, free_inodes, verified_at, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_slug) DO UPDATE SET
                    source = excluded.source, mount_target = excluded.mount_target,
                    fstype = excluded.fstype, mount_options = excluded.mount_options,
                    pool_bytes = excluded.pool_bytes, pool_inodes = excluded.pool_inodes,
                    free_bytes = excluded.free_bytes, free_inodes = excluded.free_inodes,
                    verified_at = excluded.verified_at, evidence_json = excluded.evidence_json
                """,
                (domain.project, domain.source, domain.target, domain.fstype, domain.options,
                 domain.pool_bytes, domain.pool_inodes, domain.free_bytes, domain.free_inodes,
                 timestamp, domain.evidence_json),
            )
            self._queue_task_and_workpad_locked(
                task,
                project_slug=project_slug,
                timestamp=timestamp,
                event_payload={
                    "identifier": task["identifier"], "project_slug": project_slug,
                    "reserved_bytes": policy.task_bytes, "reserved_inodes": policy.task_inodes,
                },
            )
        return self.read_task(task_id)

    def read_storage_domain(self, project_slug: str) -> dict[str, object] | None:
        project_slug = _project_slug(project_slug)
        return _row(self.connection.execute(
            "SELECT * FROM storage_domains WHERE project_slug = ?", (project_slug,)
        ).fetchone())

    def read_storage_pool(self) -> dict[str, object] | None:
        """Read the one shared pool represented by per-project observations."""
        return _row(self.connection.execute(
            "SELECT * FROM storage_domains ORDER BY verified_at DESC LIMIT 1"
        ).fetchone())

    def read_storage_reservation(self, task_id: str | uuid.UUID) -> dict[str, object] | None:
        task_id = _uuid(task_id, "task_id")
        return _row(self.connection.execute(
            "SELECT * FROM storage_reservations WHERE task_id = ?", (task_id,)
        ).fetchone())

    def storage_reservation_totals(self, project_slug: str) -> dict[str, int]:
        """Return global pool reservations and the selected project's share."""
        project_slug = _project_slug(project_slug)
        row = self.connection.execute(
            """
            SELECT COALESCE(SUM(reserved_bytes), 0), COALESCE(SUM(reserved_inodes), 0)
            FROM storage_reservations WHERE status = 'reserved'
            """,
        ).fetchone()
        project_row = self.connection.execute(
            """
            SELECT COALESCE(SUM(reserved_bytes), 0), COALESCE(SUM(reserved_inodes), 0)
            FROM storage_reservations
            WHERE project_slug = ? AND status = 'reserved'
            """, (project_slug,),
        ).fetchone()
        return {
            "reserved_bytes": int(row[0]), "reserved_inodes": int(row[1]),
            "project_reserved_bytes": int(project_row[0]),
            "project_reserved_inodes": int(project_row[1]),
        }

    def release_storage_reservation(
        self, task_id: str | uuid.UUID, *, proof: object,
        released_at: str | None = None,
    ) -> dict[str, object]:
        """Release only after trusted cleanup stops further task growth."""
        from storage import StorageContractError, validate_storage_release_proof

        task_id = _uuid(task_id, "task_id")
        timestamp = _timestamp(released_at, "released_at")
        with self._transaction():
            row = self.connection.execute(
                "SELECT * FROM storage_reservations WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ControlPlaneError(f"storage reservation does not exist: {task_id}")
            if row["status"] == "released":
                return dict(row)
            task = self.read_task(task_id)
            if task["state"] != "PREPARED":
                raise StateConflict(
                    "storage reclamation is authorized only for PREPARED admission recovery; "
                    "post-QUEUED reservations are retained"
                )
            try:
                validate_storage_release_proof(
                    proof, project=str(task["project_slug"]),
                    identifier=str(task["identifier"]),
                )
            except StorageContractError:
                raise
            self.connection.execute(
                "UPDATE storage_reservations SET status = 'released', released_at = ? WHERE task_id = ?",
                (timestamp, task_id),
            )
            row = self.connection.execute(
                "SELECT * FROM storage_reservations WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def read_task_by_identifier(self, identifier: str, *, project_slug: str | None = None) -> dict[str, object]:
        """Read one local task identity, optionally enforcing project ownership."""
        if not isinstance(identifier, str) or not IDENTIFIER_RE.fullmatch(identifier):
            raise ValueError("identifier must match T-000042")
        if project_slug is not None:
            project_slug = _project_slug(project_slug)
            value = _row(self.connection.execute(
                "SELECT * FROM tasks WHERE identifier = ? AND project_slug = ?",
                (identifier, project_slug),
            ).fetchone())
        else:
            value = _row(self.connection.execute(
                "SELECT * FROM tasks WHERE identifier = ?", (identifier,)
            ).fetchone())
        if value is None:
            raise ControlPlaneError(f"task does not exist: {identifier}")
        return value

    def list_tasks(
        self,
        *,
        project_slug: str | None = None,
        states: Sequence[str] = (),
    ) -> list[dict[str, object]]:
        if project_slug is not None:
            project_slug = _project_slug(project_slug)
        states = tuple(_state(value) for value in states)
        clauses: list[str] = []
        parameters: list[object] = []
        if project_slug is not None:
            clauses.append("project_slug = ?")
            parameters.append(project_slug)
        if states:
            clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
            parameters.extend(states)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM tasks{where} ORDER BY identifier", parameters
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_workpad(
        self,
        task_id: str | uuid.UUID,
        body: str,
        *,
        expected_version: int | None = None,
        updated_at: str | None = None,
    ) -> dict[str, object]:
        task_id = _uuid(task_id, "task_id")
        body = _text(body, "workpad body")
        timestamp = _timestamp(updated_at, "updated_at")
        with self._transaction():
            self.read_task(task_id)
            current = self.connection.execute(
                "SELECT version FROM workpads WHERE task_id = ?", (task_id,)
            ).fetchone()
            if current is None:
                if expected_version not in (None, 0):
                    raise StateConflict("workpad does not exist at the expected version")
                version = 1
                self.connection.execute(
                    "INSERT INTO workpads(task_id, body, version, updated_at) VALUES (?, ?, ?, ?)",
                    (task_id, body, version, timestamp),
                )
            else:
                current_version = int(current[0])
                if expected_version is not None and expected_version != current_version:
                    raise StateConflict("workpad version does not match the expected version")
                version = current_version + 1
                self.connection.execute(
                    "UPDATE workpads SET body = ?, version = ?, updated_at = ? WHERE task_id = ?",
                    (body, version, timestamp, task_id),
                )
        return dict(self.connection.execute("SELECT * FROM workpads WHERE task_id = ?", (task_id,)).fetchone())

    def read_workpad(self, task_id: str | uuid.UUID) -> dict[str, object] | None:
        task_id = _uuid(task_id, "task_id")
        return _row(self.connection.execute("SELECT * FROM workpads WHERE task_id = ?", (task_id,)).fetchone())

    def create_lifecycle(
        self,
        task_id: str | uuid.UUID,
        *,
        lifecycle_id: str | uuid.UUID | None = None,
        started_at: str | None = None,
    ) -> dict[str, object]:
        """Create the next lifecycle; only human disposition may reopen a task."""
        task_id = _uuid(task_id, "task_id")
        lifecycle_id = _uuid(lifecycle_id, "lifecycle_id")
        timestamp = _timestamp(started_at, "started_at")
        with self._transaction():
            task = self.read_task(task_id)
            if task["state"] not in {"QUEUED", "TERMINATED"}:
                raise StateConflict("a lifecycle may start only for a queued or human-disposed task")
            previous = self.connection.execute(
                "SELECT state, terminal_outcome FROM lifecycles WHERE task_id = ? ORDER BY ordinal DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if previous is not None and previous["state"] != "NON_CONVERGED":
                raise StateConflict("a new lifecycle requires a non-converged lifecycle and human disposition")
            if previous is not None and not self.connection.execute(
                "SELECT 1 FROM human_dispositions WHERE task_id = ? AND lifecycle_id = ? AND decision = 'START_ANOTHER_LIFECYCLE' LIMIT 1",
                (task_id, self.connection.execute("SELECT id FROM lifecycles WHERE task_id = ? ORDER BY ordinal DESC LIMIT 1", (task_id,)).fetchone()[0]),
            ).fetchone():
                raise StateConflict("a new lifecycle requires explicit human disposition")
            ordinal = int(self.connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM lifecycles WHERE task_id = ?", (task_id,)
            ).fetchone()[0])
            self.connection.execute(
                "INSERT INTO lifecycles(id, task_id, ordinal, state, terminal_outcome, started_at, ended_at) VALUES (?, ?, ?, 'RUNNING', NULL, ?, NULL)",
                (lifecycle_id, task_id, ordinal, timestamp),
            )
            self.connection.execute("UPDATE tasks SET state = 'ACTIVE', updated_at = ? WHERE id = ?", (timestamp, task_id))
            self._insert_event(task_id, "lifecycle_started", {"lifecycle_id": lifecycle_id, "ordinal": ordinal}, occurred_at=timestamp)
        return dict(self.connection.execute("SELECT * FROM lifecycles WHERE id = ?", (lifecycle_id,)).fetchone())

    def start_working_round(self, lifecycle_id: str | uuid.UUID, *, started_at: str | None = None) -> dict[str, object]:
        lifecycle_id = _uuid(lifecycle_id, "lifecycle_id")
        timestamp = _timestamp(started_at, "started_at")
        with self._transaction():
            lifecycle = _row(self.connection.execute("SELECT * FROM lifecycles WHERE id = ?", (lifecycle_id,)).fetchone())
            if lifecycle is None or lifecycle["state"] != "RUNNING":
                raise StateConflict("working round requires a running lifecycle")
            ordinal = int(self.connection.execute("SELECT COALESCE(MAX(ordinal), 0) + 1 FROM working_rounds WHERE lifecycle_id = ?", (lifecycle_id,)).fetchone()[0])
            if ordinal > 8:
                raise StateConflict("lifecycle working-round budget is exhausted")
            round_id = str(uuid.uuid4())
            self.connection.execute("INSERT INTO working_rounds(id, task_id, lifecycle_id, ordinal, state, started_at, ended_at) VALUES (?, ?, ?, ?, 'PLANNING', ?, NULL)", (round_id, lifecycle["task_id"], lifecycle_id, ordinal, timestamp))
            self._insert_event(str(lifecycle["task_id"]), "working_round_started", {"lifecycle_id": lifecycle_id, "working_round_id": round_id, "ordinal": ordinal}, occurred_at=timestamp)
        return dict(self.connection.execute("SELECT * FROM working_rounds WHERE id = ?", (round_id,)).fetchone())

    def terminate_working_round_non_converged(
        self, working_round_id: str | uuid.UUID, *, ended_at: str | None = None,
    ) -> dict[str, object]:
        """Close a planning round that exhausted its bounded attempts."""
        working_round_id = _uuid(working_round_id, "working_round_id")
        timestamp = _timestamp(ended_at, "ended_at")
        with self._transaction():
            working = _row(self.connection.execute("SELECT * FROM working_rounds WHERE id = ?", (working_round_id,)).fetchone())
            if working is None or working["state"] != "PLANNING":
                raise StateConflict("only a planning round may terminate non-converged")
            self.connection.execute(
                "UPDATE working_rounds SET state = 'NON_CONVERGED', ended_at = ? WHERE id = ?",
                (timestamp, working_round_id),
            )
            self._insert_event(
                str(working["task_id"]), "working_round_non_converged",
                {"lifecycle_id": working["lifecycle_id"], "working_round_id": working_round_id},
                occurred_at=timestamp,
            )
        return dict(self.connection.execute("SELECT * FROM working_rounds WHERE id = ?", (working_round_id,)).fetchone())

    def start_planning_attempt(self, working_round_id: str | uuid.UUID, *, started_at: str | None = None) -> dict[str, object]:
        working_round_id = _uuid(working_round_id, "working_round_id")
        timestamp = _timestamp(started_at, "started_at")
        with self._transaction():
            current = _row(self.connection.execute("SELECT * FROM working_rounds WHERE id = ?", (working_round_id,)).fetchone())
            if current is None or current["state"] != "PLANNING":
                raise StateConflict("planning attempt requires a planning working round")
            ordinal = int(self.connection.execute("SELECT COALESCE(MAX(ordinal), 0) + 1 FROM planning_attempts WHERE working_round_id = ?", (working_round_id,)).fetchone()[0])
            if ordinal > 3:
                raise StateConflict("planning-attempt budget is exhausted")
            attempt_id = str(uuid.uuid4())
            self.connection.execute("INSERT INTO planning_attempts(id, task_id, lifecycle_id, working_round_id, ordinal, state, started_at, ended_at) VALUES (?, ?, ?, ?, ?, 'OPEN', ?, NULL)", (attempt_id, current["task_id"], current["lifecycle_id"], working_round_id, ordinal, timestamp))
        return dict(self.connection.execute("SELECT * FROM planning_attempts WHERE id = ?", (attempt_id,)).fetchone())

    def _record_mechanical_validation(
        self,
        lifecycle_id: str | uuid.UUID,
        *,
        head_sha: str,
        passed: bool,
        evidence: dict[str, object],
        validated_at: str | None = None,
    ) -> dict[str, object]:
        """Persist the result computed by ``perform_mechanical_validation``."""
        lifecycle_id = _uuid(lifecycle_id, "lifecycle_id")
        head_sha = _sha(head_sha, "head_sha", required=True)
        if not isinstance(passed, bool):
            raise ValueError("mechanical validation result must be boolean")
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("mechanical validation evidence must be a non-empty object")
        timestamp = _timestamp(validated_at, "validated_at")
        validation_status = "PASSED" if passed else "FAILED"
        with self._transaction():
            lifecycle = _row(self.connection.execute("SELECT * FROM lifecycles WHERE id = ?", (lifecycle_id,)).fetchone())
            if lifecycle is None or lifecycle["state"] != "RUNNING":
                raise StateConflict("mechanical validation requires a running lifecycle")
            if lifecycle["convergence_status"] != "RECORDED" or lifecycle["convergence_head"] != head_sha:
                raise StateConflict("mechanical validation requires the exact recorded convergence head")
            task = self.read_task(str(lifecycle["task_id"]))
            if task["current_head"] != head_sha:
                raise StateConflict("mechanical validation head differs from the authoritative task head")
            existing = lifecycle["mechanical_validation_status"]
            if existing == "PASSED":
                if not passed or lifecycle["mechanical_validation_head"] != head_sha:
                    raise StateConflict("a passed mechanical validation cannot be rewritten")
                return lifecycle
            self.connection.execute(
                "UPDATE lifecycles SET mechanical_validation_status = ?, mechanical_validation_head = ?, "
                "mechanical_validation_at = ?, mechanical_validation_evidence_json = ?, "
                "mechanical_acceptance = ?, mechanical_acceptance_head = ?, mechanical_acceptance_at = ? "
                "WHERE id = ?",
                (validation_status, head_sha, timestamp, _payload(evidence),
                 "ACCEPTED" if passed else "NOT_REACHED", head_sha if passed else None,
                 timestamp if passed else None, lifecycle_id),
            )
            self._insert_event(
                str(lifecycle["task_id"]),
                "mechanical_validation_passed" if passed else "mechanical_validation_failed",
                {"lifecycle_id": lifecycle_id, "head_sha": head_sha, "evidence": evidence},
                occurred_at=timestamp,
            )
            lifecycle = _row(self.connection.execute("SELECT * FROM lifecycles WHERE id = ?", (lifecycle_id,)).fetchone())
        assert lifecycle is not None
        return lifecycle

    def authorize_dispatch(
        self,
        task_id: str | uuid.UUID,
        *,
        lifecycle_id: str | uuid.UUID,
        working_round_id: str | uuid.UUID | None,
        planning_attempt_id: str | uuid.UUID | None,
        role: str,
        expected_starting_head: str,
        read_scopes: Sequence[str],
        write_scopes: Sequence[str],
        registered_artifact_scopes: Sequence[str] = (),
        protected_artifact_scopes: Sequence[str] = (),
        implementation_roots: Sequence[str] = (),
        created_at: str | None = None,
    ) -> dict[str, object]:
        """Issue one exact grant; Runtime cannot add scope or choose a role."""
        task_id, lifecycle_id = _uuid(task_id, "task_id"), _uuid(lifecycle_id, "lifecycle_id")
        working_round_id = _uuid(working_round_id, "working_round_id") if working_round_id else None
        planning_attempt_id = _uuid(planning_attempt_id, "planning_attempt_id") if planning_attempt_id else None
        if role not in ROLE_NAMES:
            raise ValueError(f"role is not supported: {role}")
        expected_starting_head = _sha(expected_starting_head, "expected_starting_head", required=True)
        read_scopes = tuple(_text(str(path), "read scope") for path in read_scopes)
        write_scopes = tuple(_text(str(path), "write scope") for path in write_scopes)
        registered_artifact_scopes = tuple(_text(str(path), "registered artifact scope") for path in registered_artifact_scopes)
        protected_artifact_scopes = tuple(_text(str(path), "protected artifact scope") for path in protected_artifact_scopes)
        implementation_roots = tuple(_scope(path, "implementation root") for path in implementation_roots)
        if role in {"PROJECT-MANAGER", "REVIEWER", "ADVERSARY"} and write_scopes:
            raise StateConflict(f"{role} is non-writing")
        if role in {"PLANNER", "IMPLEMENTER", "ARCHIVIST"} and not write_scopes:
            raise StateConflict(f"{role} requires an explicit bounded writer grant")
        if any(path == ".git" or path.startswith(".git/") for path in (*read_scopes, *write_scopes)):
            raise StateConflict("role grants may not include Git metadata")
        if role in {"PLANNER", "ARCHIVIST"} and not registered_artifact_scopes:
            raise StateConflict(f"{role} requires registered project harness-artifact scopes")
        if role in {"PLANNER", "ARCHIVIST"} and any(
            not any(path == root or path.startswith(root + "/") for root in registered_artifact_scopes)
            for path in write_scopes
        ):
            raise StateConflict(f"{role} grant exceeds registered project harness-artifact scopes")
        if role == "IMPLEMENTER" and not implementation_roots:
            raise StateConflict("IMPLEMENTER requires registered implementation roots")
        if role == "IMPLEMENTER" and any(
            not any(path == root or path.startswith(root + "/") for root in implementation_roots)
            for path in write_scopes
        ):
            raise StateConflict("IMPLEMENTER grant exceeds registered implementation roots")
        if role == "IMPLEMENTER" and any(
            any(path == root or path.startswith(root + "/") for root in protected_artifact_scopes)
            for path in write_scopes
        ):
            raise StateConflict("IMPLEMENTER grant overlaps a registered Planner or Archivist artifact scope")
        timestamp = _timestamp(created_at, "created_at")
        dispatch_id, grant_id = str(uuid.uuid4()), str(uuid.uuid4())
        with self._transaction():
            self.read_task(task_id)
            self.connection.execute("INSERT INTO role_dispatches(id, task_id, lifecycle_id, working_round_id, planning_attempt_id, role, status, expected_starting_head, created_at, consumed_at) VALUES (?, ?, ?, ?, ?, ?, 'AUTHORIZED', ?, ?, NULL)", (dispatch_id, task_id, lifecycle_id, working_round_id, planning_attempt_id, role, expected_starting_head, timestamp))
            self.connection.execute("INSERT INTO capability_grants(id, task_id, dispatch_id, role, read_scopes_json, write_scopes_json, issued_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (grant_id, task_id, dispatch_id, role, _payload(list(read_scopes)), _payload(list(write_scopes)), timestamp))
            self._insert_event(task_id, "dispatch_authorized", {"dispatch_id": dispatch_id, "grant_id": grant_id, "role": role}, occurred_at=timestamp)
        return {"dispatch": dict(self.connection.execute("SELECT * FROM role_dispatches WHERE id = ?", (dispatch_id,)).fetchone()), "grant": dict(self.connection.execute("SELECT * FROM capability_grants WHERE id = ?", (grant_id,)).fetchone())}

    def record_execution_started(
        self,
        dispatch_id: str | uuid.UUID,
        evidence: dict[str, object],
        *,
        role_run_id: str | uuid.UUID | None = None,
    ) -> dict[str, object]:
        """Materialize an active role run only from retained launch evidence."""
        dispatch_id = _uuid(dispatch_id, "dispatch_id")
        role_run_id = _uuid(role_run_id, "role_run_id")
        if not isinstance(evidence, dict) or evidence.get("status") != "running" or not evidence.get("runtime_execution_id"):
            raise StateConflict("retained running launch evidence is required")
        started = _timestamp(str(evidence.get("started_at")), "started_at")
        evidence_id = str(uuid.uuid4())
        with self._transaction():
            dispatch = _row(self.connection.execute("SELECT * FROM role_dispatches WHERE id = ?", (dispatch_id,)).fetchone())
            if dispatch is None or dispatch["status"] != "AUTHORIZED":
                raise StateConflict("dispatch is not authorized or has already started")
            observed_role = evidence.get("observed_role")
            if (observed_role != dispatch["role"] or evidence.get("task_id") != dispatch["task_id"] or
                    evidence.get("dispatch_id") != dispatch_id or
                    evidence.get("starting_head") != dispatch["expected_starting_head"]):
                raise StateConflict("launch evidence is bound to a different role, task, or dispatch")
            round_row = self.connection.execute("SELECT ordinal FROM working_rounds WHERE id = ?", (dispatch["working_round_id"],)).fetchone()
            grant_row = self.connection.execute("SELECT id FROM capability_grants WHERE dispatch_id = ?", (dispatch_id,)).fetchone()
            if grant_row is None:
                raise StateConflict("dispatch has no capability grant")
            self.connection.execute(
                "INSERT INTO role_runs(id, task_id, role, working_round_number, lifecycle_id, working_round_id, planning_attempt_id, dispatch_id, execution_evidence_id, capability_grant_id, head_sha, status, started_at, finished_at, result_summary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, NULL, NULL)",
                (role_run_id, dispatch["task_id"], dispatch["role"], int(round_row[0]) if round_row else 1,
                 dispatch["lifecycle_id"], dispatch["working_round_id"], dispatch["planning_attempt_id"],
                 dispatch_id, evidence_id, grant_row[0], _sha(str(evidence.get("head_sha")), "head_sha") if evidence.get("head_sha") else None, started),
            )
            self.connection.execute(
                "INSERT INTO execution_evidence(id, task_id, dispatch_id, role_run_id, runtime_execution_id, observed_role, status, started_at, finished_at, receipt_json) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, NULL, ?)",
                (evidence_id, dispatch["task_id"], dispatch_id, role_run_id, str(evidence["runtime_execution_id"]), observed_role, started, _payload(evidence)),
            )
            self.connection.execute("UPDATE role_dispatches SET status = 'RUNNING' WHERE id = ?", (dispatch_id,))
            self._insert_event(str(dispatch["task_id"]), "execution_started", {"dispatch_id": dispatch_id, "role_run_id": role_run_id, "evidence_id": evidence_id, "role": observed_role}, role_run_id=role_run_id, occurred_at=started)
        return self.read_role_run(role_run_id)

    def record_execution_termination(
        self,
        dispatch_id: str | uuid.UUID,
        evidence: dict[str, object],
    ) -> dict[str, object]:
        """Complete the same role run that Pilot previously observed starting."""
        dispatch_id = _uuid(dispatch_id, "dispatch_id")
        if not isinstance(evidence, dict) or evidence.get("status") not in {"finished", "failed", "blocked"}:
            raise StateConflict("retained terminal execution evidence is required")
        finished = _timestamp(str(evidence.get("finished_at")), "finished_at")
        with self._transaction():
            dispatch = _row(self.connection.execute("SELECT * FROM role_dispatches WHERE id = ?", (dispatch_id,)).fetchone())
            if dispatch is None or dispatch["status"] != "RUNNING":
                raise StateConflict("terminal evidence has no active Pilot execution")
            retained = _row(self.connection.execute("SELECT * FROM execution_evidence WHERE dispatch_id = ? AND status = 'running'", (dispatch_id,)).fetchone())
            if retained is None:
                raise StateConflict("terminal evidence has no retained launch evidence")
            try:
                launch_evidence = json.loads(str(retained["receipt_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StateConflict("retained launch evidence is malformed") from exc
            if (evidence.get("runtime_execution_id") != retained["runtime_execution_id"] or
                    evidence.get("task_id") != dispatch["task_id"] or
                    evidence.get("dispatch_id") != dispatch_id or
                    evidence.get("observed_role") != dispatch["role"] or
                    evidence.get("starting_head") != dispatch["expected_starting_head"] or
                    evidence.get("started_at") != launch_evidence.get("started_at")):
                raise StateConflict("terminal evidence is not bound to the retained launch")
            self.connection.execute(
                "UPDATE execution_evidence SET status = ?, finished_at = ?, receipt_json = ? WHERE id = ?",
                (evidence["status"], finished, _payload(evidence), retained["id"]),
            )
            self.connection.execute(
                "UPDATE role_runs SET status = ?, finished_at = ?, head_sha = ?, result_summary = ? WHERE id = ?",
                (evidence["status"], finished, _sha(str(evidence.get("head_sha")), "head_sha", required=True), str(evidence.get("summary", ""))[:12000], retained["role_run_id"]),
            )
            self.connection.execute("UPDATE role_dispatches SET status = 'CONSUMED', consumed_at = ? WHERE id = ?", (finished, dispatch_id))
            self._insert_event(str(dispatch["task_id"]), "execution_terminated", {"dispatch_id": dispatch_id, "role_run_id": retained["role_run_id"], "evidence_id": retained["id"], "role": dispatch["role"], "status": evidence["status"]}, role_run_id=retained["role_run_id"], occurred_at=finished)
        return self.read_role_run(str(retained["role_run_id"]))

    def record_orphaned_execution(
        self,
        dispatch_id: str | uuid.UUID,
        *,
        terminated_at: str | None = None,
    ) -> dict[str, object]:
        """Terminalize a retained launch after managed Runtime stop proof."""
        dispatch_id = _uuid(dispatch_id, "dispatch_id")
        finished = _timestamp(terminated_at, "terminated_at")
        with self._transaction():
            dispatch = _row(self.connection.execute(
                "SELECT * FROM role_dispatches WHERE id = ?", (dispatch_id,)
            ).fetchone())
            if dispatch is None or dispatch["status"] != "RUNNING":
                raise StateConflict("orphan reconciliation requires a running dispatch")
            retained = _row(self.connection.execute(
                "SELECT * FROM execution_evidence WHERE dispatch_id = ? AND status = 'running'",
                (dispatch_id,),
            ).fetchone())
            if retained is None:
                raise StateConflict("running dispatch has no retained launch evidence")
            try:
                evidence = json.loads(str(retained["receipt_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StateConflict("retained launch evidence is malformed") from exc
            if not isinstance(evidence, dict):
                raise StateConflict("retained launch evidence is not an object")
            evidence = dict(evidence)
            evidence.update({
                "phase": "terminated", "status": "failed", "finished_at": finished,
                "orphaned": True,
                "summary": "managed Runtime stopped before terminal execution evidence",
            })
            self.connection.execute(
                "UPDATE execution_evidence SET status = 'failed', finished_at = ?, receipt_json = ? WHERE id = ?",
                (finished, _payload(evidence), retained["id"]),
            )
            self.connection.execute(
                "UPDATE role_runs SET status = 'failed', finished_at = ?, result_summary = ? WHERE id = ?",
                (finished, evidence["summary"], retained["role_run_id"]),
            )
            self.connection.execute(
                "UPDATE role_dispatches SET status = 'REJECTED', consumed_at = ? WHERE id = ?",
                (finished, dispatch_id),
            )
            self._insert_event(
                str(dispatch["task_id"]), "execution_terminated",
                {"dispatch_id": dispatch_id, "role_run_id": retained["role_run_id"], "evidence_id": retained["id"], "role": dispatch["role"], "status": "failed", "orphaned": True},
                role_run_id=retained["role_run_id"], occurred_at=finished,
            )
            self._insert_event(
                str(dispatch["task_id"]), "execution_reconciled",
                {"dispatch_id": dispatch_id, "role_run_id": retained["role_run_id"], "orphaned": True},
                role_run_id=retained["role_run_id"], occurred_at=finished,
            )
        return self.read_role_run(str(retained["role_run_id"]))

    def read_role_run(self, run_id: str | uuid.UUID) -> dict[str, object]:
        run_id = _uuid(run_id, "role_run_id")
        value = _row(self.connection.execute("SELECT * FROM role_runs WHERE id = ?", (run_id,)).fetchone())
        if value is None:
            raise ControlPlaneError(f"role run does not exist: {run_id}")
        return value

    def record_writer_delta(
        self,
        role_run_id: str | uuid.UUID,
        *,
        changed_paths: Sequence[str],
        workspace_head: str,
        authorization_status: str,
        dirty: bool,
        commit_sha: str | None = None,
        observed_at: str | None = None,
    ) -> dict[str, object]:
        """Record the broker's exact path decision for any authorized writer."""
        role_run_id = _uuid(role_run_id, "role_run_id")
        workspace_head = _sha(workspace_head, "workspace_head", required=True)
        commit_sha = _sha(commit_sha, "commit_sha")
        if authorization_status not in {"ACCEPTED", "REJECTED"}:
            raise ValueError("writer delta authorization status is invalid")
        paths = [str(path) for path in changed_paths]
        if any(not path for path in paths):
            raise StateConflict("writer delta contains an empty path")
        if authorization_status == "ACCEPTED" and paths and not commit_sha:
            raise StateConflict("accepted writer delta requires the resulting host commit")
        timestamp = _timestamp(observed_at, "observed_at")
        delta_id = str(uuid.uuid4())
        with self._transaction():
            run = self.read_role_run(role_run_id)
            if run["role"] not in {"PLANNER", "IMPLEMENTER", "ARCHIVIST"}:
                raise StateConflict("only canonical writer roles may record writer deltas")
            self.connection.execute("INSERT INTO writer_deltas(id, task_id, role_run_id, capability_grant_id, changed_paths_json, authorization_status, workspace_head, commit_sha, dirty, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (delta_id, run["task_id"], role_run_id, run["capability_grant_id"], _payload(paths), authorization_status, workspace_head, commit_sha, int(bool(dirty)), timestamp))
            self.connection.execute("UPDATE role_runs SET head_sha = ? WHERE id = ?", (commit_sha or workspace_head, role_run_id))
            self._insert_event(str(run["task_id"]), "writer_delta_validated", {"role_run_id": role_run_id, "delta_id": delta_id, "authorization_status": authorization_status, "changed_paths": paths}, role_run_id=role_run_id, occurred_at=timestamp)
            if authorization_status == "ACCEPTED":
                self._insert_event(str(run["task_id"]), "host_commit_created", {"role_run_id": role_run_id, "delta_id": delta_id, "commit_sha": commit_sha}, role_run_id=role_run_id, occurred_at=timestamp)
        return dict(self.connection.execute("SELECT * FROM writer_deltas WHERE id = ?", (delta_id,)).fetchone())

    def record_human_disposition(self, lifecycle_id: str | uuid.UUID, *, decision: str, detail: str, created_at: str | None = None) -> dict[str, object]:
        lifecycle_id = _uuid(lifecycle_id, "lifecycle_id")
        if decision not in {"START_ANOTHER_LIFECYCLE", "HOLD", "CLOSE_TASK"}:
            raise ValueError("human disposition is invalid")
        detail = _text(detail, "human disposition detail")
        timestamp = _timestamp(created_at, "created_at")
        disposition_id = str(uuid.uuid4())
        with self._transaction():
            lifecycle = _row(self.connection.execute("SELECT * FROM lifecycles WHERE id = ?", (lifecycle_id,)).fetchone())
            if lifecycle is None or lifecycle["state"] != "NON_CONVERGED":
                raise StateConflict("human disposition requires a non-converged lifecycle")
            self.connection.execute("INSERT INTO human_dispositions(id, task_id, lifecycle_id, decision, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)", (disposition_id, lifecycle["task_id"], lifecycle_id, decision, detail, timestamp))
            if decision == "CLOSE_TASK":
                self.connection.execute("UPDATE tasks SET state = 'TERMINATED', updated_at = ? WHERE id = ?", (timestamp, lifecycle["task_id"]))
            self._insert_event(str(lifecycle["task_id"]), "human_disposition_recorded", {"lifecycle_id": lifecycle_id, "decision": decision}, occurred_at=timestamp)
        return dict(self.connection.execute("SELECT * FROM human_dispositions WHERE id = ?", (disposition_id,)).fetchone())

    def record_finding(
        self,
        *,
        task_id: str | uuid.UUID,
        role_run_id: str | uuid.UUID,
        kind: str,
        severity: str,
        body: str,
        status: str = "open",
        finding_id: str | uuid.UUID | None = None,
    ) -> dict[str, object]:
        task_id = _uuid(task_id, "task_id")
        role_run_id = _uuid(role_run_id, "role_run_id")
        finding_id = _uuid(finding_id, "finding_id")
        kind = _text(kind, "finding kind")
        body = _text(body, "finding body")
        if severity not in FINDING_SEVERITIES:
            raise ValueError("finding severity is invalid")
        if status not in FINDING_STATUSES:
            raise ValueError("finding status is invalid")
        with self._transaction():
            self.read_task(task_id)
            self.connection.execute(
                """
                INSERT INTO findings(
                    id, task_id, role_run_id, kind, severity, body, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (finding_id, task_id, role_run_id, kind, severity, body, status),
            )
            self._insert_event(
                task_id, "finding_recorded",
                {"finding_id": finding_id, "role_run_id": role_run_id, "status": status},
                role_run_id=role_run_id,
            )
        return self.read_finding(finding_id)

    def read_finding(self, finding_id: str | uuid.UUID) -> dict[str, object]:
        finding_id = _uuid(finding_id, "finding_id")
        value = _row(self.connection.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone())
        if value is None:
            raise ControlPlaneError(f"finding does not exist: {finding_id}")
        return value

    def record_blocker(
        self,
        *,
        task_id: str | uuid.UUID,
        kind: str,
        body: str,
        blocker_id: str | uuid.UUID | None = None,
        created_at: str | None = None,
    ) -> dict[str, object]:
        task_id = _uuid(task_id, "task_id")
        blocker_id = _uuid(blocker_id, "blocker_id")
        body = _text(body, "blocker body")
        if kind not in BLOCKER_KINDS:
            raise ValueError("blocker kind is invalid")
        timestamp = _timestamp(created_at, "created_at")
        with self._transaction():
            self.read_task(task_id)
            self.connection.execute(
                """
                INSERT INTO blockers(id, task_id, kind, body, status, created_at, resolved_at)
                VALUES (?, ?, ?, ?, 'open', ?, NULL)
                """,
                (blocker_id, task_id, kind, body, timestamp),
            )
            # Step 2 persists project blockers without inventing a scheduler
            # state or falsely recording them as human intervention.
            self._insert_event(
                task_id,
                "blocker_recorded",
                {"blocker_id": blocker_id, "kind": kind},
                occurred_at=timestamp,
            )
        return self.read_blocker(blocker_id)

    def read_blocker(self, blocker_id: str | uuid.UUID) -> dict[str, object]:
        blocker_id = _uuid(blocker_id, "blocker_id")
        value = _row(self.connection.execute("SELECT * FROM blockers WHERE id = ?", (blocker_id,)).fetchone())
        if value is None:
            raise ControlPlaneError(f"blocker does not exist: {blocker_id}")
        return value

    def resolve_blocker(self, blocker_id: str | uuid.UUID, *, resolved_at: str | None = None) -> dict[str, object]:
        blocker_id = _uuid(blocker_id, "blocker_id")
        timestamp = _timestamp(resolved_at, "resolved_at")
        with self._transaction():
            current = self.read_blocker(blocker_id)
            if current["status"] != "open":
                raise StateConflict("blocker is already resolved")
            self.connection.execute(
                "UPDATE blockers SET status = 'resolved', resolved_at = ? WHERE id = ? AND status = 'open'",
                (timestamp, blocker_id),
            )
            self._insert_event(str(current["task_id"]), "blocker_resolved", {"blocker_id": blocker_id}, occurred_at=timestamp)
        return self.read_blocker(blocker_id)

    def record_publication(
        self,
        *,
        task_id: str | uuid.UUID,
        publication_status: str,
        head_sha: str | None = None,
        remote_branch: str | None = None,
        github_pr_number: int | None = None,
        published_at: str | None = None,
    ) -> dict[str, object]:
        task_id = _uuid(task_id, "task_id")
        if publication_status not in PUBLICATION_STATUSES:
            raise ValueError("publication status is invalid")
        head_sha = _sha(head_sha, "head_sha")
        if remote_branch is not None:
            remote_branch = _text(remote_branch, "remote_branch")
        if github_pr_number is not None and (not isinstance(github_pr_number, int) or github_pr_number < 1):
            raise ValueError("github_pr_number must be positive")
        if publication_status == "published" and published_at is None:
            raise ValueError("published publication requires published_at")
        timestamp = _timestamp(published_at, "published_at") if published_at is not None else None
        with self._transaction():
            task = self.read_task(task_id)
            if publication_status == "published":
                if head_sha is None:
                    raise ValueError("published publication requires head_sha")
                if task["current_head"] != head_sha:
                    raise StateConflict(
                        "published publication head must equal the task current_head"
                    )
            self.connection.execute(
                """
                INSERT INTO publications(
                    task_id, head_sha, remote_branch, github_pr_number, publication_status, published_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    head_sha = excluded.head_sha,
                    remote_branch = excluded.remote_branch,
                    github_pr_number = excluded.github_pr_number,
                    publication_status = excluded.publication_status,
                    published_at = excluded.published_at
                """,
                (task_id, head_sha, remote_branch, github_pr_number, publication_status, timestamp),
            )
            if publication_status == "published":
                self.connection.execute(
                    "UPDATE tasks SET published_head = ?, updated_at = ? WHERE id = ?",
                    (head_sha, timestamp, task_id),
                )
            if publication_status in {"started", "published"}:
                self._insert_event(
                    task_id,
                    "publication_started" if publication_status == "started" else "publication_finished",
                    {"head_sha": head_sha, "github_pr_number": github_pr_number},
                )
        return self.read_publication(task_id)

    def read_publication(self, task_id: str | uuid.UUID) -> dict[str, object] | None:
        task_id = _uuid(task_id, "task_id")
        return _row(self.connection.execute("SELECT * FROM publications WHERE task_id = ?", (task_id,)).fetchone())

    def start_publication(
        self,
        task_id: str | uuid.UUID,
        *,
        head_sha: str,
        remote_branch: str,
        started_at: str | None = None,
    ) -> dict[str, object]:
        """Persist the exact publication intent before external mutation."""
        task_id = _uuid(task_id, "task_id")
        head_sha = _sha(head_sha, "head_sha")
        remote_branch = _text(remote_branch, "remote_branch")
        timestamp = _timestamp(started_at, "started_at")
        result: dict[str, object] | None = None
        with self._transaction():
            task = self.read_task(task_id)
            lifecycle = self.connection.execute("SELECT * FROM lifecycles WHERE task_id = ? ORDER BY ordinal DESC LIMIT 1", (task_id,)).fetchone()
            if lifecycle is None or lifecycle["state"] != "ACCEPTED" or lifecycle["mechanical_acceptance"] != "ACCEPTED" or lifecycle["mechanical_acceptance_head"] != head_sha or task["current_head"] != head_sha:
                raise StateConflict("publication requires the exact accepted lifecycle head")
            if self.connection.execute(
                "SELECT 1 FROM blockers WHERE task_id = ? AND status = 'open' LIMIT 1", (task_id,)
            ).fetchone():
                raise StateConflict("publication requires no open blocker")
            if not self.connection.execute(
                "SELECT 1 FROM role_runs WHERE task_id = ? AND role = 'ARCHIVIST' "
                "AND status = 'finished' AND head_sha = ? AND execution_evidence_id IS NOT NULL LIMIT 1",
                (task_id, head_sha),
            ).fetchone():
                raise StateConflict("publication requires archival closeout for the exact current HEAD")
            current = self.read_publication(task_id)
            if current and current["publication_status"] == "published":
                raise StateConflict("publication is already finalized")
            if current and current["publication_status"] == "started":
                if current["head_sha"] != head_sha or current["remote_branch"] != remote_branch:
                    raise StateConflict("started publication intent does not match the exact task identity")
                result = current
            else:
                self.connection.execute(
                    """
                    INSERT INTO publications(task_id, head_sha, remote_branch, github_pr_number,
                                             publication_status, published_at)
                    VALUES (?, ?, ?, NULL, 'started', NULL)
                    ON CONFLICT(task_id) DO UPDATE SET
                        head_sha = excluded.head_sha,
                        remote_branch = excluded.remote_branch,
                        github_pr_number = publications.github_pr_number,
                        publication_status = 'started',
                        published_at = NULL
                    """,
                    (task_id, head_sha, remote_branch),
                )
                self._insert_event(
                    task_id, "publication_started",
                    {"head_sha": head_sha, "remote_branch": remote_branch},
                    occurred_at=timestamp,
                )
                result = dict(self.connection.execute(
                    "SELECT * FROM publications WHERE task_id = ?", (task_id,)
                ).fetchone())
        assert result is not None
        return result

    def finalize_publication(
        self,
        task_id: str | uuid.UUID,
        *,
        head_sha: str,
        remote_branch: str,
        github_pr_number: int,
        evidence: dict[str, object],
        published_at: str | None = None,
    ) -> dict[str, object]:
        """Atomically publish and move final acceptance to READY_FOR_HUMAN_MERGE."""
        task_id = _uuid(task_id, "task_id")
        head_sha = _sha(head_sha, "head_sha")
        remote_branch = _text(remote_branch, "remote_branch")
        if not isinstance(github_pr_number, int) or isinstance(github_pr_number, bool) or github_pr_number < 1:
            raise ValueError("github_pr_number must be positive")
        if not isinstance(evidence, dict):
            raise ValueError("publication evidence must be an object")
        timestamp = _timestamp(published_at, "published_at")
        result: dict[str, dict[str, object]] | None = None
        with self._transaction():
            task = self.read_task(task_id)
            lifecycle = self.connection.execute("SELECT * FROM lifecycles WHERE task_id = ? ORDER BY ordinal DESC LIMIT 1", (task_id,)).fetchone()
            if lifecycle is None or lifecycle["state"] != "ACCEPTED" or lifecycle["mechanical_acceptance"] != "ACCEPTED" or lifecycle["mechanical_acceptance_head"] != head_sha or task["current_head"] != head_sha:
                raise StateConflict("publication finalization requires the exact accepted lifecycle head")
            if self.connection.execute(
                "SELECT 1 FROM blockers WHERE task_id = ? AND status = 'open' LIMIT 1", (task_id,)
            ).fetchone():
                raise StateConflict("publication finalization requires no open blocker")
            intent = self.read_publication(task_id)
            if not intent or intent["publication_status"] != "started" or intent["head_sha"] != head_sha or intent["remote_branch"] != remote_branch:
                raise StateConflict("matching started publication intent is missing")
            self.connection.execute(
                "UPDATE publications SET head_sha = ?, remote_branch = ?, github_pr_number = ?, "
                "publication_status = 'published', published_at = ? WHERE task_id = ? AND publication_status = 'started'",
                (head_sha, remote_branch, github_pr_number, timestamp, task_id),
            )
            self.connection.execute(
                "UPDATE tasks SET published_head = ?, updated_at = ? WHERE id = ?",
                (head_sha, timestamp, task_id),
            )
            payload = dict(evidence)
            payload.update({"head": head_sha, "branch": remote_branch, "pr_number": github_pr_number})
            self._insert_event(task_id, "publication_finished", payload, occurred_at=timestamp)
            changed = self.connection.execute(
                "UPDATE tasks SET state = 'READY_FOR_HUMAN_MERGE', updated_at = ? "
                "WHERE id = ? AND state IN ('ACTIVE', 'TERMINATED')",
                (timestamp, task_id),
            ).rowcount
            if changed != 1:
                raise StateConflict("accepted lifecycle state changed during publication finalization")
            self._insert_event(
                task_id, "ready_for_human_merge", payload, occurred_at=timestamp,
            )
            result = {
                "task": dict(self.connection.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()),
                "publication": dict(self.connection.execute(
                    "SELECT * FROM publications WHERE task_id = ?", (task_id,)
                ).fetchone()),
            }
        assert result is not None
        return result

    def fail_publication(
        self,
        task_id: str | uuid.UUID,
        *,
        detail: str,
        head_sha: str | None = None,
        remote_branch: str | None = None,
        github_pr_number: int | None = None,
    ) -> dict[str, object]:
        """Record a publication failure without deleting external recovery evidence."""
        task_id = _uuid(task_id, "task_id")
        detail = _text(detail, "publication failure detail")
        head_sha = _sha(head_sha, "head_sha")
        if remote_branch is not None:
            remote_branch = _text(remote_branch, "remote_branch")
        if github_pr_number is not None and (not isinstance(github_pr_number, int) or isinstance(github_pr_number, bool) or github_pr_number < 1):
            raise ValueError("github_pr_number must be positive")
        timestamp = _timestamp(None, "created_at")
        result: dict[str, object] | None = None
        with self._transaction():
            task = self.read_task(task_id)
            current = self.read_publication(task_id)
            if task["state"] == "READY_FOR_HUMAN_MERGE" or task["published_head"] is not None:
                raise StateConflict("publication failure cannot alter a finalized task")
            if current and current["publication_status"] == "published":
                raise StateConflict("publication failure cannot downgrade published state")
            retained_head = head_sha or (current["head_sha"] if current else None)
            retained_branch = remote_branch or (current["remote_branch"] if current else None)
            retained_pr = github_pr_number or (current["github_pr_number"] if current else None)
            self.connection.execute(
                """
                INSERT INTO publications(task_id, head_sha, remote_branch, github_pr_number,
                                         publication_status, published_at)
                VALUES (?, ?, ?, ?, 'failed', NULL)
                ON CONFLICT(task_id) DO UPDATE SET
                    head_sha = excluded.head_sha,
                    remote_branch = excluded.remote_branch,
                    github_pr_number = excluded.github_pr_number,
                    publication_status = 'failed',
                    published_at = NULL
                """,
                (task_id, retained_head, retained_branch, retained_pr),
            )
            blocker_id = str(uuid.uuid4())
            self.connection.execute(
                "INSERT INTO blockers(id, task_id, kind, body, status, created_at, resolved_at) "
                "VALUES (?, ?, 'infrastructure', ?, 'open', ?, NULL)",
                (blocker_id, task_id, detail, timestamp),
            )
            self._insert_event(
                task_id, "blocker_recorded",
                {"blocker_id": blocker_id, "kind": "infrastructure"},
                occurred_at=timestamp,
            )
            result = dict(self.connection.execute(
                "SELECT * FROM publications WHERE task_id = ?", (task_id,)
            ).fetchone())
        assert result is not None
        return result

    def update_heads(
        self,
        task_id: str | uuid.UUID,
        *,
        current_head: str | None | object = _UNSET,
        published_head: str | None | object = _UNSET,
        updated_at: str | None = None,
    ) -> dict[str, object]:
        task_id = _uuid(task_id, "task_id")
        with self._transaction():
            current = self.read_task(task_id)
            if published_head is not _UNSET:
                requested_published_head = _sha(published_head, "published_head")
                if requested_published_head != current["published_head"]:
                    raise StateConflict(
                        "published_head is managed by a successful publication; "
                        "update_heads cannot clear or rewrite it"
                    )
            next_current_head = (
                current["current_head"] if current_head is _UNSET else _sha(current_head, "current_head")
            )
            next_published_head = current["published_head"]
            if current["current_head"] == next_current_head and current["published_head"] == next_published_head:
                return current
            timestamp = _timestamp(updated_at, "updated_at")
            self.connection.execute(
                "UPDATE tasks SET current_head = ?, published_head = ?, updated_at = ? WHERE id = ?",
                (next_current_head, next_published_head, timestamp, task_id),
            )
            self._insert_event(
                task_id,
                "head_changed",
                {"current_head": next_current_head, "published_head": next_published_head},
                occurred_at=timestamp,
            )
        return self.read_task(task_id)

    def transition_task(
        self,
        task_id: str | uuid.UUID,
        *,
        expected_state: str,
        new_state: str,
        event_type: str,
        payload: object | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, object]:
        """Atomically update current state and append its audit event.

        The accepted architecture supplies the finite vocabulary and lifecycle
        stages, but does not yet license a complete scheduler transition graph.
        Callers therefore provide the expected current state explicitly; this
        method enforces compare-and-set plus event atomicity without guessing
        later scheduler semantics.
        """
        task_id = _uuid(task_id, "task_id")
        expected_state = _state(expected_state)
        new_state = _state(new_state)
        if event_type not in EVENT_TYPES:
            raise ValueError(f"event type is not supported: {event_type}")
        timestamp = _timestamp(occurred_at, "occurred_at")
        with self._transaction():
            changed = self.connection.execute(
                """
                UPDATE tasks SET state = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (new_state, timestamp, task_id, expected_state),
            ).rowcount
            if changed != 1:
                exists = self.connection.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if exists is None:
                    raise ControlPlaneError(f"task does not exist: {task_id}")
                raise StateConflict(
                    f"task state is not {expected_state}; transition was not applied"
                )
            self._insert_event(task_id, event_type, payload, occurred_at=timestamp)
        return self.read_task(task_id)

    def record_event(
        self,
        task_id: str | uuid.UUID,
        event_type: str,
        payload: object | None = None,
        *,
        role_run_id: str | uuid.UUID | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, object]:
        task_id = _uuid(task_id, "task_id")
        role_run_id = _uuid(role_run_id, "role_run_id") if role_run_id is not None else None
        timestamp = _timestamp(occurred_at, "occurred_at")
        with self._transaction():
            self.read_task(task_id)
            event_id = self._insert_event(
                task_id, event_type, payload, role_run_id=role_run_id, occurred_at=timestamp,
            )
        return dict(self.connection.execute("SELECT * FROM task_events WHERE id = ?", (event_id,)).fetchone())

    def list_events(self, task_id: str | uuid.UUID) -> list[dict[str, object]]:
        task_id = _uuid(task_id, "task_id")
        rows = self.connection.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY occurred_at, id", (task_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def _insert_event(
        self,
        task_id: str,
        event_type: str,
        payload: object | None,
        *,
        role_run_id: str | None = None,
        occurred_at: str | None = None,
    ) -> str:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"event type is not supported: {event_type}")
        event_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO task_events(id, task_id, event_type, role_run_id, payload_json, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, task_id, event_type, role_run_id, _payload(payload), _timestamp(occurred_at, "occurred_at")),
        )
        return event_id

    def read_projection(self, task_id: str | uuid.UUID) -> dict[str, object]:
        task_id = _uuid(task_id, "task_id")
        lifecycle = self.connection.execute(
            "SELECT * FROM lifecycles WHERE task_id = ? ORDER BY ordinal DESC LIMIT 1", (task_id,)
        ).fetchone()
        pending_dispatch = self.connection.execute(
            "SELECT * FROM role_dispatches WHERE task_id = ? AND status = 'AUTHORIZED' ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,)
        ).fetchone()
        active_execution = self.connection.execute(
            "SELECT * FROM role_runs WHERE task_id = ? AND status = 'running' ORDER BY started_at DESC, id DESC LIMIT 1", (task_id,)
        ).fetchone()
        return {
            "task": self.read_task(task_id),
            "lifecycle": _row(lifecycle),
            "expected_next_role": pending_dispatch["role"] if pending_dispatch else None,
            "expected_dispatch": _row(pending_dispatch),
            "active_execution": _row(active_execution),
            "lifecycles": [dict(row) for row in self.connection.execute("SELECT * FROM lifecycles WHERE task_id = ? ORDER BY ordinal", (task_id,)).fetchall()],
            "working_rounds": [dict(row) for row in self.connection.execute("SELECT * FROM working_rounds WHERE task_id = ? ORDER BY lifecycle_id, ordinal", (task_id,)).fetchall()],
            "planning_attempts": [dict(row) for row in self.connection.execute("SELECT * FROM planning_attempts WHERE task_id = ? ORDER BY lifecycle_id, working_round_id, ordinal", (task_id,)).fetchall()],
            "dispatches": [dict(row) for row in self.connection.execute("SELECT * FROM role_dispatches WHERE task_id = ? ORDER BY created_at, id", (task_id,)).fetchall()],
            "capability_grants": [dict(row) for row in self.connection.execute("SELECT * FROM capability_grants WHERE task_id = ? ORDER BY issued_at, id", (task_id,)).fetchall()],
            "workpad": self.read_workpad(task_id),
            "role_runs": [
                dict(row) for row in self.connection.execute(
                    "SELECT * FROM role_runs WHERE task_id = ? ORDER BY started_at, id", (task_id,)
                ).fetchall()
            ],
            "execution_evidence": [dict(row) for row in self.connection.execute("SELECT * FROM execution_evidence WHERE task_id = ? ORDER BY finished_at, id", (task_id,)).fetchall()],
            "writer_deltas": [dict(row) for row in self.connection.execute("SELECT * FROM writer_deltas WHERE task_id = ? ORDER BY observed_at, id", (task_id,)).fetchall()],
            "human_dispositions": [dict(row) for row in self.connection.execute("SELECT * FROM human_dispositions WHERE task_id = ? ORDER BY created_at, id", (task_id,)).fetchall()],
            "findings": [
                dict(row) for row in self.connection.execute(
                    "SELECT * FROM findings WHERE task_id = ? ORDER BY rowid", (task_id,)
                ).fetchall()
            ],
            "blockers": [
                dict(row) for row in self.connection.execute(
                    "SELECT * FROM blockers WHERE task_id = ? ORDER BY created_at, id", (task_id,)
                ).fetchall()
            ],
            "publication": self.read_publication(task_id),
            "storage": self.read_storage_reservation(task_id),
            "events": self.list_events(task_id),
        }

    def backup_to(self, destination: pathlib.Path | str, *, overwrite: bool = False) -> pathlib.Path:
        """Create an atomic coherent snapshot through SQLite's backup API."""
        destination = _absolute_path(destination)
        if destination == self.path:
            raise ControlPlaneError("backup destination cannot be the live control database")
        if destination.exists() and destination.is_symlink():
            raise ControlPlaneError("backup destination must not be a symlink")
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.chmod(0o700)
        fd, temporary_name = tempfile.mkstemp(prefix=".control-backup-", dir=destination.parent)
        os.close(fd)
        temporary = pathlib.Path(temporary_name)
        try:
            connection = _connect_raw(temporary)
            try:
                self.connection.backup(connection)
                _validate_schema(connection)
            finally:
                connection.close()
            temporary.chmod(0o600)
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def restore_from(
        backup: pathlib.Path | str,
        destination: pathlib.Path | str,
        *,
        replace: bool = False,
    ) -> pathlib.Path:
        """Validate a backup, copy it via SQLite backup, then explicitly replace state."""
        backup = _absolute_path(backup)
        destination = _absolute_path(destination)
        if backup == destination:
            raise ControlPlaneError("restore source and destination must differ")
        if destination in _OPEN_DATABASE_PATHS:
            raise ControlPlaneError("restore requires the destination control database to be offline")
        if not backup.is_file() or backup.is_symlink():
            raise ControlPlaneError("restore source is not a regular database file")
        if destination.is_symlink():
            raise ControlPlaneError("restore destination must not be a symlink")
        if destination.exists() and not replace:
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.chmod(0o700)
        fd, temporary_name = tempfile.mkstemp(prefix=".control-restore-", dir=destination.parent)
        os.close(fd)
        temporary = pathlib.Path(temporary_name)
        source = _connect_readonly(backup)
        try:
            _validate_schema(source)
            target = _connect_raw(temporary)
            try:
                source.backup(target)
                _validate_schema(target)
            finally:
                target.close()
            temporary.chmod(0o600)
            os.replace(temporary, destination)
            return destination
        finally:
            source.close()
            temporary.unlink(missing_ok=True)


def open_database(path: pathlib.Path | str | None = None) -> ControlPlaneDatabase:
    """Open or create the host control database at the accepted schema."""
    return ControlPlaneDatabase.open(path)


def open_database_readonly(path: pathlib.Path | str | None = None) -> ControlPlaneDatabase:
    """Open an existing current database through SQLite's read-only mode."""
    return ControlPlaneDatabase.open_readonly(path)


def inspect_schema_version(path: pathlib.Path | str) -> int:
    """Read the stored SQLite schema version without applying migrations."""
    database_path = _absolute_path(path)
    if not database_path.exists():
        return 0
    connection = _connect_readonly(database_path)
    try:
        return _schema_version(connection)
    finally:
        connection.close()
