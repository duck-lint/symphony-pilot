# Recovery and failure policy

Preparation is serialized by an issue-scoped advisory lock. It verifies
repository identity and remote connectivity, then chooses state as follows:

- Initial dispatch: use the issue-authorized starting ref/SHA.
- Continuation: fetch and use the existing remote issue branch, not the
  original baseline SHA. Verify required-base ancestry separately.
- Dirty tree exactly equal to the authoritative remote tree: archive it, reset
  and clean it, then continue.
- Dirty tree with a unique delta: archive it outside the workspace, record one
  stable blocker fingerprint in the workpad, disable dispatch, and stop. It is
  never silently discarded.
- Recovery archives exclude Git metadata and common secret containers such as
  `.env`, credential/token/secret paths, private-key files, and private-key
  extensions. Exclusions are recorded by path name in the non-secret recovery
  manifest; contents are never copied into the archive.
- Broken/stale metadata or unsafe workspace: preserve the old path and clone a
  fresh WSL-native workspace.
- Every successful preparation writes .git/symphony-preparation.json only after
  clean status, upstream, toolchain, and publication dry-run pass.

The marker schema is symphony-pilot-preparation/v1 and records repository,
issue, branch, resolved and remote SHAs, required starting state, base ancestry,
upstream, initial/continuation mode, clean verification, run identity, and
non-secret toolchain/publication facts.

Infrastructure blocker fingerprints include issue, workspace, target SHA,
failure class, stable detail, and status hash. Repeated identical blockers do
not append another workpad diagnosis or consume unlimited retries. A
recoverable condition is repaired before worker launch; an unrecoverable
condition is escalated once with the recovery artifact location.

A remote issue branch is the continuation checkpoint. Local filesystem state
never overrides it.
