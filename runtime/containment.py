#!/usr/bin/env python3
"""The one supported Linux task-containment contract.

This module deliberately does not pretend that Codex policy is an OS boundary.
The selected backend is rootless ``unshare``.  Its capability probe is a real
namespace probe; an unavailable or incomplete backend is an infrastructure
blocker, never a reason to return to same-user execution.
"""
from __future__ import annotations

import dataclasses
import argparse
import hashlib
import os
import pathlib
import shlex
import shutil
import subprocess
import tempfile
from typing import Sequence


BACKEND_SCHEMA = "symphony-pilot-containment/v1"
BACKEND_NAME = "linux-unshare"
CONSTRUCTOR_SCHEMA = "symphony-pilot-task-domain/v1"
DEFAULT_LIMITS = {
    "processes": 128,
    "address_space_bytes": 2 * 1024 * 1024 * 1024,
    "open_files": 4096,
    "file_size_bytes": 512 * 1024 * 1024,
    "wall_seconds": 30 * 60,
}


class ContainmentError(RuntimeError):
    """A capability or configuration failure at the execution boundary."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclasses.dataclass(frozen=True)
class BackendIdentity:
    schema: str
    name: str
    executable: str
    version: str
    sha256: str


def _executable() -> pathlib.Path:
    if os.name == "nt":
        raise ContainmentError("host_platform", "task containment requires native Linux/WSL")
    path = shutil.which("unshare")
    if not path:
        raise ContainmentError(
            "containment_backend_missing",
            "required linux-unshare containment backend is unavailable; task execution is blocked",
        )
    return pathlib.Path(path).resolve()


def backend_identity() -> BackendIdentity:
    executable = _executable()
    try:
        result = subprocess.run(
            [str(executable), "--version"], capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ContainmentError("containment_identity", "cannot identify unshare") from exc
    version = (result.stdout or result.stderr).strip().splitlines()[0]
    return BackendIdentity(
        schema=BACKEND_SCHEMA,
        name=BACKEND_NAME,
        executable=str(executable),
        version=version,
        sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )


def _run_probe(command: Sequence[str]) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContainmentError("containment_probe", "namespace capability probe did not complete") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        raise ContainmentError("containment_probe", f"linux-unshare capability probe failed: {detail[:240]}")


def probe_backend() -> BackendIdentity:
    """Exercise mount, PID, and network namespaces without touching host state."""
    identity = backend_identity()
    executable = identity.executable
    probe = [
        executable,
        "--user", "--map-root-user",
        "--mount", "--pid", "--fork", "--mount-proc", "--net",
        "/bin/sh", "-c",
        "set -eu; mkdir -p /tmp/symphony-pilot-probe; "
        "mount -t tmpfs tmpfs /tmp/symphony-pilot-probe; "
        "test -d /proc/1; test ! -s /proc/net/route; "
        "prlimit --pid $$ --nproc=128 --as=2147483648 --nofile=4096 --fsize=536870912",
    ]
    _run_probe(probe)
    return identity


def require_backend() -> BackendIdentity:
    """Return the reviewed backend identity or fail closed."""
    return probe_backend()


def auth_boundary_blocker() -> ContainmentError:
    """Describe the unresolved Codex credential boundary precisely.

    The current official App Server can authenticate from its process auth
    surface, but this repository has no supported broker/descriptor contract
    that proves hostile tool children cannot recover that credential.  Keeping
    this as a typed blocker prevents configuration intent from becoming a
    security claim.
    """
    return ContainmentError(
        "codex_auth_boundary",
        "Codex App Server authentication cannot be proven inaccessible to hostile "
        "model-tool children with the accepted runtime; no unattended task may start",
    )


def require_execution_capability() -> BackendIdentity:
    """Probe OS containment, then stop at the unproven Codex auth seam."""
    identity = require_backend()
    raise auth_boundary_blocker()


def resource_limits() -> dict[str, int]:
    """Return the explicit current policy used by a future launcher wrapper."""
    return dict(DEFAULT_LIMITS)


def _mount_ro(source: str, target: str) -> str:
    return (f"if [ -e {shlex.quote(source)} ]; then "
            f"mkdir -p {shlex.quote(target)}; "
            f"mount --rbind {shlex.quote(source)} {shlex.quote(target)}; "
            f"mount -o remount,ro,bind {shlex.quote(target)}; fi")


def _fixture_script() -> str:
    return r'''#!/bin/sh
set -eu
fail() { echo "hostile fixture failure: $1" >&2; exit 1; }
[ "$(cat /symphony-inbox/admitted.txt)" = "admitted" ] || fail inbox_read
printf '%s' writable > /workspace/fixture-write
printf '%s' outbox > /symphony-outbox/fixture-write
if test -e /outside/secret; then fail host_secret_visible; fi
if test -e /operator-codex/auth.json; then fail operator_codex_visible; fi
if test -e /sibling/GH-99/secret; then fail sibling_visible; fi
if test -e /other-project/state/secret; then fail other_project_visible; fi
if test -e /host-state/secret || test -e /host-logs/secret; then fail host_state_visible; fi
if printf x > /symphony-inbox/should-not-write 2>/tmp/fixture.err; then fail inbox_writable; fi
ln -s /outside /workspace/escape
if test -e /workspace/escape/secret; then fail symlink_escape; fi
if test -e "/proc/$HOST_PID/environ"; then fail host_proc_visible; fi
if kill -0 "$HOST_PID" 2>/tmp/fixture.err; then fail host_signal_visible; fi
if test -e /ssh-agent/socket; then fail ssh_agent_visible; fi
if test -e /proc/1/fd/9; then fail inherited_fd_visible; fi
if python3 -c 'import socket; s=socket.socket(); s.settimeout(.2); s.connect(("198.51.100.1", 80))' 2>/tmp/fixture.err; then fail network_open; fi
prlimit --pid $$ --as=134217728
if python3 -c 'bytearray(256 * 1024 * 1024)' 2>/tmp/fixture.err; then fail address_limit_missing; fi
ulimit -f 1024
if python3 -c 'f=open("/workspace/too-big", "wb"); f.write(b"x" * (2 * 1024 * 1024))' 2>/tmp/fixture.err; then fail file_limit_missing; fi
rm -f /workspace/too-big
if python3 -c 'import os; f=os.open("/tmp/fixture.err", os.O_RDWR); [os.dup(f) for _ in range(8192)]' 2>/tmp/fixture.err; then fail open_file_limit_missing; fi
python3 -c '
import subprocess
p = []
for _ in range(128):
    try:
        p.append(subprocess.Popen(["sleep", "20"]))
    except OSError:
        break
for child in p:
    child.terminate()
assert len(p) < 128
' 2>/tmp/fixture.err || fail process_limit_missing
'''


def task_domain_command(identity: BackendIdentity, root: pathlib.Path, workspace: pathlib.Path,
                        home: pathlib.Path, inbox: pathlib.Path, outbox: pathlib.Path,
                        fixture: pathlib.Path, host_pid: int) -> list[str]:
    """Construct the reviewed task domain and expose only declared mounts.

    The caller supplies task-owned staging directories; no operator home,
    sibling project, task Git directory, socket, or host state is discovered
    here. The synthetic fixture and future Codex launcher share this seam.
    """
    setup = [
        "set -eu",
        f"mount -t tmpfs -o size=64m,nosuid,nodev tmpfs {shlex.quote(str(root))}",
        f"mkdir -p {shlex.quote(str(root))}/workspace {shlex.quote(str(root))}/home/task "
        f"{shlex.quote(str(root))}/symphony-inbox {shlex.quote(str(root))}/symphony-outbox "
        f"{shlex.quote(str(root))}/fixture {shlex.quote(str(root))}/proc "
        f"{shlex.quote(str(root))}/dev {shlex.quote(str(root))}/tmp",
        f"mount --bind {shlex.quote(str(workspace))} {shlex.quote(str(root / 'workspace'))}",
        f"mount --bind {shlex.quote(str(home))} {shlex.quote(str(root / 'home/task'))}",
        f"mount --bind {shlex.quote(str(inbox))} {shlex.quote(str(root / 'symphony-inbox'))}",
        f"mount -o remount,ro,bind {shlex.quote(str(root / 'symphony-inbox'))}",
        f"mount --bind {shlex.quote(str(outbox))} {shlex.quote(str(root / 'symphony-outbox'))}",
        f"touch {shlex.quote(str(root / 'fixture/hostile.sh'))}; "
        f"mount --bind {shlex.quote(str(fixture))} {shlex.quote(str(root / 'fixture/hostile.sh'))}; "
        f"mount -o remount,ro,bind {shlex.quote(str(root / 'fixture/hostile.sh'))}",
        _mount_ro("/bin", str(root / "bin")),
        _mount_ro("/usr", str(root / "usr")),
        _mount_ro("/lib", str(root / "lib")),
        _mount_ro("/lib64", str(root / "lib64")),
        _mount_ro("/etc", str(root / "etc")),
        f"mount -t tmpfs -o size=8m,nosuid,nodev,noexec tmpfs {shlex.quote(str(root / 'tmp'))}",
        f"mount -t tmpfs -o size=1m,nosuid,nodev,noexec tmpfs {shlex.quote(str(root / 'dev'))}",
        f"touch {shlex.quote(str(root / 'dev/null'))}",
        f"mount --bind /dev/null {shlex.quote(str(root / 'dev/null'))}",
        f"mount -t proc proc {shlex.quote(str(root / 'proc'))}",
        "prlimit --pid $$ --nproc=32 --as=2147483648 --nofile=4096 --fsize=1048576",
        f"HOST_PID={host_pid} chroot {shlex.quote(str(root))} /bin/sh /fixture/hostile.sh",
    ]
    return [identity.executable, "--user", "--map-root-user", "--mount", "--pid",
            "--fork", "--mount-proc", "--net", "/bin/sh", "-c", "\n".join(setup)]


def run_synthetic_hostile_fixture() -> dict[str, object]:
    """Run a real hostile fixture inside the constructed task domain."""
    if os.name == "nt":
        raise ContainmentError("host_platform", "synthetic task-domain proof requires native Linux/WSL")
    identity = require_backend()
    with tempfile.TemporaryDirectory(prefix="symphony-pilot-domain-", dir="/tmp") as directory:
        base = pathlib.Path(directory)
        root = base / "root"
        workspace = base / "workspace"
        home = base / "home"
        inbox = base / "inbox"
        outbox = base / "outbox"
        outside = base / "outside"
        fixture = base / "fixture.sh"
        for path in (root, workspace, home, inbox, outbox, outside):
            path.mkdir()
        (inbox / "admitted.txt").write_text("admitted", encoding="utf-8")
        (outside / "secret").write_text("host-secret-sentinel", encoding="utf-8")
        fixture.write_text(_fixture_script(), encoding="utf-8")
        fixture.chmod(0o500)
        command = task_domain_command(identity, root, workspace, home, inbox, outbox,
                                       fixture, os.getpid())
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=45, close_fds=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContainmentError("synthetic_fixture", "task-domain hostile fixture did not complete") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).strip().replace("\n", " ")
            raise ContainmentError("synthetic_fixture", f"task-domain hostile fixture failed: {detail[:300]}")
        if (workspace / "fixture-write").read_text(encoding="utf-8") != "writable":
            raise ContainmentError("synthetic_fixture", "task workspace was not writable")
        if (outbox / "fixture-write").read_text(encoding="utf-8") != "outbox":
            raise ContainmentError("synthetic_fixture", "task outbox was not writable")
        if (inbox / "should-not-write").exists():
            raise ContainmentError("synthetic_fixture", "task inbox was writable")
        timed = subprocess.run([
            "timeout", "1s", identity.executable, "--user", "--map-root-user", "--mount",
            "--pid", "--fork", "--mount-proc", "--net", "/bin/sh", "-c", "sleep 10",
        ], capture_output=True, text=True, check=False)
        if timed.returncode != 124:
            raise ContainmentError("synthetic_fixture", "task-domain wall-clock supervisor did not terminate the fixture")
    return {"schema": CONSTRUCTOR_SCHEMA, "backend": dataclasses.asdict(identity),
            "workspace_writable": True, "inbox_read_only": True, "outbox_writable": True,
            "hostile_denials": "passed", "resource_limits": {
                "processes": 32, "address_space_bytes": 134217728,
                "open_files": 4096, "file_size_bytes": 1048576,
                "tmpfs_bytes": 8 * 1024 * 1024, "wall_seconds": 1,
            }}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--synthetic-fixture", action="store_true")
    args = parser.parse_args()
    try:
        result = run_synthetic_hostile_fixture() if args.synthetic_fixture else require_execution_capability()
    except ContainmentError as exc:
        print(f"symphony-pilot containment blocked: {exc.kind}: {exc}")
        raise SystemExit(78)
    print(result)
