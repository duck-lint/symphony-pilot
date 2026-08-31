# Human onboarding

Register a project with a non-secret canonical profile, provision its host-side tracker credential, pin the reviewed runtime identities, and configure server-side protection on the default branch.

The required GitHub contract is:

- default/release branch changes require a pull request;
- the publication identity can update issue branches only;
- the publication identity cannot bypass default-branch protection;
- a separate human authority merges.

Do not paste secrets into chat, Git, issues, pull requests, workpads, profiles, or logs. Do not authorize a project whose protection metadata is unavailable. Do not run the canary until hostile containment tests pass.

The one-time cutover is destructive. Stop all old project processes, preserve only source, canonical profiles, GitHub historical records, and an explicitly accepted unique unpublished artifact, then delete old task workspaces, role homes, generated deployments, caches, logs, and stale recovery state. Rotate credentials that were readable under the previous same-user design.
