# symphony-pilot

A trusted host-side control plane for issue-driven runs of the project-owned
`symphony-runtime`. The canonical project registry is under `projects/`.
Target repositories remain authoritative for project meaning, architecture,
validation, and stop conditions.

## Architecture

The trusted host owns project admission, server-derived Git identity, runtime
locks, process state, credentials, logs, recovery, branch-protection preflight,
and publication. A GH-N task receives only its current source checkout and a
fresh task-local Codex policy home. Task output is an untrusted strict outbox.

Task branches are host-derived as codex/gh-<issue>-<task-id-prefix>. Base ref and
base SHA come from trusted GitHub metadata and are stored in a strict host task
record. Issue and workpad prose never controls checkout, branch, ref, SHA,
remote, credential, or process state.

Codex policy is defense in depth. The one supported structural backend is the
Linux/WSL unshare namespace contract with mount, PID, network, and resource
limits, CPU time, and child-tree teardown. These are admission invariants, not a claim that an unblocked task is
currently running: the exact runtime remains stopped at the auth-boundary
gate. Once activated, the task must have no tracker/publication credentials,
operator CODEX_HOME, SSH agent, sibling workspace, host state, or arbitrary
tool network.

## Current status

Host-side admission, dispatch provenance, strict result brokering, sterile
bundle publication, ruleset protection checks, runtime identity locks, and
fail-closed containment gates are implemented and fixture-tested.
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
The host-owned SQLite contract is documented in docs/SQLITE_CONTRACT.md; it is
not yet consumed by the runtime or scheduler.
