# Operations

Run from the pilot source checkout. Every ordinary source operation names a registered project slug; no command defaults to CLEANROOM.

```bash
python3 scripts/validate_profile.py
python3 scripts/list_projects.py
python3 scripts/list_projects.py --suggest-dashboard-port
python3 scripts/provision_secret.py --project example-four
python3 scripts/deploy.py --project example-four --dry-run
python3 scripts/deploy.py --project example-four
python3 scripts/project.py --project example-four test
python3 scripts/project.py --project example-four start
python3 scripts/project.py --project example-four status
python3 scripts/project.py --project example-four finish
python3 scripts/project.py --project example-four stop
python3 scripts/project.py --project example-four stop-now
```

`validate_profile.py` without a path validates the complete registry. The registry is presence in `projects/<slug>/profile.toml`, not a second database or a registration command. `register_project.py` was removed because it had no truthful durable registration role.

Onboarding a new project is:

1. From the source checkout, run `python3 scripts/list_projects.py --suggest-dashboard-port` and persist the returned unused `dashboard_port` in `projects/example-four/profile.toml`.
2. Run `python3 scripts/validate_profile.py` and review the whole registry.
3. Have the operator provision `~/.config/symphony-pilot/secrets/example-four/github.token` with `python3 scripts/provision_secret.py --project example-four`.
4. Run the deployment dry-run, deploy, and `test` action.
5. Enable dispatch only after a harmless end-to-end project canary proves the actual app-server role handoff and sandbox boundaries.

Normal project deployment always uses the derived slug namespace. The internal Python deployment function accepts a test-only destination parameter; the ordinary source CLI does not expose it. The shared executable must already exist on `PATH` or be named by `SYMPHONY_BIN`; deployment never downloads, copies, preserves, or migrates it.

Registry validation and port allocation may run under native Windows Python;
the supported persisted dashboard allocation domain is TCP `1024–65535`.
The read-only deployment dry-run may also run under native Windows Python.
Actual deployment, secret provisioning, `project.py` lifecycle commands
(`test`, `start`, `status`, `finish`, `stop`, and `stop-now`), internal runtime
hooks, and physical workspace/state operations must run from the WSL/Linux
operator environment. If an unrelated host process occupies the persisted
dashboard port, startup fails with a port-conflict diagnostic and does not
renumber the project. The Windows account name is not used to construct
`/home/...` paths.

Canonical project authority requires the complete valid registry for new work,
start, test, deploy, and secret provisioning. A registry defect cannot grant
new authority, but it also does not strand an already-managed process: `status`
and `stop-now` may use only that project's persisted recovery identity.

`finish` drains active work before stopping. `stop` refuses active work and `stop-now` is the emergency path. PID, awake-guard, lock, recovery, and log state are project-derived. Tracker requests contain only the selected profile's repository and dispatch labels. A shared label string in another repository is not dispatchable by this instance.

Before real unattended work, a harmless canary must prove the actual app-server role handoff, including a requested sentinel mutation that is mechanically denied with a runtime denial/error for reviewer/adversary sandbox testing; voluntary non-editing is not sandbox evidence. After this change is merged, CLEANROOM's one-time manual cutover is described in `docs/RECOVERY.md`. Do not delete or move existing host state during source validation, and do not start `symphony-canary` as part of this architecture work.
