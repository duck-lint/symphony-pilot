# Pilot SQLite boundary

Pilot SQLite is the authoritative local store for task identity, lifecycle
state, working-round and planning-attempt authority, eligibility,
reconciliation, blockers, termination classification, grants, and retained
execution evidence.

Runtime reads only the project-scoped execution projection and cannot write
authoritative state. Role packets and model output are payloads; they become
state only after Pilot validates their identity, eligibility, and retained host
execution evidence.

The database is host-side state. It is not a Runtime, role, task-workspace, or
browser authority. The local API/UI projects trusted Pilot state and does not
maintain a second lifecycle model.

Exact schema, migration, and storage mechanics are implementation seams. They
must preserve the authority rules in the parent harness without creating a
second scheduler or compatibility surface.
