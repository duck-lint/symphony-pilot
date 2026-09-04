# Step 6 implementation report

This is an implementation record, not an acceptance record. Neither
repository is merged by Step 6.

## Exact pair

- Pilot base: `bf7c546aa87e085e8ebaa5782f9193f8aa45a2e3`
- Runtime base: `54753c1a702a3131c5f522703af8c25e10638f44`
- `RUNTIME_STEP6_HEAD`: `9ad5a047d28ebfc14f390965851003e3ed99d389`
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
Runtime canonical `make ci` and live Windows-to-WSL proof remain unavailable:
`make`/the Elixir toolchain are absent and WSL returns
`Wsl/EnumerateDistros/Service/E_ACCESSDENIED`. Existing Step-8 findings remain
open, including credential isolation, aggregate task storage, Runtime
pin-to-exec TOCTOU, and live WSL containment.
