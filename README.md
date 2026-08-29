# symphony-pilot

Reusable host-side control plane for OpenAI Symphony architect-worker execution.

The control plane owns disposable execution state and generic lifecycle mechanics. A target project owns its meaning, architecture, repository instructions, validation authority, and human stop conditions.

## Quick start

```text
python3 scripts/validate_profile.py projects/cleanroom/profile.toml
python3 scripts/provision_secret.py projects/cleanroom/profile.toml
python3 scripts/deploy.py --profile projects/cleanroom/profile.toml --dry-run
python3 scripts/deploy.py --profile projects/cleanroom/profile.toml
python3 scripts/project.py --profile projects/cleanroom/profile.toml status
python3 scripts/project.py --profile projects/cleanroom/profile.toml start
```

The deployment reads the project credential from the host secret reference. It never copies the credential into this repository or the deployed workflow. The Codex child launcher removes tracker credential variables before starting App Server.

See `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `docs/OPERATIONS.md`, `docs/PROJECT_ONBOARDING.md`, and `docs/RECOVERY.md`.
