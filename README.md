# symphony-pilot

A host-side control plane for running [OpenAI Symphony](https://github.com/openai/symphony) against issue-driven software projects without moving project authority into the orchestration layer.

`symphony-pilot` handles the reusable mechanics around a Symphony deployment: project profiles, isolated issue workspaces, deployment snapshots, credential boundaries, lifecycle controls, host integration, and recovery. The target repository still owns its architecture, acceptance criteria, validation authority, and human stop conditions.

## Why this exists

Symphony provides the agent runtime and lifecycle model. Real project deployments still need host-side machinery around that runtime: preparing clean workspaces, resuming from durable Git state, keeping secrets out of prompts and repositories, deploying reviewed workflow policy, and giving an operator predictable start/finish/recovery controls.

This repository keeps that machinery separate from the projects it operates on.

```text
GitHub issue / labels / comments
            |
            v
     host preparation
            |
            v
 isolated issue workspace
            |
            v
   official OpenAI Symphony
            |
            v
       Codex architect / orchestrator
            |
            v
 project-manager -> planner -> implementer
             |
      reviewer -> adversary
             |
 licensed correction loops / archivist
            |
            v
 branch + draft PR + workpad -> human merge
```

The durable continuation boundary is GitHub state plus the remote issue branch and draft pull request. Local mutable workspaces are disposable execution state.

## What it provides

- **Project profiles** — non-secret TOML configuration for repository identity, labels, workspace/state roots, limits, Codex settings, and host integration.
- **Issue-scoped workspaces** — clean preparation for initial work and continuation from the authorized remote issue branch.
- **Reviewed deployments** — atomic deployment directories with a `DEPLOYMENT.json` manifest recording the source artifacts that were deployed.
- **Bounded role lifecycle** — generic project-manager, planner, implementer, reviewer, adversary, and archivist policies under one issue branch, draft PR, and workpad.
- **Credential isolation** — project credentials live in host-side secret files and are not copied into Git, generated workflows, or Codex child environments.
- **Operator lifecycle controls** — validate, deploy, test, start, inspect status, finish gracefully, stop when idle, or emergency-stop.
- **Host integration** — WSL/Linux-native process state with optional Windows sleep inhibition and notifications.
- **Recovery mechanics** — explicit recovery state and archives without treating a local workspace as durable truth.

## What it deliberately does not own

`symphony-pilot` is not a fork of Symphony and is not a project-architecture framework. Generic infrastructure here must not encode the target project's semantic policy, implementation architecture, phase authority, acceptance criteria, or domain-specific repair rules.

The issue is the work order. The target repository is the authority for the work.

## Requirements

A deployment expects:

- a working checkout of this repository;
- Python 3;
- Git and authenticated access to the target repository;
- an installed/configured OpenAI Symphony runtime;
- a Codex environment usable by Symphony;
- any toolchain required by the target project's profile.

The operational design is Linux-first. On Windows, workspaces and process state should remain on the WSL-native filesystem; Windows integration is limited to detected host tooling, notifications, and optional sleep inhibition.

## Quick start

The included CLEANROOM profile is a concrete example. For another project, copy its shape and replace the project-specific values rather than editing generic runtime policy.

```bash
# Validate the non-secret profile.
python3 scripts/validate_profile.py projects/cleanroom/profile.toml

# Provision the host-side credential referenced by the profile.
python3 scripts/provision_secret.py projects/cleanroom/profile.toml

# Inspect, then perform, an atomic deployment.
python3 scripts/deploy.py --profile projects/cleanroom/profile.toml --dry-run
python3 scripts/deploy.py --profile projects/cleanroom/profile.toml

# Exercise the deployed control surface.
python3 scripts/project.py --profile projects/cleanroom/profile.toml test
python3 scripts/project.py --profile projects/cleanroom/profile.toml start
python3 scripts/project.py --profile projects/cleanroom/profile.toml status

# Normal end-of-session path: drain authorized work, then stop Symphony.
python3 scripts/project.py --profile projects/cleanroom/profile.toml finish
```

`stop` refuses to terminate active work. `stop-now` is the emergency path.

Before authorizing real work for a new project, run a harmless issue through the complete dispatch/workspace/multi-role/workpad/completion/cleanup lifecycle. The canary must verify that the deployed app-server actually loads and uses the named role policies; files and prompts alone are not proof of role execution.

## Repository layout

```text
docs/       architecture, operations, onboarding, security, and recovery
projects/   non-secret per-project profiles
runtime/    host-side preparation, workflow rendering, lifecycle hooks
schemas/    machine-readable profile contracts
scripts/    deployment and operator commands
tests/      infrastructure regression tests
workflow/   generic architect policy and role policies rendered into deployments
workflow/agents/  project-scoped Codex custom-agent policies
```

## Tests

The infrastructure tests use Python's standard `unittest` runner:

```bash
python3 -m unittest tests.test_infrastructure
```

Lifecycle and security changes should carry focused regression coverage.

## Security model

Secrets do not belong in Git, project profiles, generated workflows, issue comments, workpads, logs, or recovery archives. Deployment and workspace preparation fail closed when required credentials, repositories, upstream state, toolchains, clean-worktree conditions, or publication preflight are unavailable.

See [docs/SECURITY.md](docs/SECURITY.md) for the trust boundary.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [Project onboarding](docs/PROJECT_ONBOARDING.md)
- [Human onboarding contract](docs/HUMAN_ONBOARDING.md)
- [Codex onboarding contract](docs/CODEX_ONBOARDING.md)
- [Security](docs/SECURITY.md)
- [Recovery](docs/RECOVERY.md)

The upstream Symphony lifecycle specification remains authoritative for Symphony itself. This repository defines a reusable host-side policy and deployment layer around it.

## Contributing

Keep generic infrastructure generic. Project-specific meaning belongs in the target repository and issue, while trust-boundary or credential decisions that are not mechanically determined should be surfaced rather than guessed.

## License

Apache License 2.0. See [LICENSE](LICENSE).
