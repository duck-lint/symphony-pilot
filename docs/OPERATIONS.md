# Operations

All physical lifecycle operations run in the WSL/Linux operator environment. Native Windows validation must not fabricate a WSL home or touch Linux state.

## Before start

1. Validate the complete project registry, including `trusted_dispatchers`.
2. Provision or explicitly adopt the host-side Ed25519 publication key, then
   bind its public key to exactly one writable GitHub deploy key:
   `python3 scripts/provision_publication_key.py --project <slug>` followed by
   `python3 scripts/task.py bind-publication-key --project <slug>`. The Runtime
   scheduler does not receive a GitHub API credential.
3. Deploy from a clean reviewed source checkout.
4. Build the reviewed `symphony-runtime` repository, then pin and review the owned Symphony runtime, Codex, and unshare executable identities with `scripts/pin_runtime.py --project <slug>`.
5. Configure one active GitHub repository ruleset targeting `~DEFAULT_BRANCH`, with a `pull_request` rule and no `bypass_actors`.
6. Run the real WSL namespace constructor, supervisor-teardown/CPU probes, and hostile-fixture probes. Treat aggregate persistent-workspace disk growth as an unresolved resource boundary.

Task mutation is explicit and host-only:

    python3 scripts/task.py create --project <slug> --title "..." --objective "..."
    python3 scripts/task.py queue --project <slug> --task T-000001
    python3 scripts/task.py list --project <slug>
    python3 scripts/task.py show --project <slug> --task T-000001
    python3 scripts/task.py publish --project <slug> --task T-000001

Start verifies deployment coherence, runtime lock, containment capability, and
the host-side protection preflight before launching a process. It does not
query GitHub Issues, count dispatch labels, or inject a tracker credential into
Runtime. Any failed check is an infrastructure block; no old execution path is
selected.

## Step-6 lifecycle operations

The managed lifecycle workflow renders `agent.max_turns: 1`: one Runtime
dispatch is one bounded Architect attempt, followed by host reconciliation.
The profile's historical `max_turns` value is not the lifecycle continuation
mechanism. Inspect lifecycle state, workpad, role runs, findings, blockers, and
events with `task show`; inspect only open blockers with `task blockers`.

Malformed or missing results, stale attempts, dirty workspaces, and failed
Git-truth checks are fail-closed. When SQLite is available, reconciliation
finishes the Architect attempt and records an infrastructure blocker. Resolve
one inspected blocker with the exact project-scoped `task resolve-blocker`
command, then the retained active lifecycle state becomes routable naturally.
Do not delete lifecycle evidence or repair a task by manually setting its
state. A task at ARCHIVIST is intentionally parked until the explicit Step-7
publication command. Publication derives repository, branch, base, HEAD,
credentials, ruleset evidence, and PR identity from host state; it never
consumes model publication prose. A failed publication preserves external
recovery evidence and records an infrastructure blocker.

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
projects/*/profile.toml files, host SQLite task rows, GitHub issues/PRs/workpads
as historical or deferred publication records, and only an explicitly accepted
unique unpublished source artifact.

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
`bypass_actors` is empty. The publication deploy key may push only derived task
branches; it has no Issues/PR API or merge authority. Configure these settings
before canary admission and leave human merge as an explicit GitHub action.
