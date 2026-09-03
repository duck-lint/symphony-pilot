# Trusted local host API and browser UI

Step 4 provides a dependency-free Python HTTP server and a static, read-only
operator UI. Start it under the Linux/WSL operator environment with:

```console
python3 scripts/serve_control_ui.py
```

## Network and browser boundary

The server accepts only literal loopback binds (`127.0.0.1` by default; `::1`
is also valid). Hostnames, wildcard addresses, and LAN addresses fail closed.
Every HTTP request must also carry the canonical `Host` for the actual bound
literal address and listening port (`127.0.0.1:<port>` or `[::1]:<port>`).
This second check prevents a DNS-rebinding origin from addressing the local API
as `attacker.example:<port>`, even after its DNS answer changes to loopback.
`localhost`, aliases, malformed authorities, and wrong ports are not accepted.

There is no CORS response. A strict CSP prevents framing and limits resources
to the same origin. Step 4 has no mutation route, session, or CSRF mechanism;
those controls belong with a future licensed mutation rather than dead code.

## Authority and routes

Registry profiles and the single host-owned `ControlPlaneDatabase` remain the
authority. URL slugs and task UUIDs are selectors only. The server resolves a
slug through the complete validated registry, then proves the selected task
belongs to that project. Browser input cannot select roots, database paths,
repositories, refs, heads, executable paths, process identities, credentials,
SQL, commands, files, Git, GitHub calls, or network destinations.

The entire Step-4 HTTP surface is read-only:

* `GET /api/v1/projects`
* `GET /api/v1/projects/{slug}/tasks`
* `GET /api/v1/projects/{slug}/tasks/{uuid}`

The API opens SQLite with `mode=ro`. A read never creates a database, migrates
schema, changes permissions or journal mode, or repairs incompatible state.
The existing schema version, migration identity, physical schema, integrity,
and foreign keys must validate. SQLite itself rejects writes through the read
connection. Read handles participate in the database's open-handle accounting,
so offline restore semantics remain coherent.

The routes expose registered project summaries; tasks; workpads; role runs;
findings; blockers; publications; events; and bounded deployment, runtime-lock,
and managed-process identity summaries. A structurally valid Runtime lock is
reported as an accepted identity **record** (`state: recorded`, `lock_valid:
true`, `live_verification: not_performed`). GET neither hashes nor executes the
recorded executable, so the response makes no claim about its current bytes.

Credential-shaped text is redacted even if mistakenly stored in an
operator-visible field. This and the strict outbox are defense in depth, not
credential DLP. Structural credential isolation remains required before
unattended activation. Secrets, environments, raw files, and credential
references are never returned.

This UI is not an agent chat or a second state model. It provides no queue or
dispatch mutation, manual Codex launch, scheduler cutover, publication, merge,
or arbitrary repository control. Queue/dispatch semantics wait for Step 5.
