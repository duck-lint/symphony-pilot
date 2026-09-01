# Operations

All physical lifecycle operations run in the WSL/Linux operator environment. Native Windows validation must not fabricate a WSL home or touch Linux state.

## Before start

1. Validate the complete project registry, including `trusted_dispatchers`.
2. Provision the API-only tracker token and derived publication deploy key.
3. Deploy from a clean reviewed source checkout.
4. Pin and review the official Symphony, Codex, and unshare executable identities with scripts/pin_runtime.py --project <slug>.
5. Configure one active GitHub repository ruleset targeting `~DEFAULT_BRANCH`, with a `pull_request` rule and no `bypass_actors`.
6. Run the real WSL namespace constructor, supervisor-teardown/CPU probes, and hostile-fixture probes. Treat aggregate persistent-workspace disk growth as an unresolved resource boundary.

start verifies deployment coherence, runtime lock, containment capability, credentials, real ruleset protection, and dispatch cardinality before launching a process. Any failed check is an infrastructure block; no old execution path is selected.

## Ordinary controls

    python3 scripts/validate_profile.py
    python3 scripts/deploy.py --project <slug>
    python3 scripts/project.py --project <slug> start
    python3 scripts/project.py --project <slug> status
    python3 scripts/project.py --project <slug> finish

stop-now is the bounded emergency process control. It does not infer task identity from issue prose and does not authorize publication.

## Destructive one-time cutover

Do not run this from repository tests and do not perform it automatically.

PRESERVE: the reviewed symphony-pilot source checkout, canonical
projects/*/profile.toml files, GitHub issues/PRs/workpads as historical records,
and only an explicitly accepted unique unpublished source artifact.

DELETE: after every old Symphony project process is stopped and unique work is
accounted for, delete old task workspaces, task .git directories, role homes,
role-home leases, generated deployments, project logs, stale state, recovery
archives, caches, temporary homes, and continuation markers. Do not migrate
these merely because they exist.

ROTATE: project tracker credentials, publication credentials, and any dedicated
Codex runtime credential that was readable under the previous same-user design.
Never print or copy credential contents.

REGENERATE: deployments, runtime locks, host task records, process state, logs,
and fresh execution domains from the reviewed source and accepted registry.

CONFIGURE REMOTELY: protect the default branch of every registered repository
with one active repository ruleset whose `conditions.ref_name.include` targets
`~DEFAULT_BRANCH`, whose `rules` contains `{"type":"pull_request"}`, and whose
`bypass_actors` is empty. The publication deploy key may push only derived issue
branches; it has no Issues/PR API or merge authority. Configure these settings
before canary admission and leave human merge as an explicit GitHub action.
