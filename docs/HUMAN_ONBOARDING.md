# Human onboarding

## Current supervised-local onboarding

The MVP is proven for an operator-supervised local project. The ordinary path
uses the trusted CLI for task mutation, Pilot SQLite for lifecycle authority,
the read-only loopback UI for inspection, and the installed Codex App Server
after explicit `SYMPHONY_SUPERVISED_LOCAL=1` opt-in.

Create one non-secret `projects/<slug>/profile.toml` with repository, clone
remote, and model/resource settings. Legacy dispatch labels,
`trusted_dispatchers`, and blocked label fields may remain for deferred
publication/lifecycle seams; they are not scheduler authority. Validate the
complete registry before selecting a project.

Provision two separate host secrets under WSL/Linux:

```text
~/.config/symphony-pilot/secrets/<slug>/github.token (host-side only)
~/.config/symphony-pilot/secrets/<slug>/publication-ssh-key
```

The GitHub token is a host-owned credential for authenticated official-API
reads and publication. For a configured public repository, task creation may
resolve repository identity/default branch/exact SHA without this token; the
host first proves the repository is public. A malformed configured credential
still fails closed. The publication key is a repository-scoped write deploy
key used only for derived branch pushes. Do not reuse a personal key or SSH
agent, and never print either secret.

Configure one active GitHub repository ruleset targeting the default branch.
It must contain a pull-request rule and no bypass actors. Do not configure
this automatically from the pilot. Keep the human GitHub account as the merge
authority; the pilot has no merge API.

Build the owned `symphony-runtime` repository and run runtime pinning,
deployment, profile validation, the hostile fixture, and the full test suite
as appropriate for the operating mode. The supervised-local canary is
already proven. Aggregate quota/EDQUOT enforcement and unattended credential
isolation remain hardening requirements for unattended activation, not
blockers to the supervised MVP.
