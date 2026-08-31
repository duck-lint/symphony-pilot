# Security contract

The threat model treats the model, task processes, issue content, workpad content, task filesystem, task Git metadata, and task outbox as hostile.

Trusted host state is outside the task domain:

- canonical project registry and deployment identity;
- tracker and publication credentials;
- server-derived base/default metadata;
- task admission records, runtime locks, process state, and host logs;
- branch-protection and publication preflight.

The activated task contract has no tracker token, publication key, SSH agent,
operator Codex home, operator history, recovery state, host logs, sibling
workspaces, or arbitrary host network. Model-tool network is denied. App
Server transport is a separate host concern and is not evidence that task
tools may connect outward. The current start gate admits no task because the
runtime has not yet proved this contract end to end.

The intended inner Codex policy names only the contained current workspace and
denies network access. The outer Linux/WSL unshare backend is required to
supply the filesystem, PID, mount, network, and resource boundary. Its live
capability probe passes on the current WSL kernel, but the probe is not task
activation; execution remains blocked until the auth boundary also passes.

Authentication is a separate boundary. An App Server credential must be data-plane specific and unrecoverable by hostile tools. The current accepted runtime has no proven broker/descriptor contract for that property, so the launcher fails closed before App Server start.

Never use real secrets in denial probes. Use synthetic sentinels and verify that task tools cannot read host files, sibling workspaces, unrelated processes, credentials, sockets, or arbitrary network destinations.
