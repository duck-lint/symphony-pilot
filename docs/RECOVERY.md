# Recovery and one-time CLEANROOM cutover

Normal recovery remains issue-scoped and fail-closed. Preparation uses the selected project's derived workspace and state namespace. Recovery archives exclude Git metadata, target output, and secret-shaped paths recursively; excluded names are recorded without copying contents. Remote issue branches and draft PRs are the durable continuation boundary.

This architecture change does not mutate the existing host installation. After the merge, the operator should inspect running processes, the old deployment, logs, and any needed local state. Preserve anything required for audit before removal.

The old resolved CLEANROOM topology was:

```text
profile deployment -> /home/duck-lint/symphony
```

The new topology is:

```text
profile deployment -> ~/.local/share/symphony-pilot/deployments/cleanroom
workspace          -> ~/symphony-workspaces/cleanroom
state              -> ~/.local/state/symphony-pilot/cleanroom
logs               -> ~/.local/state/symphony-pilot/cleanroom/logs
credentials        -> ~/.config/symphony-pilot/secrets/cleanroom/<reference>
official binary    -> host PATH or explicit SYMPHONY_BIN
```

Manual cutover, only after inspecting/preserving old state:

1. Confirm no old CLEANROOM Symphony process is running and preserve its logs or deployment if required by the operator.
2. Install or identify the shared official executable and set `SYMPHONY_BIN` (or place `symphony` on `PATH`). Do not put it under a project deployment.
3. Validate the merged registry and provision the existing CLEANROOM secret in its slug namespace if necessary.
4. Deploy and test with `--project cleanroom`.
5. Only after the new deployment passes, remove the obsolete `/home/duck-lint/symphony` deployment and any deliberately preserved legacy state. This repository does not perform that deletion.

The canary is then onboarded by validating its profile, provisioning only its own secret namespace, deploying `--project symphony-canary`, and performing a separate harmless canary. This document does not authorize starting it.
