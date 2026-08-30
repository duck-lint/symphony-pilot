# Codex onboarding

This document is a reusable Codex-facing contract for registering one target repository with an existing `symphony-pilot` deployment.

Do **not** build or duplicate generic Symphony lifecycle infrastructure in the target repository. The reusable control plane already owns workspace preparation/reconciliation, continuation from published issue branches, clean-tree verification, recovery archives, preparation markers, locking/process cleanup, publication preflight, retry/circuit-breaker behavior, workpad mechanics, tracker-credential isolation, project/service isolation, deployment/runtime lifecycle, and supported host tool discovery.

Your task is to establish only the target project's authority/validation contract and register the project with the existing control plane.

## 1. Read control-plane authority first

Locate the installed or checked-out `symphony-pilot` repository and read, at minimum:

- root `AGENTS.md`;
- `docs/PROJECT_ONBOARDING.md`;
- `docs/SECURITY.md`;
- `schemas/project-profile.schema.json`;
- `workflow/architect_policy.md`;
- the current reusable workflow renderer and any directly relevant deployment documentation.

The official OpenAI Symphony specification remains the upstream lifecycle authority. `symphony-pilot` is the local reusable policy/deployment layer around it.

Do not modify generic lifecycle code merely because the target project has a project-specific requirement. If onboarding exposes a real control-plane defect, classify it separately and make the smallest justified infrastructure change in `symphony-pilot`, not a forked workaround in the target repository.

## 2. Inspect the target repository

Determine from repository evidence:

- repository identity and default branch;
- root instructions such as `AGENTS.md`;
- specification/design/architecture documents;
- accepted decisions and implementation contracts;
- historical/evidence-only documents;
- generated artifacts;
- private-data boundaries;
- legacy systems that must not become design authority;
- build/test/lint/typecheck/schema commands;
- language/toolchain dependencies;
- branch and pull-request conventions;
- CI/release/deployment behavior.

Do not assume implementation is architectural authority merely because it exists.

If the project already defines an authority model, preserve it. If authority is scoped by responsibility rather than one global hierarchy, preserve that structure.

## 3. Establish the project authority manifest

Determine whether the target repository already tells an architect:

- what must be read before work;
- what decides project meaning or requirements;
- what decides architecture/runtime behavior;
- what is evidence/history only;
- what is forbidden or legacy;
- how ambiguity or authority conflict is handled;
- required validation;
- private-data restrictions;
- branch/publication expectations;
- human escalation conditions;
- merge/release/deployment policy.

Prefer minimally amending an existing root `AGENTS.md` or equivalent. Do not create a competing authority system.

If a genuinely missing project decision prevents safe automation, stop and report the missing decision rather than fabricating it.

## 4. Automation actors

Use these roles.

### ARCHITECT / ORCHESTRATOR

The parent Codex session owns issue interpretation, authority integration,
decomposition, role routing, adjudication, review sequencing, publication,
workpad state, and final acceptance. Its inspection of worker output is
triage, not independent acceptance of its own plan.

### PROJECT-MANAGER and PLANNER

Project-manager is read-only/advisory and establishes admissibility, authority
boundaries, affected and non-affected surfaces, unresolved decisions, and stop
conditions. Planner is read-only with respect to project source and turns the
admissible objective into bounded seams, acceptance criteria, and a
verification contract. Neither role defines missing project semantics.

### IMPLEMENTER

The mutating role performs only an architect-authorized implementation seam,
returns evidence, and does not independently expand scope. Corrections use a
fresh implementer instance and invalidate prior acceptance of the old HEAD.

### REVIEWER and ADVERSARY

Both roles are read-only with respect to project source. Reviewer checks
conformance with the issue, authority, plan, verification contract, and tests.
Adversary independently tries to falsify the completeness claim using the
cheapest grounded checks. Findings return to architect adjudication and do not
become new GitHub work orders automatically.

These contracts define the reusable policy boundary. They do not by themselves
prove that the installed Symphony/Codex app-server can select and isolate
named roles. The deployed launcher preflights and supplies the role pack; a
harmless live canary must establish actual role handoffs before that capability
is described as operational.

### ARCHIVIST

The continuity role produces a bounded archival packet containing accepted
lifecycle conclusions, review history, decisions, validation evidence,
limitations, publication state, and final status. The architect adjudicates
that packet and alone persists accepted durable state to the existing workpad
or explicitly authorized project surface. The archivist does not mutate the
workpad, invent decisions, or create a `harness/` directory.

### HUMAN OPERATOR

The human resolves missing constitutive/project decisions, credential/trust-boundary decisions, destructive unique-state decisions not safely recoverable by the pilot, and merge/release/deployment unless explicitly delegated.

## 5. Issue contract

Use the existing dispatch convention unless accepted target-project authority requires a difference:

```text
symphony:auto
symphony:human
```

Each issue is one schedulable unit.

Issue-specific starting refs/SHAs are authoritative for the initial dispatch. A published remote issue branch is authoritative for continuation. Do not invent a global development baseline when the project does not define one.

The current pilot defaults an initial branch to:

```text
codex/gh-<issue>-work
```

and recognizes published continuation branches/PRs under the corresponding `codex/gh-<issue>-...` namespace.

## 6. Workpad contract

Use exactly one persistent issue workpad carrying:

```text
<!-- symphony-workpad:v1 -->
```

Do not create a second workpad implementation.

The workpad stores durable issue state, conclusions, implementation/validation evidence, publication state, limitations, and human blockers. It does not store hidden reasoning or credentials.

Respect the target project's private-data rules for anything written to GitHub.

## 7. Validation contract

Establish the exact mechanical validation required before publication/completion from target-repository authority.

Examples may include:

- formatting;
- lint;
- unit/integration tests;
- type checks;
- schema validation/export;
- generated-artifact idempotence;
- documentation builds.

Do not invent expensive acceptance requirements unrelated to accepted project authority.

Where the project distinguishes synthetic/mechanical validation from real-data, production, or private-UAT acceptance, preserve that distinction. A successful mechanical test does not silently acquire authority it does not have.

## 8. Publication policy

Default unless target-project authority says otherwise:

- issue-specific branches;
- bounded commits;
- one draft PR per issue;
- continuation from the published issue-branch HEAD;
- no force-push;
- no auto-merge;
- merge remains human;
- release/deployment remains human.

Record project-specific differences explicitly.

Git source publication uses the profile's configured `git_remote`, normally SSH. Do not treat the host tracker token as ordinary Git authentication.

## 9. Private data and external systems

Identify:

- private files workers may read;
- data that must never appear in Git;
- data that must never appear in GitHub comments/workpads/PRs;
- external services workers may access;
- credentials that must remain host-side;
- generated artifacts;
- production/deployment systems that remain human-only.

Do not expose secret values.

## 10. Toolchain

Identify stable toolchain requirements from target-repository evidence and record the supported hint in the project profile.

The current concrete pilot preflight is implemented for Rust and checks `cargo`, `rustc`, `rustfmt`, and `rustdoc`, including supported Windows-toolchain discovery from WSL. A non-Rust `toolchain` profile value does not by itself prove that the required tools were mechanically preflighted.

If the target project requires a different host-level toolchain preflight for safe unattended work, identify that as a control-plane capability requirement and implement it in `symphony-pilot` only when supported by concrete project evidence.

## 11. GitHub tracker credential

Determine the minimum tracker permissions required by the approved automation policy.

Prefer a fine-grained GitHub PAT for an initial/small deployment unless there is a concrete reason to use a GitHub App.

The tracker credential is host-only. Symphony/provider-native tracker tools may use it, while the Codex child must not receive the raw tracker credential. Git clone/fetch/push uses the configured Git remote separately.

For the current default policy, begin from:

- selected target repository only;
- Metadata: read-only;
- Issues: read/write;
- Pull requests: read/write when the architect is authorized to create/update draft PRs; otherwise read-only is enough for control-plane PR discovery;
- Contents: no write permission merely for Git branch publication. Add Contents access only when a concrete provider-native operation proves it is required.

Do not request Actions, Workflows, Administration, Secrets, Deployments, Packages, organization permissions, merge authority, or additional scopes by convenience.

Never ask the human for the credential value. Return only the secret reference/location that must be provisioned through `symphony-pilot`.

## 12. Register the project

Create the target profile under:

```text
projects/<project-slug>/profile.toml
```

using the accepted schema.

A current profile represents exactly one `repository` and one `git_remote`. Do not invent a `repository_set` field or silently collapse a multi-repository project into one profile. If the project genuinely requires multi-repository orchestration, surface that as an explicit boundary decision.

Profiles contain non-secret configuration only. Register project-isolated values for:

- `slug`;
- `repository`;
- `git_remote`;
- WSL-native derived workspace, state, and log namespaces;
- `secret_reference`;
- dispatch and blocked labels;
- derived service identity (not profile-configurable);
- Codex model/reasoning settings;
- allocated dashboard port and host integration preferences;
- target toolchain hint.

The current control plane requires:

```text
max_concurrent_agents = 1
```

This is not a project-specific tuning choice.

Validate the profile with:

```bash
python3 scripts/validate_profile.py --project <project-slug>
```

If changes are needed in both repositories, keep commits bounded and separated by repository responsibility.

## 13. Secret provisioning handoff

If the required host secret does not yet exist, stop at the human credential boundary and return the exact profile path and resolved secret reference.

The human should provision it from the `symphony-pilot` checkout with:

```bash
python3 scripts/provision_secret.py --project <project-slug>
```

Do not replace this with repository `.env` files, shell startup variables, workflow secrets embedded in Git, or ad hoc secret-copying logic.

## 14. Onboarding verification

Do not turn every project onboarding into a duplicate test suite for generic pilot lifecycle mechanics.

Generic recovery, locking, circuit-breaker, credential-isolation, and process behavior belongs to `symphony-pilot`'s infrastructure regression tests. Run the relevant pilot tests when infrastructure changes are made or when onboarding exposes a suspected generic defect.

For the target project, verify the integration boundary in this order:

1. target-project authority is locatable and sufficient;
2. profile validates without reading the secret;
3. deployment dry-run renders the expected project-specific deployment;
4. the host secret is provisioned by the human when required;
5. deployment and deployed `test` action succeed;
6. a harmless project-specific canary issue explicitly identifies its authorized starting ref/SHA and uses the dispatch label;
7. the canary selects the correct repository and project profile;
8. exactly one workpad is created/updated;
9. the requested starting state is honored;
10. the issue branch and continuation behavior use published remote state;
11. the required supported toolchain preflight succeeds;
12. Git publication dry-run succeeds through the configured remote;
13. the Codex child does not receive the raw tracker credential;
14. no target-project source is changed merely to prove generic infrastructure behavior;
15. no auto-merge occurs.

The canary should exercise the real project integration, not intentionally manufacture stale workspaces, unique unpublished state, repeated blockers, or other generic recovery scenarios already covered by pilot tests.

If the canary exposes a genuine generic defect, record the evidence and fix the smallest responsible boundary in `symphony-pilot` separately.

## 15. Human stop conditions

Move the issue to `symphony:human` and stop autonomous work for genuine human decisions such as:

- project authority materially underspecified;
- accepted project authorities conflict;
- credential scope must expand;
- safe preservation cannot prevent destructive loss of unique unpublished work;
- private-data policy requires a decision;
- a multi-repository requirement exceeds the accepted profile boundary;
- merge/release/deployment requires approval;
- requested work exceeds issue authority.

Do not escalate automatically recoverable infrastructure failures merely because they are inconvenient. The host recovery/circuit-breaker path owns them.

## 16. Deliverables

Return the onboarding result under these headings.

### Project authority

- authority map;
- repository-instruction changes;
- unresolved project decisions.

### Project profile

- profile path;
- repository and Git remote;
- workspace/state/log roots;
- service identity;
- secret reference;
- toolchain hint and whether it has a real host preflight;
- labels;
- fixed concurrency (`1`);
- allocated dashboard port.

### Credential requirement

- recommended credential type;
- repository scope;
- exact minimum tracker permissions;
- secret reference/path;
- no secret value.

### Execution contract

- workpad behavior;
- initial/continuation behavior;
- validation;
- branch/PR policy;
- human escalation;
- private-data restrictions.

### Canary verification

- issue used;
- profile/deployment result;
- preparation result;
- credential-isolation result;
- publication-preflight result;
- project-specific validation result;
- any control-plane defect discovered.

### Human actions remaining

List only actions requiring account ownership, security approval, missing project authority, merge/release/deployment approval, or another genuine human authority boundary.

Do not duplicate `symphony-pilot` runtime code into the target repository.
