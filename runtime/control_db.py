#!/usr/bin/env python3
"""Host-owned SQLite contract for local Symphony task lifecycle state.

This module defines persistence authority only. Project registration remains in
``projects/<slug>/profile.toml``; SQLite stores the task and lifecycle state
that refers to that externally registered slug. The runtime and browser do not
open this database in Step 2.
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
CURRENT_SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5_000
MIGRATION_ID = "control-plane-v1"

TASK_STATES = frozenset({
    "PREPARED",
    "QUEUED",
    "PLANNED",
    "IMPLEMENTED",
    "REVIEW",
    "ADVERSARIAL_REVIEW",
    "FINAL_MECHANICAL_ACCEPTANCE",
    "ARCHIVIST",
    "READY_FOR_HUMAN_MERGE",
    "HUMAN_BLOCKED",
    "INFRASTRUCTURE_BLOCKED",
})
ROLE_NAMES = frozenset({
    "ARCHITECT",
    "PROJECT-MANAGER",
    "PLANNER",
    "IMPLEMENTER",
    "REVIEWER",
    "ADVERSARY",
    "ARCHIVIST",
})
ROLE_RUN_STATUSES = frozenset({"started", "finished", "failed", "blocked"})
FINDING_SEVERITIES = frozenset({"info", "low", "medium", "high", "critical"})
FINDING_STATUSES = frozenset({"open", "accepted", "rejected", "licensed", "resolved"})
BLOCKER_KINDS = frozenset({"human", "project", "infrastructure"})
BLOCKER_STATUSES = frozenset({"open", "resolved"})
PUBLICATION_STATUSES = frozenset({"not_started", "started", "published", "failed"})
EVENT_TYPES = frozenset({
    "task_created",
    "queued",
    "architect_started",
    "role_started",
    "role_finished",
    "finding_recorded",
    "correction_licensed",
    "head_changed",
    "review_accepted",
    "adversary_accepted",
    "validation_passed",
    "publication_started",
    "publication_finished",
    "human_blocked",
    "infrastructure_blocked",
    "ready_for_human_merge",
})

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
IDENTIFIER_RE = re.compile(r"^T-[0-9]{6}$")
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


def _project_slug(value: str) -> str:
    if not isinstance(value, str) or not PROJECT_SLUG_RE.fullmatch(value):
        raise ValueError("project_slug is invalid")
    return value


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
                    'PREPARED', 'QUEUED', 'PLANNED', 'IMPLEMENTED', 'REVIEW',
                    'ADVERSARIAL_REVIEW', 'FINAL_MECHANICAL_ACCEPTANCE',
                    'ARCHIVIST', 'READY_FOR_HUMAN_MERGE', 'HUMAN_BLOCKED',
                    'INFRASTRUCTURE_BLOCKED'
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
                    'ARCHITECT', 'PROJECT-MANAGER', 'PLANNER', 'IMPLEMENTER',
                    'REVIEWER', 'ADVERSARY', 'ARCHIVIST'
                )),
                round INTEGER NOT NULL CHECK (round >= 1),
                head_sha TEXT CHECK (
                    head_sha IS NULL OR
                    (length(head_sha) = 40 AND head_sha NOT GLOB '*[^0-9a-f]*')
                ),
                status TEXT NOT NULL CHECK (status IN ('started', 'finished', 'failed', 'blocked')),
                started_at TEXT NOT NULL CHECK (length(started_at) > 0),
                finished_at TEXT,
                result_summary TEXT,
                UNIQUE (id, task_id),
                UNIQUE (task_id, role, round),
                CHECK (
                    (status = 'started' AND finished_at IS NULL) OR
                    (status <> 'started' AND finished_at IS NOT NULL)
                ),
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
                status TEXT NOT NULL CHECK (status IN ('open', 'accepted', 'rejected', 'licensed', 'resolved')),
                licensed_correction_round INTEGER CHECK (
                    licensed_correction_round IS NULL OR licensed_correction_round >= 1
                ),
                CHECK (
                    (status = 'licensed') = (licensed_correction_round IS NOT NULL)
                ),
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
                    'task_created', 'queued', 'architect_started', 'role_started',
                    'role_finished', 'finding_recorded', 'correction_licensed',
                    'head_changed', 'review_accepted', 'adversary_accepted',
                    'validation_passed', 'publication_started',
                    'publication_finished', 'human_blocked',
                    'infrastructure_blocked', 'ready_for_human_merge'
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
            "CREATE INDEX role_runs_task_idx ON role_runs(task_id, role, round)",
            "CREATE INDEX findings_task_status_idx ON findings(task_id, status)",
            "CREATE INDEX blockers_task_status_idx ON blockers(task_id, status)",
            "CREATE INDEX task_events_task_time_idx ON task_events(task_id, occurred_at, id)",
            "CREATE INDEX task_events_type_idx ON task_events(event_type)",
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
        "id", "task_id", "role", "round", "head_sha", "status", "started_at", "finished_at",
        "result_summary",
    },
    "findings": {
        "id", "task_id", "role_run_id", "kind", "severity", "body", "status",
        "licensed_correction_round",
    },
    "blockers": {"id", "task_id", "kind", "body", "status", "created_at", "resolved_at"},
    "publications": {"task_id", "head_sha", "remote_branch", "github_pr_number", "publication_status", "published_at"},
    "task_events": {"id", "task_id", "event_type", "role_run_id", "payload_json", "occurred_at"},
}

EXPECTED_INDEXES = {
    "tasks_project_state_idx": ("tasks", False, ("project_slug", "state")),
    "tasks_updated_idx": ("tasks", False, ("updated_at",)),
    "role_runs_task_idx": ("role_runs", False, ("task_id", "role", "round")),
    "findings_task_status_idx": ("findings", False, ("task_id", "status")),
    "blockers_task_status_idx": ("blockers", False, ("task_id", "status")),
    "task_events_task_time_idx": ("task_events", False, ("task_id", "occurred_at", "id")),
    "task_events_type_idx": ("task_events", False, ("event_type",)),
}

EXPECTED_UNIQUE_INDEX_COLUMNS = {
    # INTEGER PRIMARY KEY is rowid-backed and therefore has no index entry.
    "schema_migrations": {("identity",)},
    "tasks": {("id",), ("identifier",)},
    "workpads": {("task_id",)},
    "role_runs": {("id",), ("id", "task_id"), ("task_id", "role", "round")},
    "findings": {("id",)},
    "blockers": {("id",)},
    "publications": {("task_id",)},
    "task_events": {("id",)},
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
    if [(row[0], row[1]) for row in migration_rows] != [(1, MIGRATION_ID)]:
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

    integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
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
        branch: str,
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
        branch = _text(branch, "branch")
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
        with self._transaction():
            identifier = identifier or self._allocate_identifier()
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

    def create_role_run(
        self,
        task_id: str | uuid.UUID,
        role: str,
        round: int,
        *,
        head_sha: str | None = None,
        run_id: str | uuid.UUID | None = None,
        started_at: str | None = None,
    ) -> dict[str, object]:
        task_id = _uuid(task_id, "task_id")
        run_id = _uuid(run_id, "role_run_id")
        if role not in ROLE_NAMES:
            raise ValueError(f"role is not supported: {role}")
        if not isinstance(round, int) or isinstance(round, bool) or round < 1:
            raise ValueError("role round must be a positive integer")
        head_sha = _sha(head_sha, "head_sha")
        timestamp = _timestamp(started_at, "started_at")
        with self._transaction():
            self.read_task(task_id)
            self.connection.execute(
                """
                INSERT INTO role_runs(id, task_id, role, round, head_sha, status, started_at, finished_at, result_summary)
                VALUES (?, ?, ?, ?, ?, 'started', ?, NULL, NULL)
                """,
                (run_id, task_id, role, round, head_sha, timestamp),
            )
            self._insert_event(
                task_id, "role_started", {"role": role, "round": round},
                role_run_id=run_id, occurred_at=timestamp,
            )
        return self.read_role_run(run_id)

    def read_role_run(self, run_id: str | uuid.UUID) -> dict[str, object]:
        run_id = _uuid(run_id, "role_run_id")
        value = _row(self.connection.execute("SELECT * FROM role_runs WHERE id = ?", (run_id,)).fetchone())
        if value is None:
            raise ControlPlaneError(f"role run does not exist: {run_id}")
        return value

    def finish_role_run(
        self,
        run_id: str | uuid.UUID,
        *,
        status: str = "finished",
        result_summary: str | None = None,
        head_sha: str | None = None,
        finished_at: str | None = None,
    ) -> dict[str, object]:
        run_id = _uuid(run_id, "role_run_id")
        if status not in ROLE_RUN_STATUSES - {"started"}:
            raise ValueError("finished role-run status is invalid")
        if result_summary is not None:
            result_summary = _text(result_summary, "result_summary")
        head_sha = _sha(head_sha, "head_sha")
        timestamp = _timestamp(finished_at, "finished_at")
        with self._transaction():
            current = self.read_role_run(run_id)
            if current["status"] != "started":
                raise StateConflict("role run is already finished")
            task_id = str(current["task_id"])
            self.connection.execute(
                """
                UPDATE role_runs
                SET status = ?, finished_at = ?, result_summary = ?, head_sha = COALESCE(?, head_sha)
                WHERE id = ? AND status = 'started'
                """,
                (status, timestamp, result_summary, head_sha, run_id),
            )
            self._insert_event(
                task_id, "role_finished",
                {"role": current["role"], "round": current["round"], "status": status},
                role_run_id=run_id, occurred_at=timestamp,
            )
        return self.read_role_run(run_id)

    def record_finding(
        self,
        *,
        task_id: str | uuid.UUID,
        role_run_id: str | uuid.UUID,
        kind: str,
        severity: str,
        body: str,
        status: str = "open",
        licensed_correction_round: int | None = None,
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
        if licensed_correction_round is not None and licensed_correction_round < 1:
            raise ValueError("licensed correction round must be positive")
        with self._transaction():
            self.read_task(task_id)
            self.connection.execute(
                """
                INSERT INTO findings(
                    id, task_id, role_run_id, kind, severity, body, status, licensed_correction_round
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (finding_id, task_id, role_run_id, kind, severity, body, status, licensed_correction_round),
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
            event_type = {
                "human": "human_blocked",
                "infrastructure": "infrastructure_blocked",
            }.get(kind)
            if event_type is not None:
                self._insert_event(
                    task_id,
                    event_type,
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
        return {
            "task": self.read_task(task_id),
            "workpad": self.read_workpad(task_id),
            "role_runs": [
                dict(row) for row in self.connection.execute(
                    "SELECT * FROM role_runs WHERE task_id = ? ORDER BY round, role", (task_id,)
                ).fetchall()
            ],
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
