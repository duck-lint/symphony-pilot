# Human onboarding

Create one non-secret `projects/<slug>/profile.toml` with repository, clone
remote, required dispatch labels, `trusted_dispatchers`, blocked label, and
model/resource settings. Validate the complete registry before selecting a
project. The trusted dispatcher list contains GitHub actor logins allowed to
perform the server label event that admits work; it is not a generic hard-coded
runtime identity.

Provision two separate host secrets under WSL/Linux:

```text
~/.config/symphony-pilot/secrets/<slug>/github.token
~/.config/symphony-pilot/secrets/<slug>/publication-ssh-key
```

The tracker token is for GitHub Issues/PR API calls. The publication key is a
repository-scoped write deploy key used only for derived branch pushes. Do not
reuse a personal key or SSH agent, and never print either secret.

Configure one active GitHub repository ruleset targeting the default branch.
It must contain a pull-request rule and no bypass actors. Do not configure
this automatically from the pilot. Keep the human GitHub account as the merge
authority; the pilot has no merge API.

Run runtime pinning, deployment, profile validation, the hostile fixture, and
the full test suite before any canary. The current auth blocker prevents real
execution, so no canary or live cutover is authorized by this PR.
