# Security contract

The model, task processes, issue/workpad payload, task filesystem, task Git
metadata, task result, and publication bundle are hostile inputs.

Trusted host state consists of the canonical registry, server-derived issue
and Git facts, tracker credential, dedicated publication deploy key, strict
task records, runtime locks, process state, and host audit state. Secrets are
never stored in Git, profiles, workpads, task homes, logs, or result payloads.

## Dispatch trust

Current label presence is necessary but insufficient. Admission requires the
issue to be open and each required dispatch label's latest applicable server
event to be a trusted `labeled` event. The actor login must be in that
project's non-secret `trusted_dispatchers` profile set. Missing, ambiguous,
malformed, or incomplete event history blocks admission before workpad or task
state creation.

## GitHub trust

The tracker token is API-only. The publication deploy key is project-scoped,
host-owned, mode 0600, agent-free, and used only by a sterile host repository.
The task receives neither credential. The default branch must have one active
repository ruleset requiring pull requests and no bypass actors. Human merge
authority is the actual GitHub account operating the merge; it is not a field
invented by the client parser.

The host never uses task `origin`, `remote.*`, `core.hooksPath`,
`credential.helper`, `include.path`, alternates, or SSH settings as publication
authority. A fixed regular bundle is imported into a fresh bare repository,
verified, checked for exact commit type and ancestry, and pushed with explicit
host SSH identity.

## Task domain

The activated task contract exposes only the current workspace, fresh task
home, read-only admission inbox, writable fixed outbox, minimal read-only
runtime files, task `/proc`, minimal devices, and bounded temporary storage.
Operator homes, `.codex`, `.ssh`, host state/logs, deployments, sibling tasks,
other projects, broad `/mnt/c`, agent sockets, and unrelated host processes are
outside the domain. The synthetic hostile fixture has passed these denial
checks, including network, symlink, proc, inherited-FD, and resource-limit
attempts.

This fixture proves the constructor, not live Codex behavior. The real launcher
still fails closed because the accepted Codex App Server authentication path
does not prove that hostile model tools cannot recover App Server credentials
or inherit its network authority.
