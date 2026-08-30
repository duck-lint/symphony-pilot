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
ports are persisted finite host-resource allocations in the canonical profile;
onboarding tooling chooses an unused value in the supported range and registry
validation rejects duplicate assignments. Adding or removing a project never
renumbers existing assignments.
There is no persisted `deployment_root`, `workspace_root`, `state_root`,
`log_root`, or `service_identity` field.

The host topology is:

```text
SOURCE CHECKOUT
  canonical registry
  registry-wide validation
  lifecycle/operator CLI
  deployment command

GENERATED PROJECT DEPLOYMENT
  profile snapshot
  WORKFLOW.md
  runtime hooks/preparation
  architect/role policies
  DEPLOYMENT.json

SHARED HOST
  official Symphony executable
  canonical WSL/Linux operator root

tracked registry
  projects/<slug>/profile.toml

per project pi
  ~/.local/share/symphony-pilot/deployments/<slug>
  ~/symphony-workspaces/<slug>
  ~/.local/state/symphony-pilot/<slug>
  ~/.config/symphony-pilot/secrets/<slug>/<reference>
```

The source checkout contains registry discovery, project resolution, deploy,
and lifecycle commands. A generated `deployment(pi)` contains only the
generated profile snapshot, `WORKFLOW.md`, runtime hooks and preparation code
required by Symphony, architect/role policies, and `DEPLOYMENT.json`. It never
contains the source operator CLI or the official Symphony executable. Atomic
backup/replacement is bounded to that one derived deployment directory. Source
checkout lifecycle commands operate on the selected slug's derived state and
deployment only.

`DEPLOYMENT.json` records the exact generated inventory, its hashes, the
selected profile digest, and a bounded source lifecycle-contract digest.
`project.py test` and `project.py start` use the same verifier; `start` runs it
before reading the project credential or querying the tracker. Process state
also records the launched deployment identity, profile digest, and dashboard
endpoint so later status/stop operations continue to address the process that
actually started.

Physical namespace operations are WSL/Linux operations. Native Windows Python
may perform host-neutral registry validation and port allocation, but it must
not resolve or mutate a physical project namespace; Windows `USERNAME` is
never treated as a WSL username.

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
