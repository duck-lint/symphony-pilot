# SQLite control-plane contract

Step 2 established the host-owned persistence contract. Step 5 established
that SQLite determines what work exists. Step 6 makes SQLite determine what
has happened to that work: lifecycle state, current HEAD, workpad version,
role rounds, findings, adjudications, blockers, and exact-head acceptance
evidence.

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

GitHub issues, labels, comments, and draft PRs are not lifecycle authority.
The contained task reports one strict lifecycle result; only trusted Pilot
host code reconciles it into SQLite.

## Baseline inventory

The schema was derived from the existing pilot seams with their authority
kept distinct:

| Existing seam | Step 2 classification |
|---|---|
| `projects/<slug>/profile.toml` and `project_registry.py` | Current project-registration authority; stays outside SQLite |
| `task_admission.py` task records and server-derived GitHub facts | Retired scheduler admission seam; retained only as non-deployed legacy source pending lifecycle/publication cleanup |
| `workflow/architect_policy.md` and role-policy files | Accepted lifecycle and role semantics; generated policy payload, not durable state |
| Workpad comments, task outboxes, task JSON, logs, and process markers | Current integration/transient or generated state; not copied wholesale into relational tables |
| `runtime/lifecycle.py` and the rendered hooks | Step-6 host lifecycle broker; no GitHub lifecycle API |
| Runtime locks, containment probes, credential ordering, and sterile publication boundaries | PR #4 security/runtime invariants; this module does not alter them |

The genuinely new Step 2 choices are a host-owned local UUID plus deterministic
`T-000001`-style identifier, finite database state vocabulary, current-state
tables with foreign keys, and structured event history. These choices establish
the read-only Runtime contract consumed by the Step-5 scheduler.

## Schema version and safety

The schema is version `2`, stored in SQLite `PRAGMA user_version`, with the
deterministic migration identities `control-plane-v1` and
`control-plane-v2-storage-reservations` in `schema_migrations`.
An absent database is created and migrated transactionally. A newer version,
partial migration, missing table/column, unexpected persistent object, or invalid
migration history fails closed. Reopening an accepted database is idempotent.
Version 2 adds the host-owned verified shared-pool snapshot and per-task
full-capacity reservation ledger. `storage_pool_bytes` is the nominal fixed
backing capacity; `storage_allocatable_pool_bytes` is the deliberately lower
filesystem-usable admission ceiling. A reservation is admission accounting
only; the task cannot queue unless the fixed Linux capability has already
supplied both a task-specific identity/limit binding and proof of
kernel-enforced byte and inode hard limits.

The initial canary policy is a nominal 64-GiB backing domain with a 63-GiB
allocatable filesystem ceiling. The one-GiB difference is explicit policy
headroom for ext4 metadata and is not permission to expand the backing domain.
The fixed adapter's task admission operation calls only the reviewed,
root-owned, setuid-root capability-specific quota helper at
`/usr/libexec/symphony-pilot/quota-admit-task`; its opened object, containing
parents, and root-owned identity sidecar are checked before execution. Absence,
replacement, or unsafe identity fails closed. The source and one-time
installation recipe are `provisioning/quota-admit-task.c` and
`scripts/provision_storage_domain.sh`; normal Pilot, Runtime, and model
execution never receive privilege.

The helper binds `fsx_projid` and preserves existing xflags while enabling
`FS_XFLAG_PROJINHERIT` on the exact task root; it verifies a descendant inherits
the same project ID. Kernel limits use the generic Linux
`Q_GETQUOTA`/`Q_SETQUOTA` calls with `PRJQUOTA` and `struct dqblk`, addressed
through the opened pool filesystem object. The provisioning recipe formats the
dedicated 64-GiB ext4 volume with `project,quota`, initializes
`quotatype=prjquota`, sets `-m 0`, and verifies the project quota inode,
mount/remount state, generic get/set round trip, and task-usable
`f_bavail`/`f_favail` capacity before installing the helper.

Admission is two phase. A trusted SQLite transaction first holds the complete
task reservation while the task remains PREPARED. Only then may the helper
create/bind the exact project-quota directory and prove both kernel EDQUOT
limits. A second trusted SQLite transaction changes the held reservation into
QUEUED only after that proof. Process death or helper failure retains the
reservation for reconciliation; it is never released merely because an
exception occurred.

Reservations remain active while a retained workspace or quota can grow.
The trusted database release primitive therefore requires capability-produced
proof that the exact workspace is destroyed, its project quota is removed, and
zero further growth is possible. Physical usage observed after a release is
still an admission constraint; releasing a ledger row never erases bytes that
remain on the shared pool. Pool admission uses `statvfs.f_bavail` and
`f_favail`, the capacity actually available to the unprivileged task, while
the physical `f_bfree`/`f_ffree` values remain visible evidence.

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
in the v2 contract fail closed even if `user_version` and `schema_migrations`
claim a supported version.

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
| `storage_domains` | Verified shared Symphony-pool evidence | per-project observation rows; every row must carry the same ext4 mount identity and bounded capacity proof |
| `storage_reservations` | Pre-dispatch full-task capacity reservation | one host-derived quota identity per task; byte/inode allowance; reserved/released lifecycle |

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

The database constrains the vocabulary; trusted `runtime/lifecycle.py` licenses
the Step-6 transition graph and derives the next state from an accepted
result outcome. The contained task never writes SQLite and never chooses the
next state. The `ARCHITECT` role is represented in
`role_runs` and event provenance; the specialized worker role set remains
`PROJECT-MANAGER`, `PLANNER`, `IMPLEMENTER`, `REVIEWER`, `ADVERSARY`, and
`ARCHIVIST`.

Step-6 result findings carry the finite `blocker_kind` value `human`,
`project`, `infrastructure`, or `null`. `unresolved project decision` requires
`human` or `project`; `infrastructure condition` requires `infrastructure`;
licensed and rejected findings require `null`. Descriptive finding text never
selects escalation authority. A canonical `BLOCKED` role verdict is distinct
from a reviewer `REQUEST_CHANGES` report, which the strict result normalizes to
`FINDINGS`.

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
managed Step-7 `start_publication()` / `finalize_publication()` transaction;
it is not independently writable through `update_heads()`. `publications.head_sha`
is the HEAD named by the current/last publication record. When a publication is
marked `published`, its HEAD must equal `tasks.current_head`, and that
publication row plus `tasks.published_head` plus `publication_finished` and the
READY transition are committed in one transaction. An in-progress or failed
publication may name a candidate HEAD without changing the last successful
`published_head`.

Step 7 persists a durable `started` intent before external branch or PR
mutation. A handled failure changes that intent to `failed`, retains known
branch/PR evidence, and records an infrastructure blocker; a later attempt may
move `failed` back to `started`. Successful finalization is monotonic: a
`published` publication and READY task cannot be downgraded to `started`,
`failed`, or an absent publication. A process disappearance may leave
`started` for explicit crash recovery. `finalize_publication()` returns both
the task and publication snapshots materialized inside its transaction, so no
post-commit read is needed to establish the result.

The lower-level `record_publication()` primitive remains only as a legacy/test
persistence seam; it is not the managed Step-7 publication authority.

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

## Step-6 and later boundaries

Runtime remains an observation/read-only consumer. The host reconciliation
operation is the only lifecycle writer; `after_run` exit status is not the
barrier because SQLite blocker side effects govern later routing. Role-run
records are lifecycle records of accepted orchestrator results, not proof of
real custom-agent invocation. ARCHIVIST is the Step-6 endpoint. Step 7 owns
publication and the final READY transition. Existing Step-8
credential-isolation, aggregate-storage, Runtime pin-to-exec TOCTOU, and live
WSL containment blockers remain open.
