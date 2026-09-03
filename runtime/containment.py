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
import signal
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Sequence


BACKEND_SCHEMA = "symphony-pilot-containment/v1"
BACKEND_NAME = "linux-unshare"
CONSTRUCTOR_SCHEMA = "symphony-pilot-task-domain/v1"
DEFAULT_LIMITS = {
    "processes": 128,
    "address_space_bytes": 2 * 1024 * 1024 * 1024,
    "cpu_seconds": 300,
    "open_files": 4096,
    "file_size_bytes": 512 * 1024 * 1024,
    "wall_seconds": 30 * 60,
}
MAX_DOMAIN_OUTPUT_BYTES = 4 * 1024 * 1024


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
        "--mount", "--pid", "--fork", "--kill-child=SIGKILL", "--mount-proc", "--net",
        "/bin/sh", "-c",
        "set -eu; mkdir -p /tmp/symphony-pilot-probe; "
        "mount -t tmpfs tmpfs /tmp/symphony-pilot-probe; "
        "test -d /proc/1; test ! -s /proc/net/route; "
        "prlimit --pid $$ --nproc=128 --as=2147483648 --cpu=300 --nofile=4096 --fsize=536870912",
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
    """Return the explicit limits inherited by the task-domain constructor."""
    return dict(DEFAULT_LIMITS)


@dataclasses.dataclass(frozen=True)
class TaskDomainResult:
    returncode: int
    timed_out: bool
    stdout: str
    stderr: str


def run_task_domain(command: Sequence[str], wall_seconds: float) -> TaskDomainResult:
    """Run and reap one namespace supervisor, including timeout teardown.

    ``task_domain_command`` includes util-linux ``--kill-child=SIGKILL``. On
    timeout the host kills that supervisor, waits for it to be reaped, and
    reports completion only after the supervisor is definitely gone. The
    synthetic sentinel fixture verifies the delegated child tree is gone too.
    """
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, text=False, close_fds=True,
                                   start_new_session=True)
    except OSError as exc:
        raise ContainmentError("task_domain_start", "task-domain supervisor could not start") from exc
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    output_limit = threading.Event()
    reader_error = threading.Event()

    def drain(stream: object, buffer: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(8192)  # type: ignore[union-attr]
                if not chunk:
                    return
                remaining = MAX_DOMAIN_OUTPUT_BYTES - len(buffer)
                if remaining <= 0:
                    output_limit.set()
                    return
                buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    output_limit.set()
                    return
        except (OSError, ValueError):
            reader_error.set()

    stdout_thread = threading.Thread(target=drain, args=(process.stdout, stdout_buffer), daemon=True)
    stderr_thread = threading.Thread(target=drain, args=(process.stderr, stderr_buffer), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    termination = "completed"
    deadline = time.monotonic() + wall_seconds
    while process.poll() is None:
        if reader_error.is_set():
            termination = "output_error"
            break
        if output_limit.is_set():
            termination = "output_limit"
            break
        if time.monotonic() >= deadline:
            termination = "timeout"
            break
        time.sleep(0.01)

    if termination != "completed":
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise ContainmentError("task_domain_teardown", "task-domain supervisor was not reaped after timeout") from exc
    else:
        process.wait()
    stdout_thread.join(timeout=10)
    stderr_thread.join(timeout=10)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        raise ContainmentError("task_domain_teardown", "task-domain output readers were not reaped")
    if reader_error.is_set():
        raise ContainmentError("task_domain_output", "task-domain output could not be read safely")
    if output_limit.is_set():
        raise ContainmentError("task_domain_output", "task-domain output exceeded its bounded limit")
    try:
        stdout = bytes(stdout_buffer).decode("utf-8")
        stderr = bytes(stderr_buffer).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContainmentError("malformed_output", "task-domain output was not valid UTF-8") from exc
    return TaskDomainResult(process.returncode, termination == "timeout", stdout, stderr)


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
if test -e /etc/passwd; then fail broad_etc_visible; fi
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


def _teardown_fixture_script() -> str:
    return r'''#!/bin/sh
set -eu
python3 -c 'import subprocess,time; grandchild=subprocess.Popen(["sh","-c","while :; do printf x >> /workspace/descendant-sentinel; sleep .05; done"]); time.sleep(30)' &
wait
'''


def _cpu_fixture_script() -> str:
    return r'''#!/bin/sh
set -eu
python3 -c 'while True: pass'
'''


def task_domain_command(identity: BackendIdentity, root: pathlib.Path, workspace: pathlib.Path,
                        home: pathlib.Path, inbox: pathlib.Path, outbox: pathlib.Path,
                        fixture: pathlib.Path, host_pid: int, *, cpu_seconds: int | None = None,
                        processes: int | None = None, address_space_bytes: int | None = None,
                        file_size_bytes: int | None = None) -> list[str]:
    """Construct the reviewed task domain and expose only declared mounts.

    The caller supplies task-owned staging directories; no operator home,
    sibling project, task Git directory, socket, or host state is discovered
    here. The synthetic fixture and future Codex launcher share this seam.
    No host ``/etc`` tree is mounted; the current allowlist is intentionally
    empty until a concrete runtime dependency is reviewed.
    """
    cpu_seconds = cpu_seconds if cpu_seconds is not None else DEFAULT_LIMITS["cpu_seconds"]
    processes = processes if processes is not None else DEFAULT_LIMITS["processes"]
    address_space_bytes = (address_space_bytes if address_space_bytes is not None
                           else DEFAULT_LIMITS["address_space_bytes"])
    file_size_bytes = file_size_bytes if file_size_bytes is not None else DEFAULT_LIMITS["file_size_bytes"]
    if any(not isinstance(value, int) or value < 1
           for value in (cpu_seconds, processes, address_space_bytes, file_size_bytes)):
        raise ContainmentError("task_domain_limits", "task resource limits must be positive integers")
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
        f"mount -t tmpfs -o size=8m,nosuid,nodev,noexec tmpfs {shlex.quote(str(root / 'tmp'))}",
        f"mount -t tmpfs -o size=1m,nosuid,nodev,noexec tmpfs {shlex.quote(str(root / 'dev'))}",
        f"touch {shlex.quote(str(root / 'dev/null'))}",
        f"mount --bind /dev/null {shlex.quote(str(root / 'dev/null'))}",
        f"mount -t proc proc {shlex.quote(str(root / 'proc'))}",
        f"prlimit --pid $$ --nproc={processes} --as={address_space_bytes} "
        f"--cpu={cpu_seconds} --nofile={DEFAULT_LIMITS['open_files']} --fsize={file_size_bytes}",
        f"HOST_PID={host_pid} exec /usr/sbin/chroot {shlex.quote(str(root))} /bin/sh /fixture/hostile.sh",
    ]
    return [identity.executable, "--user", "--map-root-user", "--mount", "--pid",
            "--fork", "--kill-child=SIGKILL", "--mount-proc", "--net", "/bin/sh", "-c", "\n".join(setup)]


def _acceptance_mount_ro(source: pathlib.Path, target: pathlib.Path) -> str:
    """Bind one reviewed host path read-only without importing nested mounts."""
    return (f"mkdir -p {shlex.quote(str(target))}; "
            f"mount --bind {shlex.quote(str(source))} {shlex.quote(str(target))}; "
            f"mount -o remount,ro,bind {shlex.quote(str(target))}")


def _acceptance_mount_rw(source: pathlib.Path, target: pathlib.Path) -> str:
    """Bind one explicitly writable output/cache path into the domain."""
    return (f"mkdir -p {shlex.quote(str(target))}; "
            f"mount --bind {shlex.quote(str(source))} {shlex.quote(str(target))}; "
            f"mount -o remount,rw,bind {shlex.quote(str(target))}")


def acceptance_domain_command(
    identity: BackendIdentity,
    root: pathlib.Path,
    control_source: pathlib.Path,
    project_source: pathlib.Path,
    build_root: pathlib.Path,
    toolchain_bin: pathlib.Path,
    toolchain_data: pathlib.Path,
    cwd: str,
    command: Sequence[str],
    writable_outputs: Sequence[tuple[pathlib.Path, str]] = (),
    *,
    processes: int | None = None,
    address_space_bytes: int | None = None,
    file_size_bytes: int | None = None,
) -> list[str]:
    """Construct a bounded host acceptance/build domain.

    This is the same rootless user, mount, PID, network, prlimit, chroot, and
    child-teardown boundary as the task domain.  Its mount allowlist differs:
    source and control trees are read-only; only declared build/cache roots and
    release output directories are writable.  The requested command is not
    language-parsed here.  It runs after chroot, where the mount namespace—not
    an argv blacklist—limits what shells and interpreters can reach.
    """
    processes = processes if processes is not None else DEFAULT_LIMITS["processes"]
    address_space_bytes = address_space_bytes if address_space_bytes is not None else DEFAULT_LIMITS["address_space_bytes"]
    file_size_bytes = file_size_bytes if file_size_bytes is not None else DEFAULT_LIMITS["file_size_bytes"]
    values = (processes, address_space_bytes, file_size_bytes)
    if any(not isinstance(value, int) or value < 1 for value in values):
        raise ContainmentError("task_domain_limits", "task resource limits must be positive integers")
    if not command or any(not isinstance(argument, str) or "\x00" in argument for argument in command):
        raise ContainmentError("task_domain_command", "contained command must be a non-empty argv")
    if not cwd.startswith("/project") or (len(cwd) > len("/project") and not cwd.startswith("/project/")):
        raise ContainmentError("task_domain_cwd", "contained cwd must remain under /project")

    root = root.resolve()
    control_source = control_source.resolve()
    project_source = project_source.resolve()
    build_root = build_root.resolve()
    toolchain_bin = toolchain_bin.resolve()
    toolchain_data = toolchain_data.resolve()
    project_target = root / "project"
    control_target = root / "pilot-control"
    build_target = root / "build"
    toolchain_bin_target = root / "home/duck-lint/.local/bin"
    toolchain_data_target = root / "home/duck-lint/.local/share/mise"
    shell_command = f"cd {shlex.quote(cwd)} && exec {shlex.join(tuple(command))}"
    system_mounts = [
        _mount_ro(source, str(root / source.lstrip("/")))
        for source in ("/bin", "/usr", "/lib", "/lib64")
        if pathlib.Path(source).exists()
    ]
    setup = [
        "set -eu",
        "mount --make-rprivate /",
        f"mount -t tmpfs -o size=128m,nosuid,nodev tmpfs {shlex.quote(str(root))}",
        f"mkdir -p {shlex.quote(str(root / 'project'))} {shlex.quote(str(root / 'pilot-control'))} "
        f"{shlex.quote(str(root / 'build'))} {shlex.quote(str(root / 'home/duck-lint/.local/bin'))} "
        f"{shlex.quote(str(root / 'home/duck-lint/.local/share/mise'))} "
        f"{shlex.quote(str(root / 'proc'))} {shlex.quote(str(root / 'dev'))} "
        f"{shlex.quote(str(root / 'tmp'))}",
        _acceptance_mount_ro(control_source, control_target),
        _acceptance_mount_ro(project_source, project_target),
        _acceptance_mount_rw(build_root, build_target),
        _acceptance_mount_ro(toolchain_bin, toolchain_bin_target),
        _acceptance_mount_ro(toolchain_data, toolchain_data_target),
        *(_acceptance_mount_rw(source, root / target.lstrip("/")) for source, target in writable_outputs),
        *system_mounts,
        f"mount -t tmpfs -o size=32m,nosuid,nodev tmpfs {shlex.quote(str(root / 'tmp'))}",
        f"mount -t tmpfs -o size=2m,nosuid,nodev tmpfs {shlex.quote(str(root / 'dev'))}",
        f"touch {shlex.quote(str(root / 'dev/null'))} {shlex.quote(str(root / 'dev/zero'))} "
        f"{shlex.quote(str(root / 'dev/urandom'))}",
        f"mount --bind /dev/null {shlex.quote(str(root / 'dev/null'))}",
        f"mount --bind /dev/zero {shlex.quote(str(root / 'dev/zero'))}",
        f"mount --bind /dev/urandom {shlex.quote(str(root / 'dev/urandom'))}",
        f"mount -t proc proc {shlex.quote(str(root / 'proc'))}",
        f"prlimit --pid $$ --nproc={processes} --as={address_space_bytes} "
        f"--cpu={DEFAULT_LIMITS['cpu_seconds']} --nofile={DEFAULT_LIMITS['open_files']} --fsize={file_size_bytes}",
        f"exec /usr/sbin/chroot {shlex.quote(str(root))} /usr/bin/env -i "
        "HOME=/home/duck-lint USER=duck-lint LOGNAME=duck-lint "
        "PATH=/home/duck-lint/.local/bin:/usr/local/bin:/usr/bin:/bin "
        "LANG=C.UTF-8 LC_ALL=C.UTF-8 "
        "MIX_HOME=/build/mix-home HEX_HOME=/build/hex-home "
        "MIX_BUILD_ROOT=/build/mix/linux/_build "
        "ZIG_LOCAL_CACHE_DIR=/build/zig/local ZIG_GLOBAL_CACHE_DIR=/build/zig/global "
        "SYMPHONY_INSTALL_DIR=/build/burrito-install "
        f"/bin/sh -c {shlex.quote(shell_command)}",
    ]
    return [identity.executable, "--user", "--map-root-user", "--mount", "--pid",
            "--fork", "--kill-child=SIGKILL", "--mount-proc", "--net", "/bin/sh", "-c", "\n".join(setup)]


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
                                       fixture, os.getpid(), processes=32)
        result = run_task_domain(command, wall_seconds=45)
        if result.timed_out:
            raise ContainmentError("synthetic_fixture", "normal task-domain fixture unexpectedly timed out")
        if result.returncode:
            detail = (result.stderr or result.stdout).strip().replace("\n", " ")
            raise ContainmentError("synthetic_fixture", f"task-domain hostile fixture failed: {detail[:300]}")
        if (workspace / "fixture-write").read_text(encoding="utf-8") != "writable":
            raise ContainmentError("synthetic_fixture", "task workspace was not writable")
        if (outbox / "fixture-write").read_text(encoding="utf-8") != "outbox":
            raise ContainmentError("synthetic_fixture", "task outbox was not writable")
        if (inbox / "should-not-write").exists():
            raise ContainmentError("synthetic_fixture", "task inbox was writable")
        teardown_workspace = base / "teardown-workspace"
        teardown_home = base / "teardown-home"
        teardown_inbox = base / "teardown-inbox"
        teardown_outbox = base / "teardown-outbox"
        teardown_root = base / "teardown-root"
        teardown_fixture = base / "teardown.sh"
        for path in (teardown_root, teardown_workspace, teardown_home, teardown_inbox, teardown_outbox):
            path.mkdir()
        teardown_fixture.write_text(_teardown_fixture_script(), encoding="utf-8")
        teardown_fixture.chmod(0o500)
        teardown_command = task_domain_command(identity, teardown_root, teardown_workspace,
                                                teardown_home, teardown_inbox, teardown_outbox,
                                                teardown_fixture, os.getpid(), cpu_seconds=5, processes=32)
        sentinel = teardown_workspace / "descendant-sentinel"
        import time
        for _ in range(2):
            teardown = run_task_domain(teardown_command, wall_seconds=1)
            if not teardown.timed_out or teardown.returncode == 0:
                raise ContainmentError("synthetic_fixture", "task-domain wall timeout did not reap its supervisor")
            first_size = sentinel.stat().st_size if sentinel.exists() else 0
            time.sleep(.3)
            second_size = sentinel.stat().st_size if sentinel.exists() else 0
            if second_size != first_size:
                raise ContainmentError("synthetic_fixture", "descendant survived task-domain teardown")

        cpu_root = base / "cpu-root"
        cpu_workspace = base / "cpu-workspace"
        cpu_home = base / "cpu-home"
        cpu_inbox = base / "cpu-inbox"
        cpu_outbox = base / "cpu-outbox"
        cpu_fixture = base / "cpu.sh"
        for path in (cpu_root, cpu_workspace, cpu_home, cpu_inbox, cpu_outbox):
            path.mkdir()
        cpu_fixture.write_text(_cpu_fixture_script(), encoding="utf-8")
        cpu_fixture.chmod(0o500)
        cpu_command = task_domain_command(identity, cpu_root, cpu_workspace, cpu_home,
                                           cpu_inbox, cpu_outbox, cpu_fixture, os.getpid(),
                                           cpu_seconds=1, processes=32)
        cpu = run_task_domain(cpu_command, wall_seconds=10)
        if cpu.timed_out or cpu.returncode == 0:
            raise ContainmentError("synthetic_fixture", "CPU limit did not bound the busy-loop fixture")
    return {"schema": CONSTRUCTOR_SCHEMA, "backend": dataclasses.asdict(identity),
            "workspace_writable": True, "inbox_read_only": True, "outbox_writable": True,
            "hostile_denials": "passed", "normal_completion": True,
            "wall_timeout": {"bound_seconds": 1, "supervisor_reaped": True, "descendant_stopped": True},
            "cpu_bound": {"production_seconds": DEFAULT_LIMITS["cpu_seconds"], "probe_seconds": 1},
            "resource_limits": {
                "fixture": {"processes": 32, "address_space_bytes": 134217728,
                            "open_files": 4096, "file_size_bytes": 1048576,
                            "tmpfs_bytes": 8 * 1024 * 1024,
                            "wall_seconds": 1},
                "production": {**resource_limits(), "aggregate_workspace_disk_bytes": None},
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
