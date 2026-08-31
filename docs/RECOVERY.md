# Recovery

Execution domains are disposable. Host task admission records, runtime locks, process identity, and host audit events are the durable machine authority. GitHub issues, the authoritative workpad comment, the derived branch, and the draft PR are durable lifecycle evidence.

On restart, validate the exact task record and runtime identities. If the task record is missing or malformed, require clean re-admission. Never import branch, base, SHA, credential, or process state from arbitrary workpad prose or local .git metadata.

Do not migrate old role homes, temporary homes, task caches, stale workspaces, generated deployments, or recovery archives. Preserve a unique unpublished source artifact only through an explicitly host-approved narrow recovery format; it must exclude .git, credentials, sockets, devices, absolute escape paths, and task homes.

If containment, authentication, runtime identity, or branch protection cannot be proven, stop before task or tracker activity and report the concrete blocker.
