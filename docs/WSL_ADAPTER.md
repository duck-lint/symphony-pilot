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
`/mnt/f/PROJECT-REPOS`. Pilot-owned state roots are admitted only for the
Pilot project. The requested cwd is normalized lexically, canonicalized with
Ubuntu's `/usr/bin/readlink -e`, checked for a symlink escape, and verified as a
directory before the requested command runs.

Invocation is structured `wsl.exe` argv with `shell=False`. An explicit
`bash -lc` command is allowed only as the Linux shell boundary; it is passed as
one argument and runs with `--noprofile --norc`. The Windows environment is
reduced to `SystemRoot` and `WINDIR`; the Linux command receives a sterile
allowlisted environment as `duck-lint`. Windows executable/path forms,
credential channels, secret paths, `sudo`, and root requests are rejected.

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
Docker, or same-user execution when WSL is unavailable.
