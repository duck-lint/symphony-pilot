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
partial migration, missing table/column, unexpected persistent object, or invalid
migration history fails closed. Reopening an accepted database is idempotent.

Every writable connection explicitly enables:

```text
PRAGMA foreign_keys = ON
PRAGMA journal_mode = WAL
PRAGMA busy_timeout = 5000
```

The database file is mode `0600` and its containing state directory is mode
`0700` where the host filesystem supports those permissions.

Schema validation is stronger than migration-history validation. On every open,
backup validation, and restore validation, pilot checks the persistent object
set, table columns, the deterministic physical-schema signature generated from
the checked-in migration, expected index names/columns/uniqueness, foreign-key
definitions (including paired composite provenance keys), migration rows, and
foreign-key enforcement. It also runs SQLite `PRAGMA integrity_check` and
`PRAGMA foreign_key_check`. Persistent views, triggers, indexes, or tables not
in the v1 contract fail closed even if `user_version` and `schema_migrations`
still claim v1.

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

### HEAD and publication authority

`tasks.current_head` is the latest host-accepted task HEAD. `tasks.published_head`
is the last successful publication HEAD and is a projection maintained by the
successful `record_publication()` operation; it is not independently writable
through `update_heads()`. `publications.head_sha` is the HEAD named by the
current/last publication record. When a publication is marked `published`, its
HEAD must equal `tasks.current_head`, and that publication row plus
`tasks.published_head` plus `publication_finished` are committed in one
transaction. An in-progress or failed publication may name a candidate HEAD
without changing the last successful `published_head`.

Changing `current_head` does not rewrite the last successful `published_head`.
Omitting a HEAD argument preserves its current value; explicit `None` clears
`current_head`, while `published_head` cannot be cleared or rewritten through
`update_heads()` because publication is its authority. A genuine no-op does not
change `updated_at` or append `head_changed`.

The database prevents these cross-table contradictions at the persistence
operation boundary. Authorization for whether a HEAD is ready to publish,
whether review/adversary/final validation has been satisfied, and whether
publication is permissible remains host/orchestrator policy.

### Blocker event semantics

`blockers.kind` preserves the three distinct values `human`, `project`, and
`infrastructure`. v1 records `human_blocked` only for a human blocker and
`infrastructure_blocked` only for an infrastructure blocker. A project blocker
is persisted without a blocked task-state/event mapping because Step 2 does not
license a `PROJECT_BLOCKED` scheduler state or a truthful existing event type.
The later lifecycle step must define that mapping explicitly; project blockers
must not be relabeled as human blockers.

## Backup and restore

`ControlPlaneDatabase.backup_to()` uses SQLite's online backup API to make a
coherent snapshot, validates the snapshot, and atomically publishes it to the
requested destination. `restore_from()` requires the destination to be offline:
pilot tracks open control-plane handles and refuses replacement while any such
handle is live. The caller must explicitly pass `replace=True`; after the
offline precondition is met, restore opens the backup read-only, validates it,
copies it with SQLite's backup API into a temporary sibling, validates the
temporary database, and atomically replaces the destination. A live-file copy
or an informal “remember to close it first” convention is not the contract.

There was no durable non-test `~/.local/state/symphony-pilot/control.sqlite3`
in the inspected WSL host state root when this correction was made. Therefore
the physical-schema and authority corrections modify schema v1 in place; no
compatibility migration was added for disposable pre-acceptance test state.

## Deferred boundaries

This contract does not add `tracker.kind = sqlite`, runtime database reads,
GitHub-to-SQLite migration, GH-N identity changes, browser endpoints, inbox or
outbox routing, project-registry duplication, or App Server authentication
changes. Those are later seams.
