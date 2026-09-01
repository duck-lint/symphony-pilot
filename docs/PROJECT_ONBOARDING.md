# Project onboarding

The canonical registry is the tracked `projects/` directory. A registered project is exactly one `projects/<slug>/profile.toml` whose `slug` matches its directory name and whose non-secret fields pass profile validation.

Create a profile with repository, Git remote, tracker secret reference, non-empty trusted dispatcher logins, dispatch and blocked labels, an allocated dashboard port, execution limits, Codex settings, toolchain hint, and optional notification/sleep preferences. Do not add host paths, publication-key paths, or service names: deployment, workspace, state, logs, credentials, process state, locks, workflow location, publication-key location, and service identity are derived from the slug.

```bash
mkdir -p projects/example-four
python3 scripts/list_projects.py --suggest-dashboard-port
$EDITOR projects/example-four/profile.toml
python3 scripts/validate_profile.py
python3 scripts/list_projects.py
python3 scripts/provision_secret.py --project example-four
python3 scripts/provision_publication_key.py --project example-four < publication-key.pem
python3 scripts/deploy.py --project example-four --dry-run
python3 scripts/deploy.py --project example-four
python3 scripts/project.py --project example-four test
```

The profile's repository is globally unique in the registry. This is a deliberate tracker-isolation rule: identical label text is safe across different repositories, but not across two profiles targeting one repository. Registry validation also rejects duplicate dashboard allocations, derived resource equality, and path containment. Adding or removing a profile changes only registry membership; generic implementation remains unchanged.

The project repository and issue remain authoritative for architecture, acceptance criteria, private inputs, and human stop conditions. The pilot owns only reusable host mechanics. A harmless live canary is required before real dispatch, and `symphony-canary` is just another profile when it is onboarded.
