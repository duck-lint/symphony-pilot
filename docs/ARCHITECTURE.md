# Architecture

symphony-pilot is a host control plane around the official OpenAI Symphony lifecycle. Project profiles remain the canonical registry; target repositories remain authoritative for project meaning and validation.

## Trust topology

The host owns the registry, GitHub credentials, task admission, runtime locks, process state, logs, recovery decisions, and publication. A task receives only its current source checkout, a task-local home, pilot role policy, and a narrow read-only admission projection. Issue prose, agent output, local Git metadata, and the task outbox are untrusted.

The selected execution backend is one Linux/WSL unshare namespace contract: user, mount, PID, and network namespaces plus explicit resource limits. The backend capability probe passes on the current WSL kernel, but activation is not licensed until the Codex credential boundary also passes. Codex policy is defense in depth, not structural proof. If either boundary cannot be proven, admission stops with an infrastructure blocker.

## Task admission

At dispatch the trusted host obtains the repository default branch and its server-reported HEAD, creates the workpad, and writes:

    ~/.local/state/symphony-pilot/<slug>/tasks/GH-<N>/task.json

The strict symphony-pilot-task/v1 record contains the repository, project, issue, task id, dispatcher, default ref, base SHA, derived issue branch, authoritative workpad comment id, publication/PR state, and reviewed runtime identities. Continuation uses this record and server metadata only. Missing state requires fresh admission.

Branches are host-derived as codex/gh-<issue>-<task-id-prefix>. No issue or workpad text can choose a branch, ref, SHA, checkout, credential, or remote.

## Publication

The task writes a strict /symphony-outbox/result.json request, which is a separate task-local mount and not project source. The host-side validator checks its task identity, action, and exact commit before a host-owned publication clone may publish with ambient Git configuration, hooks, SSH-agent sockets, and task remotes disabled. Network publication remains disabled while the execution blocker is active. The task has no publication credential and cannot push.

## Lifecycle

There is one issue, one derived issue branch, one draft PR, and one workpad. The default branch must be protected server-side so changes require a pull request and the automation identity cannot bypass protection. Human merge is not a prompt claim; it is an admission prerequisite.

## Current execution status

The host-side contracts are implemented, but unattended execution is disabled. The exact current Codex runtime does not provide a proven boundary keeping App Server authentication inaccessible to hostile tool children, and external execution routing has an observed host-local fallback. This is a concrete runtime blocker, not permission to select a same-user fallback.
