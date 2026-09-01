# symphony-pilot instructions

This repository is the trusted local host control plane for the
project-owned `symphony-runtime` implementation.

- `symphony-runtime` is the Symphony implementation this project builds and
  runs.
- OpenAI Symphony is historical/reference material only. Upstream compatibility
  is not a requirement, upstream changes are not lifecycle or architectural
  authority, and no upstream-sync workflow is required.
- This repository defines reusable deployment, profile, security, and host
  lifecycle policy around the owned runtime.
- Target project repositories remain authoritative for project meaning, architecture, validation, forbidden areas, and human stop conditions.
- Generic infrastructure must not contain project architecture or semantic policy.
- Secrets never belong in Git, profiles, workflows, issue comments, workpads, logs, or recovery archives.
- Relevant lifecycle and security tests are required for infrastructure changes.
- A trust-boundary or credential decision that is not mechanically determined must be surfaced rather than guessed.
- Keep Git workspaces on the host's native Linux filesystem when running under WSL; use Windows paths only for detected Windows toolchain artifacts.
