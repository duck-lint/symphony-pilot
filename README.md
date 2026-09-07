# symphony-pilot

> **SUPERVISED LOCAL MVP: PROVEN**
>
> Pilot `a88b075fb0ab60af369b377992c98706affd3b5e` has been proven with
> Runtime `bca0d7027c49ef9bc62ee07de0bf669b8d3cb3d6` under
> `SYMPHONY_SUPERVISED_LOCAL=1`. The control UI is
> `http://127.0.0.1:8765`; the Runtime dashboard is
> `http://127.0.0.1:4041` while Runtime is running.

A trusted host-side control plane for local-task runs of the project-owned
`symphony-runtime`. The canonical project registry is under `projects/`.
Target repositories remain authoritative for project meaning, architecture,
validation, and stop conditions.

## Architecture

Step 5: SQLite determines what work exists. Step 6: SQLite determines what
has happened to that work. Lifecycle reconciliation is host-only; Runtime
remains read-only, and the Archivist role closes out final mechanical
acceptance before Step 7 publication.

The trusted host owns project admission, server-derived Git identity, runtime
locks, process state, credentials, logs, recovery, branch-protection preflight,
and publication. A T-N task receives only its current source checkout and a
fresh task-local Codex policy home. Task output is an untrusted strict outbox.

Task branches are host-derived as
codex/t-<identifier>-<task-id-prefix>. The trusted host reads the registered
repository's default ref and exact authoritative SHA from the official GitHub
API; both are stored in a strict host task record. Git transport materializes
bytes only and cannot choose repository authority.
GitHub Issue and workpad prose never controls checkout, branch, ref, SHA,
remote, credential, or process state.

Codex policy is defense in depth. The one supported structural backend is the
Linux/WSL unshare namespace contract with mount, PID, network, and resource
limits, CPU time, and child-tree teardown. These are admission invariants for
hardened/unattended operation. In the explicit supervised-local path, the
normal installed Codex App Server runs with the operator's existing
authenticated Codex environment. The task must still have no
tracker/publication credentials, operator CODEX_HOME, SSH agent, sibling
workspace, host state, or arbitrary tool network.

## Current status

Host-side local-task intake, SQLite scheduler configuration, local workspace
preparation, supervised Codex execution, lifecycle reconciliation, and the
read-only control UI are live and canary-proven. The ordinary local queue path
performs `PREPARED -> QUEUED` without invoking Step-8 quota admission.

The supervised path is explicitly operator-enabled with
`SYMPHONY_SUPERVISED_LOCAL=1`. It is not an unattended credential-isolation
boundary. Aggregate task quota/EDQUOT enforcement, hostile-child credential
isolation, Runtime pin-to-exec TOCTOU closure, remote workers, authenticated
mutating UI, and publication acceptance remain future hardening/target seams;
none of them means supervised SYMPHONY cannot run.

## Requirements

- Python 3
- Git
- reviewed `symphony-runtime` executable
- official Codex
- Linux/WSL with unprivileged user, mount, PID, and network namespaces
- reviewed runtime lock and protected default branch
- project-scoped publication deploy key at the derived host secret path

Physical lifecycle operations run under WSL/Linux. Native Windows checks may
validate profiles and dry-run deployment but must not fabricate WSL paths or
mutate Linux state.

The bounded Windows-host bridge in `scripts/wsl_adapter.py` is the only
approved Windows-to-Linux transition for diagnostic or acceptance work. It
targets only Ubuntu-24.04 as `duck-lint`, admits only approved Pilot/runtime
roots, and passes structured argv to the existing Linux `linux-unshare`
containment boundary. The pre-containment supervisor is loaded only from the
host-owned `symphony-canary` control deployment under
`/home/duck-lint/.local/share/symphony-pilot/deployments/symphony-canary`; its
manifest must verify the supervisor and containment files before containment
is built.
The contained domain mounts only the selected project read-only, the reviewed
`mise` executable and data root read-only, and explicit build/cache and
release-output roots writable. It does not mount the Pilot source checkout.
It fails closed on WSL, deployment identity, path, command, timeout, or
output-boundary errors. See `docs/WSL_ADAPTER.md` for its contract and safe
invocation form.

## Validation

    python3 -m unittest discover -s tests -v
    python3 -m compileall -q runtime scripts tests
    python3 scripts/validate_profile.py
    python3 scripts/validate_profile.py
    python3 scripts/deploy.py --project symphony-canary
    python3 scripts/project.py --project symphony-canary start

For supervised local development, start with the explicit opt-in documented in
`docs/OPERATOR_RUNBOOK.md`. The stronger containment and credential checks
remain required before unattended activation.

See docs/ARCHITECTURE.md, docs/SECURITY.md, docs/OPERATIONS.md,
docs/HUMAN_ONBOARDING.md, docs/CODEX_ONBOARDING.md, and docs/RECOVERY.md.
The exact operator sequence is in [docs/OPERATOR_RUNBOOK.md](docs/OPERATOR_RUNBOOK.md).
The host-owned SQLite contract is documented in docs/SQLITE_CONTRACT.md.
Runtime implements the production SQLite tracker adapter, and the managed
scheduler is configured to use it. Create a PREPARED task and explicitly queue
it with scripts/task.py; no GitHub Issue or dispatch label is scheduler input.
The Step-4 local operator surface is documented in docs/HOST_API.md and open or
deferred security decisions are preserved in docs/SECURITY_FINDINGS.md.
