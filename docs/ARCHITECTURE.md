# Pilot architecture

The parent harness is authoritative for SYMPHONY semantics. This document
records only the Pilot boundary.

Pilot is the deterministic local control plane. It owns:

- durable task identity and project registration;
- lifecycle, working-round, and planning-attempt state;
- eligibility, accepted transitions, blockers, and termination classification;
- role capability grants and result/evidence reconciliation;
- repository-authority observation and workspace facts;
- host-side Git and publication brokerage; and
- the canonical local API/UI projection.

Pilot does not perform specialist reasoning or execution. Runtime consumes the
Pilot-authorized execution projection and returns host-observed evidence.

The lifecycle benchmark is the persistent task-scoped
PROJECT-MANAGER / DISPATCHER coordinating fresh Planner, Reviewer,
Implementer, Adversary, and Archivist executions. There is no Architect actor.
The parent harness defines the lifecycle and should be referenced rather than
duplicated here.

## Authority boundary

Pilot authorizes exact role grants. Planner may write only bounded plan and
decision-memory artifacts. Implementer may write only its authorized project
seam. Archivist may write only bounded archive, documentation, and
project-memory artifacts. Reviewer, Adversary, and PM/Dispatcher are
non-writing.

For every authorized writer, the host broker observes the exact filesystem
delta, validates every changed path against that role's Pilot grant, rejects
unauthorized changes, stages the accepted delta, and creates the commit.
Pilot records the execution evidence, grant, changed paths, commit, and HEAD
facts. No role stages, commits, writes .git, or expands its own grant. Writer
grants are separate; there is no shared broad writable root.

Publication is a separate Pilot-authorized host operation. Final merge remains
human-only.

## Current evidence

The supervised-local substrate is proven: local task intake, Pilot SQLite,
Runtime integration, App Server startup, reconciliation, and read-only UI
observation were exercised under explicit operator supervision. The canonical
fresh-specialist lifecycle, unattended credential isolation, publication, and
merge are not thereby live-proven. See the parent harness current-state record.

