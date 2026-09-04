# Architecture

symphony-pilot is a trusted host control plane around the project-owned
`symphony-runtime` lifecycle implementation. Target repositories remain
authoritative for project meaning, validation, private inputs, and human stop
conditions. OpenAI Symphony is historical/reference material, not continuing
lifecycle or architectural authority.

## Authority topology

The host owns the canonical registry, SQLite task rows, local task identity,
publication deploy key, runtime locks, process state, and publication. Issue
prose, workpad prose, task Git metadata, task output, and task filesystem state
are untrusted payload. GitHub remains a deferred publication/lifecycle
integration, not scheduler authority.

The profile supplies project onboarding data: repository, clone remote, any
currently retained host-secret/publication settings, model settings, and
resource preferences. Host paths and publication-key paths are derived from
the project slug. Legacy dispatch fields are not Runtime scheduler input.

## Local task intake and scheduler authority

The trusted operator CLI creates local tasks in the host-wide
`control.sqlite3`. It accepts only a registered project slug, title, and
objective. Pilot resolves the registered Git remote's symbolic `HEAD` and
exact commit with `git ls-remote --symref <remote> HEAD`; ambiguity fails
closed. The database transaction derives the UUID, next unique `T-000001`
identifier, and `codex/t-000001-<12-hex-character-UUID-prefix>` branch.

Creation produces `PREPARED`. A separate `task.py queue` command performs the
accepted compare-and-set transition `PREPARED -> QUEUED` and appends the
`queued` event in the same transaction. Runtime reads SQLite in read-only mode,
scoped to the rendered project slug, and dispatches only unblocked `QUEUED`
rows. No GitHub issue, issue number, dispatch label, or `GH-N` identity is
required or accepted in this causal chain.

The workspace sequence is:

```text
PREPARED
   ↓ explicit host queue
QUEUED
   ↓ Runtime SQLite scheduler
T-000001 workspace
```

## Protection and publication

The supported GitHub protection contract is one active repository ruleset
targeting `~DEFAULT_BRANCH` (or the exact default ref), containing one
`pull_request` rule and an empty `bypass_actors` list. Classic branch-protection
normalization and invented human-actor fields are not supported.

Publication uses the deterministic host secret
`~/.config/symphony-pilot/secrets/<slug>/publication-ssh-key`, mode 0600. It is
separate from the Issues/PR tracker token, human account, and task domain.

The prior task outbox and broker remain deferred lifecycle/publication source
code. Step 5 does not activate them: the rendered `after_run` script returns
78 and performs no legacy GitHub lifecycle mapping, so no local `T-N` task is
mapped to a GitHub issue or publication record here. Frozen Runtime treats
after_run hook failures as best-effort; this return code is not an activation
barrier.

The prior ready_for_human_merge bundle/publication path remains a deferred
Step-6/Step-7 seam. Step 5 does not persist full lifecycle state, publish
branches, create draft PRs, or map local tasks to GitHub records. The rendered
after_run script only signals that reconciliation is unavailable. Actual
Step-5 safety depends on the existing execution-capability gate. Step 6 must
establish lifecycle reconciliation before Step 8 may authorize unattended
execution.

## Execution truth states

| Boundary | State | Evidence |
|---|---|---|
| Linux user/mount/PID/network namespace primitive | PROVEN | real WSL `unshare` probe |
| Synthetic task filesystem constructor | PROVEN | hostile fixture; no broad `/etc` mount |
| Supervisor child teardown and CPU bound | PROVEN BY FIXTURE | shared `--kill-child=SIGKILL` runner |
| Aggregate persistent-workspace disk quota | UNBOUNDED | no generic quota authority in this cutover |
| Local SQLite task intake / queue authority | TARGETED / FIXTURE-VERIFIED | trusted task CLI, transactionally allocated identity, and PREPARED-to-QUEUED CAS tests |
| Runtime SQLite scheduler contract | ACCEPTED CONTRACT / LIVE PROOF BLOCKED | frozen Runtime adapter contract; Pilot fixture and workflow pair await the canonical artifact smoke because WSL is unavailable |
| Legacy GitHub admission / dispatch provenance | RETIRED / NOT MANAGED | historical parser/provenance source is outside the deployed Step-5 scheduler path |
| Host broker and publication transfer | DEFERRED / NOT ACTIVE | source-only lifecycle/publication seam reserved for Steps 6 and 7 |
| Ruleset protection parser | PROVEN BY FIXTURES | real API-shaped fixtures |
| Codex App Server activation | BLOCKED | existing execution-capability gate and launch boundary remain fail-closed |
| Live Codex task containment | NOT YET PROVEN | no Codex task was started |
| Multi-role canary and live cutover | NOT RUN | explicitly prohibited for this PR |

## Containment

The selected backend is rootless Linux/WSL `unshare`. The constructor creates a
fresh tmpfs root, mounts only the current workspace, task home, read-only
admission inbox, writable fixed outbox, bounded tmpfs, minimal devices, and
read-only runtime libraries, then mounts a task PID namespace and restricted
network namespace. It does not mount host `/etc`; the current explicit
allowlist is empty. The shared runner uses util-linux `--kill-child=SIGKILL`,
reaps the supervisor after timeout, and the fixture verifies a child and
grandchild stop modifying a task sentinel. CPU time, process count, address
space, open files, individual file size, task tmpfs, and wall clock are
bounded. Aggregate writes to the persistent task workspace are not quota
bounded, so unattended activation remains blocked. The constructor is
exercised independently of Codex; the auth blocker still stops the real
launcher before App Server start.
