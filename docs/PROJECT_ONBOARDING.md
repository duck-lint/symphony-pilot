# Project onboarding

1. Copy the profile shape from projects/cleanroom/profile.toml.
2. Set repository, SSH remote, WSL-native workspace/state/log roots, labels,
   concurrency, and toolchain requirements for the new project.
3. Validate the TOML profile and review schemas/project-profile.schema.json.
4. Commit the profile and policy changes. No secret belongs in Git.
5. Provision ~/.config/symphony-pilot/secrets/<slug>/<reference> with mode
   0600 using the project's tracker credential.
6. Run scripts/deploy.py --profile ... and inspect DEPLOYMENT.json.
7. Create a harmless issue labeled with the profile dispatch label. Verify the
   issue-specific workspace, preparation marker, architect/worker handoff,
   workpad, completion event, and cleanup.
8. Only then authorize real work.

The issue body remains the work order. The profile does not encode project
architecture, phase authority, semantic boundaries, or acceptance criteria.
Those remain in the target repository and issue.
