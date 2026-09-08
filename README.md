# symphony-pilot

The parent harness is the canonical SYMPHONY authority. This repository is
the trusted local control plane; it does not redefine the product contract.

Pilot owns durable task identity, project registration, lifecycle state,
working-round and planning-attempt authority, eligibility, accepted
transitions, blockers, termination classification, role capability grants,
execution/result reconciliation, repository-authority observation, host Git
and publication brokerage, and the canonical local API/UI projection.

Pilot does not perform specialist reasoning or execution. Runtime is the
deterministic executor and may only consume the Pilot-authorized projection and
grant.

## Canonical boundary

The parent harness defines the task-scoped persistent
PROJECT-MANAGER / DISPATCHER and fresh Planner, Reviewer, Implementer,
Adversary, and Archivist executions. There is no Architect lifecycle actor.

Planner writes only bounded plan and decision-memory artifacts. Implementer
writes only its authorized project seam. Archivist writes only bounded archive,
documentation, and project-memory artifacts. Reviewer, Adversary, and
PM/Dispatcher are non-writing.

For each authorized writer, the host broker observes the exact filesystem
delta, validates it against that role's exact Pilot grant, rejects unauthorized
paths, stages the accepted delta, and creates the commit. Pilot records the
execution evidence and resulting authorization, changed-path, commit, and HEAD
facts. Roles never stage, commit, write .git, or expand grants. Writer grants
are separate; no shared broad writable root exists.

## Evidence boundary

The supervised-local substrate is proven: local task intake, Pilot SQLite,
Runtime integration, App Server startup, reconciliation, and read-only UI
observation were exercised with explicit operator supervision. The canonical
fresh-specialist lifecycle, unattended credential isolation, publication, and
merge are not live-proven by that evidence.

See:

- docs/ARCHITECTURE.md for Pilot ownership;
- docs/HOST_API.md for the read-only projection boundary;
- docs/OPERATIONS.md and docs/RECOVERY.md for current operational limits;
- docs/SECURITY.md and docs/SECURITY_FINDINGS.md for security status;
- docs/SQLITE_CONTRACT.md for the authority boundary only;
- docs/WSL_ADAPTER.md for the separate diagnostic bridge.

The parent harness defines lifecycle semantics, conformance, and the golden
path. It is not imported as a runtime dependency.

