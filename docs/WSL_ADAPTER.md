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
trusted Linux supervisor `runtime/wsl_contained_exec.py`, which revalidates the
project and executes the requested argv inside the existing rootless
`linux-unshare` containment boundary. That boundary uses a private mount/PID/
network namespace, a chroot, `--kill-child=SIGKILL`, resource limits, and
explicit mounts. Project and Pilot-control source are read-only; only the
disposable per-run build/cache root and runtime release output directories are
writable.
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

The adapter never falls back to another distro, Windows shell, Windows Git,
Docker, or unconstrained same-user execution when containment or WSL is
unavailable. The trusted Linux supervisor itself is a setup process; the
requested command does not run until its containment domain is established.
