# Operations

Run these commands from a checkout of this repository:

    python3 scripts/validate_profile.py projects/cleanroom/profile.toml
    python3 scripts/provision_secret.py projects/cleanroom/profile.toml
    python3 scripts/deploy.py --profile projects/cleanroom/profile.toml
    python3 scripts/project.py --profile projects/cleanroom/profile.toml test
    python3 scripts/project.py --profile projects/cleanroom/profile.toml start
    python3 scripts/project.py --profile projects/cleanroom/profile.toml status
    python3 scripts/project.py --profile projects/cleanroom/profile.toml finish
    python3 scripts/project.py --profile projects/cleanroom/profile.toml stop
    python3 scripts/project.py --profile projects/cleanroom/profile.toml stop-now

finish is the normal end-of-session path: it waits for the one authorized
running/retrying issue to drain, then stops Symphony. It also stops an idle or
durably human-blocked service. stop refuses to terminate active work;
stop-now is the emergency path. A cancelled finish leaves Symphony and active
work running.
The PID and log are under the profile state root. A detached process does not
depend on the terminal that launched it.

For a profile with prevent_host_sleep=true, start establishes the pilot-owned
Windows execution-state guard before launching Symphony. Successful finish,
stop, and stop-now release it; stale bookkeeping for a dead helper is removed
on the next start. Windows adapters invoke the deployed command
`<deployment>/scripts/project.py`, never a source checkout.

The deployed `test` action is deployment-safe: it checks that the deployed
profile, manifest, workflow, operator command, required runtime modules, and
the six generic role-policy files exist. Source-only validation remains the
responsibility of the pilot checkout.

During an app-server run, the launcher exposes those role policies through a
temporary external `CODEX_HOME`; it does not create pilot role files in the
target checkout. The archivist returns a closeout packet, while the architect
persists only accepted archival facts to the single workpad.

Start performs a GitHub dispatch-label count before launching and enforces the
profile's one-issue limit. Deployment and preparation fail closed when a
profile secret, Git repository, upstream, toolchain, clean worktree, or
publication preflight is unavailable.

For Windows-facing wrappers, invoke these WSL commands through a detached
PowerShell launcher and display the resulting status. The launcher should keep
all process state in WSL and should not embed a token or a machine-specific
secret. Dashboard discovery belongs to the official Symphony dashboard; a
closed browser is not a process control. Generic Windows notifications are
emitted by existing host lifecycle hooks for durable human, infrastructure,
and completed states, with fingerprints under the profile state root. They are
convenience only; GitHub labels and the workpad remain authoritative.

To add a project: commit a non-secret profile under projects/<slug>/, review
the generated workflow and role pack, provision its host secret, run
deployment, and perform a harmless canary before enabling its dispatch label.
The canary must prove a real app-server role handoff, not just deployment
presence: observe project-manager and planner packets, an implementer change,
a fresh reviewer verdict, a fresh adversarial verdict, and the same final HEAD
in both acceptance records and mechanical validation. If role selection is not
available in the installed Codex path, record that capability boundary and do
not silently run the old architect-worker lifecycle.
