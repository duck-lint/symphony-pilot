# Pilot operations

The parent harness defines the lifecycle. This document records only current
operating boundaries and evidence status.

The supervised-local substrate is proven with explicit operator opt-in:
SYMPHONY_SUPERVISED_LOCAL=1. Task mutation is trusted host CLI plus Pilot
SQLite. The local UI is read-only. Runtime reads a project-scoped Pilot
projection and returns execution observations.

This evidence does not prove the canonical fresh-specialist lifecycle,
unattended credential isolation, aggregate quota enforcement, publication, or
human merge. Do not present those target behaviors as current operation.

## Safe operating boundary

- Use the registered project profile and trusted host controls.
- Keep host credentials outside task workspaces, Runtime scheduler state, role
  context, logs, and UI responses.
- Treat model output, role packets, workspace bytes, Git metadata, and Runtime
  observations as inputs to Pilot validation, not as authority.
- Do not manually edit lifecycle state or infer execution from a role label.
- Do not use external service state, browser state, or repository prose as local
  scheduler authority.
- Do not treat Runtime retries or process events as lifecycle transitions.
- Publication is a separate Pilot-authorized host operation; merge is human-only.

The host Git broker applies to every authorized writer role. Planner,
Implementer, and Archivist each receive a separate bounded grant. The host
observes the exact delta, validates every path against that grant, rejects
unauthorized changes, stages the accepted delta, creates the commit, and
records the resulting facts in Pilot. No role may write .git or perform Git
staging/commit.

## Evidence categories

Report these separately:

- frozen benchmark: the parent harness target;
- current implementation: the checked-out Pilot/Runtime behavior;
- tested behavior: behavior established by tests or fixtures;
- live-proven behavior: behavior established by supervised execution.

A successful supervised run does not promote current implementation into
conformance with the frozen lifecycle.
