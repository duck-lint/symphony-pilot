# Trusted local host API and browser UI

Step 4 provides a dependency-free Python HTTP server and a static operator UI.
Start it under the Linux/WSL operator environment with:

```console
python3 scripts/serve_control_ui.py
```

The server accepts only literal loopback binds (`127.0.0.1` by default; `::1`
is also valid). Hostnames, wildcard addresses, and LAN addresses fail closed.
There is no CORS response. A strict CSP prevents framing and limits resources
to the same origin.

## Authority and routes

Registry profiles and the single host-owned `ControlPlaneDatabase` remain the
authority. URL slugs and task UUIDs are selectors only. The server resolves a
slug through the complete validated registry, then proves the selected task
belongs to that project. Browser input cannot select roots, database paths,
repositories, refs, heads, executable paths, process identities, credentials,
SQL, commands, files, Git, GitHub calls, or network destinations.

Read-only routes are:

* `GET /api/v1/projects`
* `GET /api/v1/projects/{slug}/tasks`
* `GET /api/v1/projects/{slug}/tasks/{uuid}`

They expose registered project summaries; tasks; workpads; role runs; findings;
blockers; publications; events; and bounded deployment, accepted-runtime, and
managed-process identity summaries. Credential-shaped text is redacted even if
it was mistakenly stored in an operator-visible text field. Secrets,
environments, raw files, and credential references are never returned.

The only command is:

* `POST /api/v1/projects/{slug}/tasks/{uuid}/queue`

It accepts exactly `{}`. The route hard-codes `PREPARED` → `QUEUED`, invokes
`ControlPlaneDatabase.transition_task()`, and atomically appends the `queued`
event. Its compare-and-set predicate makes repeated or conflicting requests a
409 conflict rather than a silent overwrite. GET handlers invoke no lifecycle
mutation.

## Browser mutation boundary

Loading `/` creates a random server-side session and independent CSRF token.
The session cookie is `HttpOnly`, `SameSite=Strict`, and path-bound. A mutation
must have the server's exact `Origin`, the session cookie, its matching
`X-Symphony-CSRF` header, and `application/json`. The custom header and JSON
content type make the request non-simple; an untrusted website cannot produce
it cross-origin without preflight, and this server grants no CORS permission.

This UI is not an agent chat or a second state model. It renders current API
responses and clearly reports the intentional execution block. It provides no
manual Codex launch, scheduler cutover, publication, merge, or arbitrary
repository control.
