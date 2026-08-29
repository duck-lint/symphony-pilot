# Operations

Run these commands from a checkout of this repository:

    python3 scripts/validate_profile.py projects/cleanroom/profile.toml
    python3 scripts/provision_secret.py projects/cleanroom/profile.toml
    python3 scripts/deploy.py --profile projects/cleanroom/profile.toml
    python3 scripts/project.py --profile projects/cleanroom/profile.toml test
    python3 scripts/project.py --profile projects/cleanroom/profile.toml start
    python3 scripts/project.py --profile projects/cleanroom/profile.toml status
    python3 scripts/project.py --profile projects/cleanroom/profile.toml stop
    python3 scripts/project.py --profile projects/cleanroom/profile.toml stop-now

stop is the normal path and refuses to terminate an active Symphony process;
the official lifecycle must drain it first. stop-now is the emergency path.
The PID and log are under the profile state root. A detached process does not
depend on the terminal that launched it.

Start performs a GitHub dispatch-label count before launching and enforces the
profile's one-issue limit. Deployment and preparation fail closed when a
profile secret, Git repository, upstream, toolchain, clean worktree, or
publication preflight is unavailable.

For Windows-facing wrappers, invoke these WSL commands through a detached
PowerShell launcher and display the resulting status. The launcher should keep
all process state in WSL and should not embed a token or a machine-specific
secret. Dashboard discovery belongs to the official Symphony dashboard; a
closed browser is not a process control.

To add a project: commit a non-secret profile under projects/<slug>/, review
the generated workflow, provision its host secret, run deployment, and perform
a harmless canary before enabling its dispatch label.
