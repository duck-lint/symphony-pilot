# Recovery

Execution domains are disposable. The canonical registry, host SQLite task
rows, local UUID/`T-N` identity, runtime locks, process identity, and host
audit events are the durable machine authority. GitHub issues, workpad
comments, and draft PRs are deferred lifecycle/publication evidence, not task
identity or scheduler input.

On restart, validate the exact task record and runtime identities. If the task record is missing or malformed, require clean re-admission. Never import branch, base, SHA, credential, or process state from arbitrary workpad prose or local .git metadata.

Step 6 recovery preserves the task row, current/versioned SQLite workpad,
role-run history, findings, blockers, events, and any unique unpublished HEAD.
If a host crash leaves one `ARCHITECT` role run in `started`, use the bounded
`task.py fail-attempt` control. It marks that exact run failed and preserves an
infrastructure blocker; it never creates a replacement attempt or advances
state. Resolve blockers only through `task.py resolve-blocker` after
inspection.

Missing, malformed, stale, or identity-mismatched lifecycle results fail
closed and produce an SQLite blocker when the database is available. Do not
repair by deleting the result, role run, workpad history, or events.
`ARCHIVIST` is a valid Step-6 parked endpoint awaiting Step 7 publication.

Do not migrate old role homes, temporary homes, task caches, stale workspaces, generated deployments, or recovery archives. Preserve a unique unpublished source artifact only through an explicitly host-approved narrow recovery format; it must exclude .git, credentials, sockets, devices, absolute escape paths, and task homes.

If local SQLite identity, containment, authentication, runtime identity,
ruleset protection, publication identity, or broker state cannot be proven,
stop and report the concrete blocker. Never recreate a task by marker, GitHub
Issue, or local Git state.
