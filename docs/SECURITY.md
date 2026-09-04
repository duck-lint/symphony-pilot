# Security contract

The model, task processes, issue/workpad payload, task filesystem, task Git
metadata, task result, and publication bundle are hostile inputs.

Trusted host state consists of the canonical registry, host-derived Git facts,
SQLite task rows, dedicated publication deploy key, runtime locks, process
state, and host audit state. Any retained publication credential is read only
by its host operation. Secrets are never stored in Git, profiles, workpads,
task homes, logs, or result payloads.

## Local dispatch trust

The trusted operator is the authority for local task creation and the explicit
`PREPARED -> QUEUED` transition. SQLite CAS plus its atomic `queued` event
prevents repeated or conflicting queue requests from silently overwriting
state. Runtime receives no tracker credential and reads only the project-scoped
SQLite projection.

GitHub issue labels and event history are not scheduler trust inputs after Step
5. They survive only in deferred legacy/publication code and cannot create a
local task or authorize a workspace.

## GitHub trust

The tracker token is API-only. The publication deploy key is project-scoped,
host-owned, mode 0600, agent-free, and used only by a sterile host repository.
The task receives neither credential. The default branch must have one active
repository ruleset requiring pull requests and no bypass actors. Human merge
authority is the actual GitHub account operating the merge; it is not a field
invented by the client parser.

The host never uses task `origin`, `remote.*`, `core.hooksPath`,
`credential.helper`, `include.path`, alternates, or SSH settings as publication
authority. The fixed bundle is opened once with no-follow descriptor semantics,
bounded-copied to host-owned temporary storage, and closed; Git never reopens
the task pathname. Only the staged regular file is imported into a fresh bare
repository, verified, checked for exact commit type and ancestry, and pushed
with explicit host SSH identity.

## Task domain

The task-domain contract exposes only the current workspace, fresh task home,
read-only admission inbox, writable fixed outbox, minimal read-only runtime
files, task `/proc`, minimal devices, and bounded temporary storage. It does
not expose host `/etc`; the explicit current `/etc` allowlist is empty.
Operator homes, `.codex`, `.ssh`, host state/logs, deployments, sibling tasks,
other projects, broad `/mnt/c`, agent sockets, and unrelated host processes are
outside the domain. The synthetic hostile fixture has passed these denial
checks, including network, symlink, proc, inherited-FD, and resource-limit
attempts.

The shared namespace runner applies a CPU-time limit and uses util-linux
`--kill-child=SIGKILL`; timeout handling kills and reaps the namespace
supervisor before reporting completion, and the fixture verifies that a
grandchild can no longer modify a task-visible sentinel. Individual file size
is bounded, but aggregate growth of the persistent task workspace is not: no
total workspace disk quota is claimed or available in this cutover.

This fixture proves the constructor, not live Codex behavior. The real launcher
still fails closed because the accepted Codex App Server authentication path
does not prove that hostile model tools cannot recover App Server credentials
or inherit its network authority.
