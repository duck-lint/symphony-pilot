# Step 6 implementation report

This is an implementation record, not an acceptance record. Neither
repository is merged by Step 6.

## Exact pair

- Pilot base: `bf7c546aa87e085e8ebaa5782f9193f8aa45a2e3`
- Runtime base: `54753c1a702a3131c5f522703af8c25e10638f44`
- `RUNTIME_STEP6_HEAD`: `5a4405d8346caee6fff0ed50dcc11119151a1f3e`
- Runtime branch: `codex/step-6-runtime-routing`
- Pilot branch: `codex/step-6-pilot-lifecycle`

Pilot targets the exact Runtime candidate above. No alternate Runtime revision
is silently substituted.

The review correction round makes mechanical-validation correction top-level
Architect evidence with no specialized packet, permits bounded partial blocked
evidence with canonical `BLOCKED` role verdicts, binds blocker authority to a
finite `blocker_kind`, opens hostile files nonblocking before `fstat`, and
compensates post-allocation staging failures. Architect allocation and its
started event now share one SQLite `BEGIN IMMEDIATE` transaction. Policy files
and the activation launcher are part of deployment coherence.

## Implemented authority

Step 5 says what work exists: SQLite task rows are the scheduler input and
Runtime reads them project-scoped and read-only. Step 6 says what happened:
trusted Pilot host code owns queue activation, Architect-attempt identity,
run-specific lifecycle staging, strict result validation, Git verification,
role-run allocation, findings, blockers, workpad CAS, exact-head acceptance,
replay rejection, and atomic lifecycle reconciliation.

The managed workflow requests exactly:

```text
QUEUED
PLANNED
IMPLEMENTED
REVIEW
ADVERSARIAL_REVIEW
FINAL_MECHANICAL_ACCEPTANCE
```

It excludes PREPARED, ARCHIVIST, READY_FOR_HUMAN_MERGE, HUMAN_BLOCKED, and
INFRASTRUCTURE_BLOCKED, and renders `agent.max_turns: 1`. ARCHIVIST is the
Step-6 endpoint. READY remains guarded by successful exact-current-HEAD
publication, which is Step 7 and has not started.

## Evidence boundary

The disposable fixture producer is `scripts/generate_step6_fixture.py`; it
contains PREPARED through ARCHIVIST, a blocked active task, and a second
project. The lifecycle E2E uses a disposable target Git repository and
disposable SQLite database without launching Codex.

Pilot tests and Python compilation are runnable in this Windows environment.
The sandbox still reports WSL `E_ACCESSDENIED`, but approved execution outside
the sandbox reaches Ubuntu-24.04 and runs native tests. Runtime PR #1 first
tested `9ad5a04` and failed dependency audit (Mint 1.9.3); native `make all`
then exposed the remaining blocker-free HUMAN_BLOCKED fixture assertion.
The corrected candidate changes only that assertion and the Mint lock entry.
Native `make all` passes, including audit, coverage and Dialyzer; the PR's
production artifact workflow supplies the separate hosted artifact evidence.
Existing Step-8 findings remain
open, including credential isolation, aggregate task storage, Runtime
pin-to-exec TOCTOU, and live WSL containment.

## Trusted workspace boundary correction

`workspace_boundary.py` owns host Git calls against reused task clones and
atomic host metadata writes. The task must be stopped during these operations;
this does not prove safe concurrent execution or remove the Step-8 gate.
Local configuration is parsed as data outside the repository with includes
disabled, then admitted through a finite allowlist: basic non-executable clone
settings, origin URL/fetch refspec, branch tracking, and inert author identity.
Unknown keys, includes, filters, execution commands, rewrite rules, metadata
symlinks/special files and external Git-directory/object indirection fail closed.
Hooks and fsmonitor are disabled even for otherwise accepted repositories.

The Git environment drops inherited Git variables and system/global config,
replacement objects, interactive prompts and pagers. The registered remote is
verified literally and supplied directly to fetch. SSH may use the operator's
host-owned SSH configuration and agent. Optional Git credential/transport
configuration is admitted only from the explicit host file
`~/.config/symphony-pilot/git-transport.config` for transport calls; it must
remain outside the task. Task configuration cannot select that authority.

Metadata writes use unpredictable exclusive regular temporaries (0600), fsync
and atomic replacement. POSIX parent descriptors are opened without following
symlinks. Windows rejects reparse parents; the stopped-task prerequisite also
applies there. Existing predictable `.tmp` and final leaf symlinks are never
followed. Preparation resolves T-N through project-scoped SQLite before Git
inspection, permitting durable infrastructure blockers for early failures.

Successful implementation/correction requires a new clean descendant HEAD.
A blocked implementation can retain its starting HEAD; a clean partial commit
is persisted with `head_changed`, invalidating earlier acceptance while keeping
the phase and blocker. Planning requires ordered PM then Planner evidence;
blocked planning accepts only prefixes of that order.

### Unpublished continuation correction

When `tasks.current_head` is populated, Step 6 continues from the retained
physical T-N workspace. Preparation proves the shared workspace boundary, the
registered origin, the host-owned branch, exact equality between local `HEAD`
and SQLite `current_head`, clean status, commit validity, and recorded-base
ancestry. It does not fetch or repair the task branch from the remote, and it
does not switch the branch as a continuation repair. A branch or HEAD mismatch
is persisted as an infrastructure blocker while leaving both the local branch
and SQLite value unchanged. The remote task branch remains a Step-7 publication
artifact.

When a licensed correction reaches an actual IMPLEMENTER attempt and that attempt
is blocked, the persisted role round is retained in role history and the exact
still-licensed findings are atomically moved to the next host-allocated
IMPLEMENTER round. The lifecycle phase, blocker, ownership and HEAD rules do not
change. A blocker before an IMPLEMENTER packet consumes no IMPLEMENTER round, so
the correction license remains where it was. Completion still requires a new
clean HEAD and an exact finding/round match.
