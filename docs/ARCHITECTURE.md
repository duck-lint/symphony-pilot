# Architecture

`symphony-pilot` is a generic control plane for an arbitrary finite registry
of projects. The official OpenAI Symphony executable is shared host
infrastructure; it is installed independently of this repository and is
resolved at runtime only through `SYMPHONY_BIN` or `PATH`.

The pilot source checkout contains generic runtime, policy, deployment, and
operator code. It contains no project architecture or closed list of project
identities. The canonical registry is the tracked set of
`projects/<slug>/profile.toml` files. The directory name must equal `slug`.
An empty registry is valid; every discovered profile is loaded and the full
collection is validated before a slug is resolved.

Each profile supplies repository identity, its Git remote, tracker labels,
secret reference, execution limits, Codex settings, and optional host
integration preferences. Deployment, workspace, state, log, credential, lock,
process, workflow, and service namespaces are derived from the slug. Dashboard
ports are stable derived user-space ports and are rejected if they collide.
There is no persisted `deployment_root`, `workspace_root`, `state_root`,
`log_root`, `service_identity`, or `dashboard_port` field.

The host topology is:

```text
shared host
  official Symphony executable
  symphony-pilot source checkout

tracked registry
  projects/<slug>/profile.toml

per project pi
  ~/.local/share/symphony-pilot/deployments/<slug>
  ~/symphony-workspaces/<slug>
  ~/.local/state/symphony-pilot/<slug>
  ~/.config/symphony-pilot/secrets/<slug>/<reference>
```

`deployment(pi)` contains the generated profile, workflow, runtime, generic
role policies, operator CLI, and manifest. It never contains the official
Symphony executable. Atomic backup/replacement is bounded to that one derived
deployment directory. `start`, `stop`, `finish`, `status`, and `test` operate
on the selected slug's derived state and deployment only.

For distinct projects, registry validation rejects duplicate repository
identity, service identity, dashboard port, or any equality/containment overlap
among project-owned namespaces. Repository identity is globally unique because
the same label text in the same repository would otherwise create tracker
dispatch authority across profiles. Tracker labels remain repository-scoped;
the same label may be used in different repositories.

Adding `p(n+1)` means adding only `projects/<slug>/profile.toml` and satisfying
real host prerequisites. No generic source, schema, runtime dispatch, or
operator code changes are needed. The remaining limits are actual host
resources: available ports, disk, process/memory capacity, GitHub/API access,
the installed shared executable, and the target repository's credentials and
toolchain.

The role scaffold remains generic: ARCHITECT/orchestrator ownership,
PROJECT-MANAGER, PLANNER, IMPLEMENTER, REVIEWER, ADVERSARY, ARCHIVIST,
conformance review, falsification, fresh correction review, exact-HEAD
agreement, one issue/branch/draft PR/workpad, pagination, role-home leases,
process identity, recovery boundaries, and no auto-merge. Named-role App
Server execution remains a future live-canary capability boundary.
