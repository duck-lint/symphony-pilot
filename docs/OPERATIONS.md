# Operations

Run from the pilot source checkout. Every operation names a project or uses an explicit selected profile; no command defaults to CLEANROOM.

```bash
python3 scripts/validate_profile.py
python3 scripts/list_projects.py
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

1. Create `projects/example-four/profile.toml` using the schema and fill only repository-owned policy and identity inputs.
2. Run `python3 scripts/validate_profile.py` and review the whole registry.
3. Have the operator provision `~/.config/symphony-pilot/secrets/example-four/github.token` with `python3 scripts/provision_secret.py --project example-four`.
4. Run the deployment dry-run, deploy, and `test` action.
5. Enable dispatch only after a harmless end-to-end project canary proves the actual app-server role handoff and sandbox boundaries.

`--install-root` is retained only as an explicit non-persisted developer/test override. Normal project deployment always uses the derived slug namespace. The shared executable must already exist on `PATH` or be named by `SYMPHONY_BIN`; deployment never downloads, copies, preserves, or migrates it.

`finish` drains active work before stopping. `stop` refuses active work and `stop-now` is the emergency path. PID, awake-guard, lock, recovery, and log state are project-derived. Tracker requests contain only the selected profile's repository and dispatch labels. A shared label string in another repository is not dispatchable by this instance.

Before real unattended work, a harmless canary must prove the actual app-server role handoff, including a requested sentinel mutation that is mechanically denied with a runtime denial/error for reviewer/adversary sandbox testing; voluntary non-editing is not sandbox evidence. After this change is merged, CLEANROOM's one-time manual cutover is described in `docs/RECOVERY.md`. Do not delete or move existing host state during source validation, and do not start `symphony-canary` as part of this architecture work.
