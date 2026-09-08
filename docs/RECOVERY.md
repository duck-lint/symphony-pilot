# Pilot recovery boundary

The parent harness defines lifecycle meaning and termination classification.
Recovery mechanics must preserve those distinctions.

A task is durable. A lifecycle is one bounded orchestration attempt on that
task. Working rounds and planning attempts are nested within a lifecycle.
A non-converged lifecycle termination is not acceptance, completion, a blocker,
or an automatic new lifecycle. Human disposition is required before another
lifecycle for the same task.

Pilot is the authority for recovery of lifecycle state, grants, retained
execution evidence, blockers, termination facts, repository observations, and
workspace observations. Runtime process/session state is observation supplied
back to Pilot; it cannot terminalize or advance a lifecycle by itself.

On recovery, reconcile only after the managed Runtime identity and stop state
are proven. Preserve the exact task and execution identities and retain
incomplete, failed, or contradictory evidence. Never infer state from model
prose, packets, external service records, browser state, task-controlled Git metadata,
or a stale dashboard row.

Writer recovery uses the same broker contract as ordinary execution:
observe the exact delta, validate it against the original role grant, reject
unauthorized changes, then host-stage and host-commit only the accepted delta.
Pilot records the result. Planner, Implementer, and Archivist grants remain
distinct.

The parent harness does not prescribe crash/restart/session mechanics for the
task-scoped PM/Dispatcher. Any implementation must preserve task-scoped
continuity without requiring an immortal process, App Server session, or
thread.

If identity, authority, containment, execution evidence, grant, workspace,
or repository facts cannot be proven, stop and report the concrete blocker.
Do not create compatibility fallbacks or revive obsolete scheduler state.
