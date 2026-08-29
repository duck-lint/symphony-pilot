# symphony-pilot instructions

This repository is infrastructure for OpenAI Symphony deployments.

- The official OpenAI Symphony specification is the upstream lifecycle authority.
- This repository defines reusable deployment, profile, security, and lifecycle policy on top of that specification.
- Target project repositories remain authoritative for project meaning, architecture, validation, forbidden areas, and human stop conditions.
- Generic infrastructure must not contain project architecture or semantic policy.
- Secrets never belong in Git, profiles, workflows, issue comments, workpads, logs, or recovery archives.
- Relevant lifecycle and security tests are required for infrastructure changes.
- A trust-boundary or credential decision that is not mechanically determined must be surfaced rather than guessed.
- Keep Git workspaces on the host's native Linux filesystem when running under WSL; use Windows paths only for detected Windows toolchain artifacts.
