# Architecture
# Architecture

symphony-pilot is a reusable host-side control plane around the official
OpenAI Symphony runtime. It does not contain target-project semantics.

The durable state boundary is:

    GitHub issue/comments/labels + remote issue branch + draft PR
      -> host preparation
      -> clean WSL-native issue workspace
      -> official Symphony
      -> Codex architect
      -> bounded built-in worker

A project profile supplies repository identity, non-secret paths, labels, limits,
and Codex settings. runtime/prepare_workspace.py resolves issue-specific
initial or continuation state. The first run uses the issue-authorized starting
SHA; a continuation uses the existing remote issue branch and checks ancestry
against the required base. Local mutable workspaces are execution state, never
the continuation checkpoint.

runtime/render_workflow.py emits the official Symphony WORKFLOW.md for a
profile. scripts/deploy.py copies reviewed runtime and policy into an atomic
deployment directory and records source hashes in DEPLOYMENT.json. The
architect policy is generic; the issue body and target repository remain the
authority for project work.

The host owns Git, recovery archives, toolchain discovery, publication
preflight, and credential isolation. The architect owns issue interpretation,
authority review, bounded delegation, evidence review, and the issue workpad.
The target repository owns semantic decisions.

Upstream runtime: OpenAI openai/symphony Elixir reference implementation.
Upstream lifecycle authority: OpenAI SPEC.md.
Local policy layer: symphony-pilot.
No maintained fork of Symphony unless a documented upstream incompatibility forces one.
