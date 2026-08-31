# Architecture

symphony-pilot is a host control plane around the official OpenAI Symphony
lifecycle. Target repositories remain authoritative for project meaning,
validation, private inputs, and human stop conditions.

## Authority topology

The host owns the canonical registry, tracker credential, publication deploy
key, server-derived task admission, runtime locks, process state, workpad
identity, lifecycle labels, draft-PR identity, and publication. Issue prose,
workpad prose, task Git metadata, task output, and task filesystem state are
untrusted payload.

The profile supplies only project onboarding data: repository, clone remote,
dispatch labels, trusted dispatcher logins, blocked label, model settings, and
resource preferences. Host paths and publication-key paths are derived from
the project slug.

## Admission

Before a workpad or task record exists, the host reads the open issue, confirms
all required labels are present, fetches the complete paginated issue-event
history, and proves that the latest transition for every required label is a
`labeled` event performed by a configured trusted dispatcher. The record stores
`dispatch_provenance` entries containing label, actor, event id, and timestamp.
No issue author, comment author, marker, branch, ref, or SHA is accepted as a
substitute.

The host then reads the server default branch and exact HEAD, creates exactly
one workpad comment, and writes `symphony-pilot-task/v1` outside the workspace.
The task branch is derived as `codex/gh-<issue>-<task-id-prefix>`.

## Protection and publication

The supported GitHub protection contract is one active repository ruleset
targeting `~DEFAULT_BRANCH` (or the exact default ref), containing one
`pull_request` rule and an empty `bypass_actors` list. Classic branch-protection
normalization and invented human-actor fields are not supported.

Publication uses the deterministic host secret
`~/.config/symphony-pilot/secrets/<slug>/publication-ssh-key`, mode 0600. It is
separate from the Issues/PR tracker token, human account, and task domain.

The task writes only `/symphony-outbox/result.json` and the fixed
`/symphony-outbox/publication.bundle`. The strict result contains `task_id`,
exact optional `head`, `workpad_body`, `disposition`, and `summary`. The
host updates the admitted workpad comment, maps dispositions to fixed label
operations, and never accepts task-supplied endpoints or label names.

For `ready_for_human_merge`, the host validates the result, imports the fixed
bundle into a fresh bare repository, runs bundle verification and `git fsck`,
requires the exact requested commit, checks ancestry from the licensed base or
published head, and pushes only the derived branch with the dedicated deploy
key. It then creates or retains exactly one matching draft PR and updates
`task.json` only after publication succeeds. It never merges, closes, or
deploys.

## Execution truth states

| Boundary | State | Evidence |
|---|---|---|
| Linux user/mount/PID/network namespace primitive | PROVEN | real WSL `unshare` probe |
| Synthetic task filesystem constructor | PROVEN | hostile fixture |
| Host admission and dispatch provenance | PROVEN BY FIXTURES | strict parser and pagination fixtures |
| Host broker and publication transfer | PROVEN BY FIXTURES | lifecycle and sterile bundle fixture |
| Ruleset protection parser | PROVEN BY FIXTURES | real API-shaped fixtures |
| Codex App Server activation | BLOCKED | auth boundary remains unproven |
| Live Codex task containment | NOT YET PROVEN | no Codex task was started |
| Multi-role canary and live cutover | NOT RUN | explicitly prohibited for this PR |

## Containment

The selected backend is rootless Linux/WSL `unshare`. The constructor creates a
fresh tmpfs root, mounts only the current workspace, task home, read-only
admission inbox, writable fixed outbox, bounded tmpfs, minimal devices, and
read-only runtime libraries, then mounts a task PID namespace and restricted
network namespace. Resource limits are applied to the fixture domain. The
constructor is exercised independently of Codex; the auth blocker still stops
the real launcher before App Server start.
