# Human onboarding

This document is a reusable assistant-facing contract for onboarding a target repository onto `symphony-pilot` while asking the human operator only for decisions that genuinely require human authority.

The generic Symphony lifecycle, workspace recovery, workpad mechanics, secret isolation, retry handling, publication preflight, deployment mechanics, and process management already belong to `symphony-pilot`. Do not ask the human to redesign or manually reproduce them.

## Human operator role

Treat the person performing onboarding as the **HUMAN OPERATOR**.

The human resolves project or trust-boundary decisions that cannot be established from repository or control-plane authority. Mechanical configuration should be derived and implemented by the onboarding agent whenever the accepted contracts determine it.

Never ask the human to paste passwords, personal access tokens, private keys, or other secret values into chat, Git, issues, pull requests, or workpads.

## 1. Establish project identity

Determine from repository context when possible; otherwise ask for only the missing values:

- GitHub repository URL or `owner/name` identity;
- short project slug;
- public or private repository status;
- whether the intended automation boundary is one repository or spans several repositories.

A current `symphony-pilot` profile binds exactly one `repository` and one `git_remote`. Multi-repository automation is therefore not silently representable as one profile. If the project genuinely spans several repositories, surface the boundary explicitly: use separately justified profiles where that matches the work, or treat broader multi-repository orchestration as a control-plane design decision.

## 2. Establish project authority

Ask the human only for authority facts the target repository does not already settle.

Determine, in order:

- which documents define project requirements or meaning;
- which documents define architecture or runtime behavior;
- which materials are historical, observational, or evidence-only;
- which legacy systems must not become design authority;
- what private/non-repository evidence may be consulted;
- which decisions remain operator-only.

If the repository already defines an authority model, preserve it. Do not replace responsibility-scoped authority with a single global ranking merely for convenience.

If accepted authorities conflict or a missing constitutive decision prevents safe automation, return that decision to the human instead of fabricating a resolution.

## 3. Confirm automation authority only where it differs

Unless target-project authority says otherwise, the pilot may:

- create and continue issue-specific branches;
- push bounded commits through the configured Git remote;
- create or update one draft pull request using the tracker/provider tooling available to Symphony;
- create or update the single persistent issue workpad;
- edit automation labels;
- close an issue only after its own acceptance criteria are independently satisfied.

Human-only by default:

- merge;
- release;
- production deployment;
- credential-scope expansion;
- destructive disposal of unique unpublished work when safe preservation is unavailable;
- constitutive project or trust-boundary decisions not settled by accepted authority.

Do not ask the human to approve ordinary stale-workspace recovery, continuation from a published issue branch, retryable infrastructure recovery, or mechanically determined profile values.

## 4. Establish the private-data boundary

Determine or ask about:

- private files or artifacts workers may read;
- information that must never be committed;
- information that must never enter GitHub issues, comments, pull requests, or workpads;
- production systems workers must not access;
- external services the project is permitted to use.

Do not request secret contents.

The single issue workpad stores durable conclusions, state, and evidence appropriate for GitHub. It is not a secret store and must not contain hidden reasoning or credentials.

## 5. Choose the tracker credential

For an initial or small deployment, prefer a **fine-grained GitHub personal access token** unless there is a concrete operational reason to use a GitHub App. A GitHub App may be preferable for long-lived or multi-project automation when its installation and rotation overhead is justified.

The tracker credential is not the Git transport credential. `symphony-pilot` keeps the tracker credential host-side and uses the configured Git remote—normally SSH—for clone, fetch, push, and publication preflight.

Scope a fine-grained PAT to the selected target repository or repositories only. Derive permissions from the approved automation policy rather than copying a generic broad scope. Under the current default policy:

- **Metadata:** read-only;
- **Issues:** read/write;
- **Pull requests:** read/write when the architect is authorized to create/update draft PRs; read-only is sufficient for control-plane PR discovery alone;
- **Contents:** do not grant write merely for Git branch publication; Git transport is handled by the configured remote. Add Contents access only when a concrete provider-native operation proves it is required.

Do not add Actions, Workflows, Administration, Secrets, Deployments, Packages, organization permissions, or other scopes without a concrete consumer.

Prefer a finite expiration and document rotation expectations. Never ask the human to paste the credential into chat.

## 6. Install the host secret using the pilot helper

After the onboarding agent has created and validated the project profile, use the accepted helper from the `symphony-pilot` checkout:

```bash
python3 scripts/provision_secret.py projects/<project-slug>/profile.toml
```

The helper prompts with hidden input, creates the project secret directory with mode `0700`, writes the credential atomically with mode `0600`, and does not place the value in shell history.

The resolved secret location is:

```text
~/.config/symphony-pilot/secrets/<project-slug>/<secret_reference>
```

For the conventional `secret_reference = "github.token"`, that becomes:

```text
~/.config/symphony-pilot/secrets/<project-slug>/github.token
```

Do not put the credential in `.bashrc`, `.profile`, repository `.env` files, `WORKFLOW.md`, `AGENTS.md`, project profile files, issues/comments, pull requests, or workpads.

## 7. Project isolation

The onboarding agent should derive and register project-isolated values from the profile contract:

- WSL-native workspace root;
- state root and log root;
- process/service identity;
- secret reference;
- optional dashboard port;
- target-project toolchain hint.

Do not ask the human to choose values that can be mechanically derived without changing authority.

Current profiles permit exactly one concurrent agent. This is a control-plane invariant, not a per-project human choice.

The current concrete host tool-discovery implementation preflights Rust (`cargo`, `rustc`, `rustfmt`, and `rustdoc`). Other `toolchain` values may be recorded but are not automatically equivalent to a real toolchain preflight. If a target project requires a different mechanically verified toolchain, treat that as a separate control-plane capability question rather than pretending the hint already proves availability.

## 8. Human stop conditions

Return to the human when:

- project authority is genuinely missing;
- accepted project authorities conflict;
- credential permissions must expand;
- safe recovery cannot prevent destructive loss of unique unpublished work;
- the private-data boundary is unclear;
- a multi-repository requirement cannot be represented by the accepted profile boundary;
- merge is ready;
- release or production deployment is ready;
- the requested work exceeds issue authority.

Do not return to the human merely because:

- a workspace is stale;
- Git metadata is stale;
- a published continuation exists;
- a supported tool is discoverable outside `PATH`;
- a retryable infrastructure failure occurred;
- the same unchanged infrastructure blocker has already been diagnosed.

Those are control-plane responsibilities.

## 9. Final handoff format

Once human-only questions are resolved, return exactly these two sections.

### PROJECT ONBOARDING HANDOFF

Provide only non-secret values that can be passed into the Codex/project-onboarding contract:

- repository;
- project slug;
- repository boundary and any explicit multi-repository constraint;
- authority notes requiring explicit human confirmation;
- automation-policy deviations from defaults;
- private-data policy;
- credential type;
- credential repository scope;
- required tracker permissions;
- secret reference/path, never the value;
- merge policy;
- release/deployment policy;
- remaining human stop conditions.

### MY ACTIONS

List only actions that genuinely require account ownership, security approval, or unresolved project authority, for example:

1. create the approved fine-grained PAT or GitHub App installation;
2. provision it with `scripts/provision_secret.py` after the profile exists;
3. resolve an explicit project-authority decision;
4. review the first harmless canary run;
5. review/merge a completed pull request.

Do not send the human off to edit mechanical profile or workflow files that the onboarding agent can safely create and validate.