# Human onboarding

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

The retained host token is for the trusted host GitHub publication API calls;
it is not passed to Runtime's SQLite scheduler. The publication key is a
repository-scoped write deploy key used only for derived branch pushes. Do not
reuse a personal key or SSH agent, and never print either secret.

Configure one active GitHub repository ruleset targeting the default branch.
It must contain a pull-request rule and no bypass actors. Do not configure
this automatically from the pilot. Keep the human GitHub account as the merge
authority; the pilot has no merge API.

Build the owned `symphony-runtime` repository and run runtime pinning,
deployment, profile validation, the hostile fixture, and
the full test suite before any canary. The fixture proves namespace teardown,
CPU enforcement, and the empty `/etc` allowlist, but aggregate growth of the
persistent task workspace remains unbounded. Along with the current auth
blocker, that means unattended execution is not activatable; no canary or
live cutover is authorized by this PR.
