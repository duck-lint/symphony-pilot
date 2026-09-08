# Pilot security boundary

The parent harness is authoritative for security and authority semantics.
Pilot is the trusted local control plane; Runtime and roles are not
independent authorities.

Host-trusted facts include registered project identity, authoritative
repository observations, Pilot SQLite state, process identity, workspace Git
facts, retained execution evidence, and host-owned publication credentials.
Model output, role packets, task files, task-controlled Git metadata, Runtime
observations, and UI input are untrusted until validated at the appropriate
host boundary.

Pilot authorizes role capability grants. Runtime may instantiate and enforce
only the exact grant Pilot issued. Planner, Implementer, and Archivist have
separate bounded writable domains; Reviewer, Adversary, and PM/Dispatcher have
no project or harness-artifact write authority. No role may write .git,
stage, commit, or broaden its own grant.

For every writer, the host observes the exact filesystem delta and validates
every changed path against the corresponding Pilot grant before staging or
committing. A documentation grant cannot reach adjacent project content.
Publication is a separate host-brokered operation, and final merge remains
human-only.

The local UI projects trusted Pilot state and does not infer execution from
role names, expected sequence, prose, packets, or dashboard presence. Runtime
process/session state is evidence for reconciliation, not lifecycle authority.

The supervised-local mode uses explicit operator authentication. That proves
supervised local operation only; it does not prove unattended credential
isolation, hostile-child isolation, or publication hardening. Containment,
storage, executable pinning, and publication findings remain separately
classified in SECURITY_FINDINGS.md.

