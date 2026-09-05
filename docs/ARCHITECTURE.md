# Architecture

symphony-pilot is a trusted host control plane around the project-owned
`symphony-runtime` lifecycle implementation. Target repositories remain
authoritative for project meaning, validation, private inputs, and human stop
conditions. OpenAI Symphony is historical/reference material, not continuing
lifecycle or architectural authority.

## Authority topology

Step 5: SQLite determines what work exists. Step 6: SQLite determines what
has happened to that work. Pilot is the lifecycle authority; Runtime only
observes the database through its read-only adapter. The Architect result is
an untrusted report, and host code derives identities, rounds, paths, Git
truth, and the next state before one atomic reconciliation.

The host owns the canonical registry, SQLite task rows, local task identity,
publication deploy key, runtime locks, process state, and publication. Issue
prose, workpad prose, task Git metadata, task output, and task filesystem state
are untrusted payload. GitHub is publication-only and is not scheduler or
lifecycle authority.

Review correction routing is phase-specific: reviewer and adversary corrections
require their specialized `FINDINGS` packet; mechanical-validation correction is
top-level Architect evidence and requires no specialized packet. A `BLOCKED`
result may contain only the bounded role evidence available before the stop, but
must produce an open SQLite blocker. Human/project/infrastructure escalation is
carried by finite `blocker_kind`, never inferred from finding prose.

The profile supplies project onboarding data: repository, clone remote, any
currently retained host-secret/publication settings, model settings, and
resource preferences. Host paths and publication-key paths are derived from
the project slug. Legacy dispatch fields are not Runtime scheduler input.

## Local task intake and scheduler authority

The trusted operator CLI creates local tasks in the host-wide
`control.sqlite3`. It accepts only a registered project slug, title, and
objective. Pilot resolves the registered Git remote's symbolic `HEAD` and
exact commit with `git ls-remote --symref <remote> HEAD`; ambiguity fails
closed. The database transaction derives the UUID, next unique `T-000001`
identifier, and `codex/t-000001-<12-hex-character-UUID-prefix>` branch.

Creation produces `PREPARED`. A separate `task.py queue` command performs the
accepted compare-and-set transition `PREPARED -> QUEUED`, creates the first
host-generated workpad, and appends the `queued` event in the same transaction.
Runtime reads SQLite in read-only mode, scoped to the rendered project slug;
its adapter routing gate is no open blocker, while the configured
`active_states` set is the scheduler gate. No GitHub issue, issue number,
dispatch label, or `GH-N` identity is required or accepted in this causal
chain.

The workspace sequence is:

```text
PREPARED
   ↓ explicit host queue
QUEUED
   ↓ Runtime SQLite scheduler
T-000001 workspace
```

## Protection and publication

The supported GitHub protection contract is one active repository ruleset
targeting `~DEFAULT_BRANCH` (or the exact default ref), containing one
`pull_request` rule and an empty `bypass_actors` list. Classic branch-protection
normalization and invented human-actor fields are not supported.

Publication uses the deterministic host secret
`~/.config/symphony-pilot/secrets/<slug>/publication-ssh-key`, mode 0600. It is
separate from the Issues/PR tracker token, human account, and task domain.

Step 6 deploys the strict local lifecycle result broker. A bounded
`before_run` allocates one host Architect attempt and run namespace; the
rendered `after_run` independently verifies Git truth and atomically
reconciles accepted results into SQLite. Since Frozen Runtime treats
`after_run` failures as best-effort, exit 78 is not an activation barrier; the
SQLite blocker side effect is the routing barrier.

Step 7 publication is a separate trusted operator operation:

```text
SQLite ARCHIVIST
      |
      v
Pilot exact-head publication broker
      |-- retained trusted workspace evidence
      |-- host-generated bundle and sterile Git
      |-- exact deploy-key proof
      |-- fresh ruleset Snapshot A/B
      |-- exact task branch and draft PR
      v
SQLite atomic finalization -> READY_FOR_HUMAN_MERGE -> HUMAN ONLY
```

The model cannot request publication and GitHub cannot create lifecycle
authority. Publication failures preserve branch/PR evidence and create an
infrastructure blocker. Step 7 does not merge, approve, un-draft, or enable
auto-merge. Ruleset state can change after READY, so a human must recheck
protection and the exact PR head before merging.

## Execution truth states

| Boundary | State | Evidence |
|---|---|---|
| Linux user/mount/PID/network namespace primitive | PROVEN | real WSL `unshare` probe |
| Synthetic task filesystem constructor | PROVEN | hostile fixture; no broad `/etc` mount |
| Supervisor child teardown and CPU bound | PROVEN BY FIXTURE | shared `--kill-child=SIGKILL` runner |
| Aggregate persistent-workspace disk quota | IMPLEMENTED / TARGETED-VERIFIED / REQUIRES STEP-8 REVIEW | shared-pool reservation and kernel-proof admission seams; dedicated domain provisioning remains required |
| Local SQLite task intake / queue authority | TARGETED / FIXTURE-VERIFIED | trusted task CLI, transactionally allocated identity, and PREPARED-to-QUEUED CAS tests |
| Runtime SQLite scheduler contract | ACCEPTED CONTRACT / LIVE PROOF BLOCKED | frozen Runtime adapter contract; Pilot fixture and workflow pair await the canonical artifact smoke because WSL is unavailable |
| Legacy GitHub admission / dispatch provenance | RETIRED / NOT MANAGED | historical parser/provenance source is outside the deployed Step-5 scheduler path |
| SQLite lifecycle reconciliation | TARGETED / FIXTURE-VERIFIED | strict result, trusted Git checks, and atomic Step-6 synthetic E2E |
| Host publication transfer | TARGETED / FIXTURE-VERIFIED | Step-7 exact-head host publication; no model or Runtime publication path |
| Ruleset protection parser | PROVEN BY FIXTURES | real API-shaped fixtures |
| Codex App Server activation | BLOCKED | existing execution-capability gate and launch boundary remain fail-closed |
| Live Codex task containment | NOT YET PROVEN | no Codex task was started |
| Multi-role canary and live cutover | NOT RUN | explicitly prohibited for this PR |

## Containment

The selected backend is rootless Linux/WSL `unshare`. The constructor creates a
fresh tmpfs root, mounts only the current workspace, task home, read-only
admission inbox, writable fixed outbox, bounded tmpfs, minimal devices, and
read-only runtime libraries, then mounts a task PID namespace and restricted
network namespace. It does not mount host `/etc`; the current explicit
allowlist is empty. The shared runner uses util-linux `--kill-child=SIGKILL`,
reaps the supervisor after timeout, and the fixture verifies a child and
grandchild stop modifying a task sentinel. CPU time, process count, address
space, open files, individual file size, task tmpfs, and wall clock are
bounded. The managed queue now reserves full task capacity against one shared
pool and rejects admission unless task-specific kernel byte/inode proof is
supplied. The canary policy distinguishes nominal 64-GiB backing from a
63-GiB allocatable filesystem ceiling. The fixed adapter's task admission
operation requires a root-owned, capability-specific quota helper to bind the
host-derived task project and prove byte/inode `EDQUOT` behavior.
Reservations cannot be released without trusted proof that the
exact workspace and quota can no longer grow. The current root filesystem is
rejected, so unattended activation remains blocked pending dedicated-domain
and helper provisioning. The constructor is
exercised independently of Codex; the auth blocker still stops the real
launcher before App Server start.
