# Windows-to-WSL adapter

`scripts/wsl_adapter.py` is the narrow Windows-host bridge for bounded Linux
work that must run in the operator's WSL environment. It is not a Windows
command broker and it is not a replacement for the Linux containment contract.

The adapter has one fixed transition:

```text
Windows host -> Ubuntu-24.04 -> duck-lint
```

It does not accept a distro argument. Project selection is limited to the
registered `symphony-pilot` and `symphony-runtime` source roots under
`/mnt/f/PROJECT-REPOS`. No Pilot state or secret root is an adapter cwd. The
requested cwd is normalized lexically, canonicalized with Ubuntu's
`/usr/bin/readlink -e`, checked for a symlink escape, and verified as a
directory before the requested command runs.

Invocation is structured `wsl.exe` argv with `shell=False`. The adapter does
not attempt to parse shell, Python, Elixir, or Make source. It selects the
supervisor from the fixed host-owned `symphony-canary` control deployment at
`/home/duck-lint/.local/share/symphony-pilot/deployments/symphony-canary/runtime/`
and never falls back to the mutable source checkout. The control deployment is
created by the existing `scripts/deploy.py --project symphony-canary` contract;
the requested source project remains a separate, allowlisted input to the
supervisor. The deployed supervisor
validates `DEPLOYMENT.json`, including the SHA-256 inventory and deployment
identity, before importing `containment.py`. It then revalidates the project
and executes the requested argv inside the existing rootless `linux-unshare`
containment boundary. That boundary uses a private mount/PID/network
namespace, a chroot, `--kill-child=SIGKILL`, resource limits, and explicit
mounts. The selected project is read-only; only the disposable per-run
build/cache root, expected runtime release-output directories, and reviewed
toolchain data are exposed. Pilot source and credentials are not mounted.
The contained HOME exposes only the reviewed mise binary/data paths, not the
operator home, `.codex`, `.ssh`, or Pilot secrets. `/proc` is the contained
namespace and no host descriptors are inherited. The Windows environment is
reduced to `SystemRoot` and `WINDIR` before the supervisor starts, and the
contained command receives a sterile allowlisted environment as `duck-lint`.
The selected top-level executable cannot be a Windows command; this is defense
in depth, not the filesystem security boundary.

Each process has a bounded timeout and 4 MiB per-stream output cap. Results
include the fixed distro, canonical cwd, exit code, output, timeout/termination
classification, truncation flags, timestamps, request identity, and approval
status. `audit_record()` excludes command and output content so callers can
persist safe host evidence through their existing audit mechanism.

The host-side diagnostic form is:

```powershell
python scripts/wsl_adapter.py `
  --project symphony-runtime `
  --cwd /mnt/f/PROJECT-REPOS/symphony-runtime/elixir `
  -- /usr/bin/id -un
```

The control snapshot must be deployed from a clean reviewed Pilot checkout
before this bridge is used:

```bash
python3 scripts/deploy.py --project symphony-canary
```

That command uses the existing atomic deployment path. The resulting
`DEPLOYMENT.json` records the Pilot source commit, file SHA-256 inventory, and
deployment identity. The supervisor requires its root to be exactly the
`symphony-canary` directory and verifies the complete inventory before it
imports the containment implementation. A changed source checkout therefore
cannot silently replace the pre-containment authority.

The acceptance-domain mount map is intentionally small:

| Path | Authority |
| --- | --- |
| `/project` | selected source tree, read-only |
| `/build` | disposable per-run cache/build root, writable |
| `/project/bin`, `/project/burrito_out` | declared runtime output paths, writable |
| `/home/duck-lint/.local/bin/mise` | one reviewed executable, read-only |
| `/home/duck-lint/.local/share/mise` | required mise data, read-only |
| `/tmp`, `/dev`, `/proc` | fresh namespace-local runtime surfaces |
| `/bin`, `/usr`, `/lib`, `/lib64` | existing recursive read-only system helper |

The system helper remains the accepted task-domain helper and is reused here
because the narrower non-recursive bind did not support the prior acceptance
workload. Nested-mount contents and effective read-only behavior still require
live WSL inspection; unit tests do not close that assessment. No Pilot source,
Pilot state, credentials, or operator home is mounted.

The adapter never falls back to another distro, Windows shell, Windows Git,
Docker, or unconstrained same-user execution when containment or WSL is
unavailable. The trusted Linux supervisor itself is a setup process; the
requested command does not run until its deployment manifest and containment
domain are established. A missing deployment, missing authority file,
manifest/hash mismatch, or unlisted deployment file fails closed.
