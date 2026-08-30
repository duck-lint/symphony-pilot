# Security boundary

Tracker credentials are host-only. A profile contains a secret reference, never
a token. The default path is:

    ~/.config/symphony-pilot/secrets/<profile>/<reference>

The file must be a single-line 0600 file. Provision it out of band; do not
commit it, print it, place it in a workflow, or put it in a workpad.
The repository helper scripts/provision_secret.py reads the value with a
hidden prompt and writes the directory/file permissions without shell history.

Symphony receives the tracker token because it must read and update GitHub
issues. The generated Codex launcher explicitly removes the tracker variables
before sourcing the workspace toolchain fragment or starting App Server. SSH is
the preferred source credential for
repository clone/fetch/push. A tracker PAT is not used as ordinary Git
authentication.

The deployment excludes local Git metadata, secrets, and state. Logs are host
artifacts and must be reviewed for accidental credential material. Recovery
archives exclude .git and target; they are stored outside the execution
workspace and contain a non-secret manifest.

No global Git configuration is changed. WSL-native paths are required for
repositories and workspaces. Windows paths may be selected dynamically for
Cargo target output when a Windows Rust toolchain is the usable toolchain.
