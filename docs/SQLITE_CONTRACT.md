# SQLite control-plane contract

Step 2 establishes the host-owned persistence contract that the later runtime
tracker adapter will consume read-only. It does not switch scheduler polling,
workspace admission, or publication from GitHub to SQLite.

## Authority and location

The database is derived from the WSL/Linux operator home:

```text
~/.local/state/symphony-pilot/control.sqlite3
```

`runtime/control_db.py` owns creation, migration, validation, and writes. The
database is host-side state. It is not mounted into a contained task domain,
and the browser and contained agent are not authorities for its contents.

`projects/<slug>/profile.toml`, validated by the existing project registry,
remains the only project registration/configuration authority. Database rows
store `project_slug` as a semantic reference; there is deliberately no
SQLite `projects` table and no SQLite project registry.

The existing GitHub issue, label, comment, task-record, and JSONL-shaped
mechanisms are historical/current integration seams until a later cutover.
This step does not migrate or delete them and adds no compatibility path.

## Baseline inventory

The schema was derived from the existing pilot seams with their authority
kept distinct:

| Existing seam | Step 2 classification |
|---|---|
| `projects/<slug>/profile.toml` and `project_registry.py` | Current project-registration authority; stays outside SQLite |
| `task_admission.py` task records and server-derived GitHub facts | Current host admission identity; GitHub issue number, labels, and comments are later-cutover authority, not the new local task identity |
| `workflow/architect_policy.md` and role-policy files | Accepted lifecycle and role semantics; generated policy payload, not durable state |
| Workpad comments, task outboxes, task JSON, logs, and process markers | Current integration/transient or generated state; not copied wholesale into relational tables |
| `broker.py` blockers and publication/draft-PR handling | Accepted blocker/publication concepts with GitHub-specific mechanisms deferred to later cutover |
| Runtime locks, containment probes, credential ordering, and sterile publication boundaries | PR #4 security/runtime invariants; this module does not alter them |

The genuinely new Step 2 choices are a host-owned local UUID plus deterministic
`T-000001`-style identifier, finite database state vocabulary, current-state
tables with foreign keys, and structured event history. These choices establish
the later read-only runtime contract; they do not claim that the scheduler has
already been cut over.

## Schema version and safety

The schema is version `1`, stored in SQLite `PRAGMA user_version`, with the
deterministic migration identity `control-plane-v1` in `schema_migrations`.
An absent database is created and migrated transactionally. A newer version,
partial migration, missing table/column, unexpected table, or invalid
migration history fails closed. Reopening an accepted database is idempotent.

Every connection explicitly enables:

```text
PRAGMA foreign_keys = ON
PRAGMA journal_mode = WAL
PRAGMA busy_timeout = 5000
```

The database file is mode `0600` and its containing state directory is mode
`0700` where the host filesystem supports those permissions.

## Relational domain

| Table | Authority represented | Important constraints |
|---|---|---|
| `tasks` | Local task identity and lifecycle projection | Canonical UUID primary key; unique `T-000000` identifier; finite state vocabulary; validated SHA/ref fields |
| `workpads` | One current workpad per task | `task_id` primary key and foreign key; monotonically increasing version |
| `role_runs` | Architect and specialized lifecycle rounds | finite roles/statuses; unique `(task_id, role, round)`; finished status requires `finished_at` |
| `findings` | Reviewer/adversary findings and adjudication | task and role-run foreign keys are paired, preventing cross-task provenance |
| `blockers` | Human/project/infrastructure blockers | finite kind/status; open/resolved timestamp consistency |
| `publications` | Optional downstream GitHub publication state | one optional row per task; GitHub PR number is nullable; published status requires a head and timestamp |
| `task_events` | Structured lifecycle history | task foreign key; finite event vocabulary; optional role-run provenance paired to the task |

The accepted task state vocabulary is:

```text
PREPARED
QUEUED
PLANNED
IMPLEMENTED
REVIEW
ADVERSARIAL_REVIEW
FINAL_MECHANICAL_ACCEPTANCE
ARCHIVIST
READY_FOR_HUMAN_MERGE
HUMAN_BLOCKED
INFRASTRUCTURE_BLOCKED
```

The database constrains the vocabulary but does not invent a complete future
scheduler transition graph. Callers supply an expected current state to the
compare-and-set transition primitive. The `ARCHITECT` role is represented in
`role_runs` and event provenance; the specialized worker role set remains
`PROJECT-MANAGER`, `PLANNER`, `IMPLEMENTER`, `REVIEWER`, `ADVERSARY`, and
`ARCHIVIST`.

Database-enforceable identity, uniqueness, foreign-key, shape, and timestamp
relationships belong to SQLite. Project-slug resolution, permission to
perform a lifecycle transition, role adjudication, and scheduler policy remain
host application/orchestrator authority. The later runtime adapter must treat
this schema as a read-only cross-repository contract, not this Python API.

Task deletion is not exposed as a normal operation. The initial task event and
other audit/history foreign keys use `RESTRICT`, so a task with lifecycle
history cannot be deleted accidentally; its current workpad uses `CASCADE`
only because it is a replaceable projection of the retained task.

## Host API

The intentionally small `ControlPlaneDatabase` API supports:

- `open`/`open_database`, schema-version inspection, and idempotent initialization;
- task creation with transactionally allocated `T-000001`-style identifiers,
  reads, and project/state filtering;
- one-current-workpad upsert with optimistic version checks;
- role-run creation and completion;
- finding, blocker, publication, and structured event recording;
- task head updates and compare-and-set state transitions that append the
  corresponding event in the same transaction;
- a current lifecycle projection assembled from relational current state and
  historical events.

`task_events` is audit/history evidence, not an event-sourced replacement for
the current relational projection. Agent stdout remains payload and is not
event authority.

## Backup and restore

`ControlPlaneDatabase.backup_to()` uses SQLite's online backup API to make a
coherent snapshot, validates the snapshot, and atomically publishes it to the
requested destination. `restore_from()` validates the source, copies it with
the same SQLite backup mechanism into a temporary sibling, validates the
temporary database, and replaces the destination only when the caller passes
the explicit `replace=True` flag. A live-file copy is not the backup contract.

## Deferred boundaries

This contract does not add `tracker.kind = sqlite`, runtime database reads,
GitHub-to-SQLite migration, GH-N identity changes, browser endpoints, inbox or
outbox routing, project-registry duplication, or App Server authentication
changes. Those are later seams.
