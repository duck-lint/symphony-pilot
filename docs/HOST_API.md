# Pilot local API and UI

The parent harness is authoritative for lifecycle and authority semantics. The
local API/UI is a read-only projection of trusted Pilot state, not a second
scheduler, lifecycle model, agent chat, or execution authority.

Pilot owns the projected task, lifecycle, working-round, planning-attempt,
eligibility, blocker, termination, grant, execution-evidence, repository, and
workspace facts. Runtime observation is reported as observation; it does not
become lifecycle meaning.

The interface may expose, where available:

- expected role and dispatch eligibility;
- actual retained execution identity and evidence;
- role-authored result status;
- grant and authorization status;
- exact changed paths and resulting commit;
- accepted HEAD and workspace HEAD;
- clean/dirty workspace status; and
- publication status.

Expected role, role name, PM prose, packet content, and UI state do not prove
that an execution occurred. A role run is reportable only when Pilot has
retained host evidence for that actual execution and has reconciled the
corresponding packet.

The existing loopback surface is read-only and operator-supervised. Browser
input does not select repositories, roots, database paths, refs, processes,
credentials, commands, files, Git operations, or network destinations. Queue
and lifecycle mutation remain trusted host operations. The current supervised
substrate is evidence of operation, not proof that the frozen fresh-specialist
lifecycle is live.

