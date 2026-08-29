# Project onboarding

Use this checklist for the mechanical control-plane side of onboarding.

For the two reusable role-specific contracts, see:

- [Human onboarding](HUMAN_ONBOARDING.md) — determines only human-authority inputs and credential actions.
- [Codex onboarding](CODEX_ONBOARDING.md) — inspects target-project authority and registers the project without duplicating pilot infrastructure.

## Mechanical onboarding sequence

1. Establish the target project's authority, validation, private-data, and human-stop contracts from target-repository evidence.
2. Copy the profile shape from `projects/cleanroom/profile.toml` and create `projects/<slug>/profile.toml`.
3. Set the single target repository, SSH Git remote, WSL-native workspace/state/log roots, labels, service identity, supported toolchain hint, and optional host-integration settings. Current profiles require `max_concurrent_agents = 1`.
4. Validate the TOML profile with `python3 scripts/validate_profile.py projects/<slug>/profile.toml` and review `schemas/project-profile.schema.json`.
5. Commit the non-secret profile and any bounded policy/documentation changes. No secret belongs in Git.
6. Have the human provision the tracker credential with `python3 scripts/provision_secret.py projects/<slug>/profile.toml`. The helper writes the resolved host secret under `~/.config/symphony-pilot/secrets/<slug>/<reference>` with the required permissions.
7. Run `scripts/deploy.py --profile ... --dry-run`, then deploy and inspect `DEPLOYMENT.json`.
8. Run the deployed `test` action.
9. Create one harmless target-project issue with an explicit authorized starting ref/SHA and the profile dispatch label. Verify the project-specific integration: correct repository/profile, issue workspace, preparation marker, single workpad, architect/worker handoff, supported toolchain preflight, publication preflight, completion/handoff behavior, and cleanup.
10. Only then authorize real unattended work.

Do not make the canary re-prove generic stale-workspace recovery, unique-state archiving, retry/circuit-breaker, locking, or credential-isolation mechanics already owned by `symphony-pilot` regression tests. If onboarding exposes a real generic defect, fix that boundary in `symphony-pilot` separately.

The issue body remains the work order. The profile does not encode project architecture, phase authority, semantic boundaries, or acceptance criteria. Those remain in the target repository and issue.

A current profile represents exactly one `repository` and one `git_remote`. Multi-repository orchestration requires an explicit boundary decision rather than an invented profile extension.