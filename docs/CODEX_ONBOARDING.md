# Codex onboarding

Runtime/App Server execution is an implementation boundary owned by Runtime.
Pilot supplies the task-scoped execution projection and the exact role grant;
it does not perform specialist reasoning or launch a role as a semantic actor.

The supervised-local path uses explicit operator opt-in:

    SYMPHONY_SUPERVISED_LOCAL=1

The operator's authenticated Codex environment in this mode is evidence of
supervised local operation only. It is not proof of unattended credential
isolation.

Role policy files describe bounded behavior and are not execution evidence or
capability authority. Fresh specialist execution must be established by
retained host evidence. The PM/Dispatcher is task-scoped and persistent in the
canonical model; the harness does not prescribe one immortal process, App
Server session, or thread.

Pilot authorizes separate grants for Planner, Implementer, and Archivist.
Reviewer, Adversary, and PM/Dispatcher remain non-writing. Host Git brokerage
handles every authorized writer delta; no role stages, commits, writes .git,
or changes its grant.

