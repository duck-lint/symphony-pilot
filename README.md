# symphony-pilot

A trusted host-side control plane for local-task runs of the project-owned
`symphony-runtime`. The canonical project registry is under `projects/`.
Target repositories remain authoritative for project meaning, architecture,
validation, and stop conditions.

## Architecture

The trusted host owns project admission, server-derived Git identity, runtime
locks, process state, credentials, logs, recovery, branch-protection preflight,
and publication. A T-N task receives only its current source checkout and a
fresh task-local Codex policy home. Task output is an untrusted strict outbox.

Task branches are host-derived as
codex/t-<identifier>-<task-id-prefix>. The registered Git remote supplies the
default ref and exact base SHA; both are stored in a strict host task record.
GitHub Issue and workpad prose never controls checkout, branch, ref, SHA,
remote, credential, or process state.

Codex policy is defense in depth. The one supported structural backend is the
Linux/WSL unshare namespace contract with mount, PID, network, and resource
limits, CPU time, and child-tree teardown. These are admission invariants, not a claim that an unblocked task is
currently running: the exact runtime remains stopped at the auth-boundary
gate. Once activated, the task must have no tracker/publication credentials,
operator CODEX_HOME, SSH agent, sibling workspace, host state, or arbitrary
tool network.

## Current status

Host-side local-task intake, SQLite scheduler configuration, local workspace
preparation, ruleset protection checks, runtime identity locks, and fail-closed
containment gates are implemented and fixture-tested. Full lifecycle
persistence and publication narrowing remain deferred to Steps 6 and 7.
Persistent task-workspace aggregate disk growth is explicitly unbounded in this
cutover, and the exact current Codex App Server authentication path has no
proven way to keep its credential out of hostile tool children. Unattended
execution is therefore not activatable. Do not replace these limits with a
same-user or prompt-only fallback.

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
    python3 scripts/provision_publication_key.py --project cleanroom < publication-key.pem
    python3 scripts/deploy.py --project cleanroom --dry-run
    python3 scripts/project.py --project cleanroom start

The start command must fail closed until the capability blocker is resolved.
Run the original multi-role canary only after this repository is reviewed,
cut over, credentials are rotated, branch protection is configured, runtime
identities are pinned, and hostile-boundary probes pass.

See docs/ARCHITECTURE.md, docs/SECURITY.md, docs/OPERATIONS.md,
docs/HUMAN_ONBOARDING.md, docs/CODEX_ONBOARDING.md, and docs/RECOVERY.md.
The host-owned SQLite contract is documented in docs/SQLITE_CONTRACT.md.
Runtime implements the production SQLite tracker adapter, and the managed
scheduler is configured to use it. Create a PREPARED task and explicitly queue
it with scripts/task.py; no GitHub Issue or dispatch label is scheduler input.
The Step-4 local operator surface is documented in docs/HOST_API.md and open or
deferred security decisions are preserved in docs/SECURITY_FINDINGS.md.
