# Recovery

Execution domains are disposable. The canonical registry, host SQLite task
rows, local UUID/`T-N` identity, runtime locks, process identity, and host
audit events are the durable machine authority. GitHub issues, workpad
comments, and draft PRs are deferred lifecycle/publication evidence, not task
identity or scheduler input.

On restart, validate the exact task record and runtime identities. If the task record is missing or malformed, require clean re-admission. Never import branch, base, SHA, credential, or process state from arbitrary workpad prose or local .git metadata.

Do not migrate old role homes, temporary homes, task caches, stale workspaces, generated deployments, or recovery archives. Preserve a unique unpublished source artifact only through an explicitly host-approved narrow recovery format; it must exclude .git, credentials, sockets, devices, absolute escape paths, and task homes.

If local SQLite identity, containment, authentication, runtime identity,
ruleset protection, publication identity, or broker state cannot be proven,
stop and report the concrete blocker. Never recreate a task by marker, GitHub
Issue, or local Git state.
