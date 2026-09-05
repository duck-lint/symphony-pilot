#!/usr/bin/env python3
"""Bounded Windows-host entry into the one supported WSL distribution.

This module is deliberately narrower than a general host command broker.  It
can only launch a Linux argv in Ubuntu-24.04 as duck-lint, after the requested
project cwd has been canonicalized by that same distribution.  Windows command
selection, inherited credentials, and arbitrary cwd aliases never cross the
adapter boundary.
"""
from __future__ import annotations

import argparse
import ctypes
import dataclasses
import datetime as dt
import json
import os
import pathlib
import posixpath
import re
import subprocess
import sys
import threading
import time
import uuid
from typing import Sequence


FIXED_DISTRO = "Ubuntu-24.04"
EXPECTED_USER = "duck-lint"
DEFAULT_TIMEOUT_SECONDS = 30 * 60
MAX_TIMEOUT_SECONDS = 60 * 60
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_ARGUMENT_BYTES = 1024 * 1024

PROJECT_ROOTS = {
    "symphony-pilot": "/mnt/f/PROJECT-REPOS/symphony-pilot",
    "symphony-runtime": "/mnt/f/PROJECT-REPOS/symphony-runtime",
}
# Storage inspection is a separate fixed capability. It does not expand the
# executable project-root map or permit a caller-selected Linux path.
STORAGE_PROJECTS = frozenset({"symphony-pilot", "symphony-runtime", "symphony-canary"})
CONTROL_DEPLOYMENT_PROFILE = "symphony-canary"
CONTROL_DEPLOYMENT_ROOT = (
    "/home/duck-lint/.local/share/symphony-pilot/deployments/"
    + CONTROL_DEPLOYMENT_PROFILE
)
CONTAINED_ENTRYPOINT = (
    CONTROL_DEPLOYMENT_ROOT + "/"
    "runtime/wsl_contained_exec.py"
)

_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_WINDOWS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)")
_WINDOWS_EXECUTABLE = re.compile(r"(?i)^(?:cmd|powershell|pwsh|wsl|docker)(?:\.exe)?$")


class WslAdapterError(RuntimeError):
    """A fail-closed validation or transport error at the WSL boundary."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclasses.dataclass(frozen=True)
class WslExecutionResult:
    request_id: str
    project: str
    distro: str
    cwd: str
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    termination: str
    stdout_truncated: bool
    stderr_truncated: bool
    started_at: str
    finished_at: str
    approval: str = "none"

    def audit_record(self) -> dict[str, object]:
        """Return safe host audit evidence without command or output content."""
        return {
            "request_id": self.request_id,
            "project": self.project,
            "distro": self.distro,
            "cwd": self.cwd,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "termination": self.termination,
            "stdout_bytes": len(self.stdout.encode("utf-8")),
            "stderr_bytes": len(self.stderr.encode("utf-8")),
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "approval": self.approval,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.audit_record(), "stdout": self.stdout, "stderr": self.stderr}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _windows_system_directory() -> pathlib.Path:
    if os.name != "nt":
        raise WslAdapterError("host_platform", "the WSL adapter requires a Windows host")
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise WslAdapterError("wsl_unavailable", "Windows system directory is unavailable")
    return pathlib.Path(buffer.value).resolve()


def _host_wsl_executable() -> pathlib.Path:
    path = (_windows_system_directory() / "wsl.exe").resolve()
    if not path.is_file() or path.name.lower() != "wsl.exe":
        raise WslAdapterError("wsl_unavailable", "the fixed Windows WSL executable is unavailable")
    return path


def _sterile_windows_environment() -> dict[str, str]:
    system_root = str(_windows_system_directory().parent)
    # wsl.exe needs only the Windows system identity.  In particular, do not
    # inherit PATH, WSLENV, credential variables, or application secrets.
    return {"SystemRoot": system_root, "WINDIR": system_root}


def _validate_project(project: str) -> None:
    if project not in PROJECT_ROOTS:
        raise WslAdapterError("unknown_project", "project is not an approved SYMPHONY project")


def _validate_storage_project(project: str) -> None:
    if project not in STORAGE_PROJECTS:
        raise WslAdapterError("unknown_storage_project", "project is not approved for storage inspection")


def _validate_request_id(request_id: str) -> None:
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise WslAdapterError("invalid_request_id", "request identity is malformed")


def _lexical_cwd(project: str, cwd: str) -> str:
    _validate_project(project)
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
        raise WslAdapterError("invalid_cwd", "Linux cwd is missing or malformed")
    if "\\" in cwd or ":" in cwd or cwd.startswith("//"):
        raise WslAdapterError("invalid_cwd", "Windows and UNC path forms are not accepted")
    normalized = posixpath.normpath(cwd)
    if not normalized.startswith("/"):
        raise WslAdapterError("invalid_cwd", "Linux cwd must be absolute")
    if not _within(normalized, _allowed_roots(project)):
        raise WslAdapterError("cwd_outside_project", "Linux cwd is outside the approved project roots")
    return normalized


def _allowed_roots(project: str) -> tuple[str, ...]:
    return (PROJECT_ROOTS[project],)


def _within(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)) or not command:
        raise WslAdapterError("invalid_command", "Linux command must be a non-empty argv")
    try:
        command = tuple(command)
    except TypeError as exc:
        raise WslAdapterError("invalid_command", "Linux command must be a non-empty argv") from exc
    if any(not isinstance(argument, str) or "\x00" in argument for argument in command):
        raise WslAdapterError("invalid_command", "Linux argv contains a malformed argument")
    if sum(len(argument.encode("utf-8")) for argument in command) > MAX_ARGUMENT_BYTES:
        raise WslAdapterError("invalid_command", "Linux argv exceeds the adapter argument bound")
    executable = pathlib.PurePosixPath(command[0]).name
    if _WINDOWS_EXECUTABLE.fullmatch(executable):
        raise WslAdapterError("windows_command_boundary", "a Windows executable cannot be selected as the Linux command")
    if executable == "bash":
        if len(command) != 3 or command[1] != "-lc" or not command[2]:
            raise WslAdapterError("shell_boundary", "only one explicit bash -lc boundary is supported")
        return ("/usr/bin/bash", "--noprofile", "--norc", "-lc", command[2])
    return tuple(command)


def _raw_command(wsl: pathlib.Path, arguments: Sequence[str], cwd: str) -> list[str]:
    return [
        str(wsl),
        "--distribution",
        FIXED_DISTRO,
        "--user",
        EXPECTED_USER,
        "--cd",
        cwd,
        "--exec",
        *arguments,
    ]


def _decode_output(raw: bytes, truncated: bool) -> str:
    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return raw.decode("utf-16")
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        if truncated:
            return raw.decode("utf-8", errors="replace")
        raise WslAdapterError("malformed_output", "WSL returned malformed output") from exc


def _bounded_process(
    command: Sequence[str], timeout_seconds: float, *, cwd: str, metadata: tuple[str, str, str]
) -> WslExecutionResult:
    request_id, project, distro = metadata
    started_at = _utc_now()
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    output_limit = threading.Event()
    reader_error = threading.Event()

    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_sterile_windows_environment(),
            shell=False,
            close_fds=True,
        )
    except (OSError, ValueError) as exc:
        raise WslAdapterError("wsl_unavailable", "the fixed WSL process could not start") from exc

    def drain(stream: object, buffer: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(8192)  # type: ignore[union-attr]
                if not chunk:
                    return
                remaining = MAX_OUTPUT_BYTES - len(buffer)
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
    deadline = time.monotonic() + timeout_seconds
    while process.poll() is None:
        if reader_error.is_set():
            termination = "output_error"
            process.kill()
            break
        if output_limit.is_set():
            termination = "output_limit"
            process.kill()
            break
        if time.monotonic() >= deadline:
            termination = "timeout"
            process.kill()
            break
        time.sleep(0.01)

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise WslAdapterError("termination_failed", "WSL process did not terminate after bounded cleanup") from exc
    stdout_thread.join(timeout=10)
    stderr_thread.join(timeout=10)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        raise WslAdapterError("termination_failed", "WSL output reader did not terminate")
    if reader_error.is_set():
        raise WslAdapterError("output_read", "WSL output could not be read safely")
    if output_limit.is_set() and termination == "completed":
        termination = "output_limit"

    stdout_truncated = output_limit.is_set() and len(stdout_buffer) == MAX_OUTPUT_BYTES
    stderr_truncated = output_limit.is_set() and len(stderr_buffer) == MAX_OUTPUT_BYTES
    return WslExecutionResult(
        request_id=request_id,
        project=project,
        distro=distro,
        cwd=cwd,
        returncode=process.returncode,
        stdout=_decode_output(bytes(stdout_buffer), stdout_truncated),
        stderr=_decode_output(bytes(stderr_buffer), stderr_truncated),
        timed_out=termination == "timeout",
        termination=termination,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        started_at=started_at,
        finished_at=_utc_now(),
    )


def _raise_if_wsl_transport_failed(result: WslExecutionResult) -> None:
    """Keep WSL service failures distinct from an ordinary Linux command error."""
    # Some Windows WSL failures arrive as UTF-16 bytes without a BOM.  The
    # normal decoder preserves those NULs so malformed output remains visible;
    # service classification may safely remove them because it never returns
    # or logs this diagnostic text.
    service_output = f"{result.stdout}\n{result.stderr}".replace("\x00", "")
    if result.returncode in (-1, 0xFFFFFFFF) and re.search(
        r"(?i)(?:wsl/|access\s+is\s+denied|createinstance|enumeratedistros)", service_output
    ):
        raise WslAdapterError("wsl_unavailable", "the fixed WSL distribution is unavailable")


def _canonicalize_cwd(project: str, cwd: str, wsl: pathlib.Path, request_id: str) -> str:
    lexical = _lexical_cwd(project, cwd)
    result = _bounded_process(
        _raw_command(wsl, ["/usr/bin/readlink", "-e", "--", lexical], "/"),
        10,
        cwd="/",
        metadata=(request_id, project, FIXED_DISTRO),
    )
    _raise_if_wsl_transport_failed(result)
    if result.returncode != 0:
        raise WslAdapterError("cwd_unresolvable", "Linux cwd could not be canonicalized")
    resolved_lines = result.stdout.strip().splitlines()
    if len(resolved_lines) != 1 or not resolved_lines[0].startswith("/"):
        raise WslAdapterError("cwd_unresolvable", "WSL cwd resolution returned malformed evidence")
    resolved = posixpath.normpath(resolved_lines[0])
    if not _within(resolved, _allowed_roots(project)):
        raise WslAdapterError("cwd_symlink_escape", "canonical Linux cwd leaves the approved project roots")

    directory = _bounded_process(
        _raw_command(wsl, ["/usr/bin/test", "-d", resolved], "/"),
        10,
        cwd="/",
        metadata=(request_id, project, FIXED_DISTRO),
    )
    if directory.returncode != 0:
        raise WslAdapterError("cwd_not_directory", "canonical Linux cwd is not a directory")
    return resolved


def execute(
    project: str,
    cwd: str,
    command: Sequence[str],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    request_id: str | None = None,
    distro: str = FIXED_DISTRO,
) -> WslExecutionResult:
    """Execute one bounded Linux argv through fixed Ubuntu-24.04 WSL entry."""
    if distro != FIXED_DISTRO:
        raise WslAdapterError("fixed_distro", "only Ubuntu-24.04 is supported")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise WslAdapterError("invalid_timeout", "timeout is outside the bounded adapter range")
    _validate_project(project)
    request_id = request_id or uuid.uuid4().hex
    _validate_request_id(request_id)
    validated_command = _validate_command(command)
    wsl = _host_wsl_executable()
    resolved_cwd = _canonicalize_cwd(project, cwd, wsl, request_id)
    command = _raw_command(wsl, [
        "/usr/bin/env", "-i",
        "HOME=/home/duck-lint", "USER=duck-lint", "LOGNAME=duck-lint",
        "PATH=/usr/bin:/bin", "LANG=C.UTF-8", "LC_ALL=C.UTF-8",
        # The deployed supervisor imports authority modules from its own
        # manifest-covered directory.  Python bytecode would be an unlisted
        # file mutation of that directory, so disable writes at interpreter
        # startup rather than relying on cleanup after the run.
        "/usr/bin/python3", "-B", CONTAINED_ENTRYPOINT,
        "--project", project, "--cwd", resolved_cwd,
        "--wall-seconds", str(max(1.0, float(timeout_seconds) - 5.0)), "--",
        *validated_command,
    ], "/")
    return _bounded_process(
        command,
        float(timeout_seconds),
        cwd=resolved_cwd,
        metadata=(request_id, project, FIXED_DISTRO),
    )


def inspect_quota(
    project: str,
    *,
    timeout_seconds: float = 30,
    request_id: str | None = None,
) -> dict[str, object]:
    """Inspect one derived persistent project-storage domain through the fixed supervisor."""
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise WslAdapterError("invalid_timeout", "timeout is outside the bounded adapter range")
    _validate_storage_project(project)
    request_id = request_id or uuid.uuid4().hex
    _validate_request_id(request_id)
    wsl = _host_wsl_executable()
    command = _raw_command(wsl, [
        "/usr/bin/env", "-i",
        "HOME=/home/duck-lint", "USER=duck-lint", "LOGNAME=duck-lint",
        "PATH=/usr/bin:/bin", "LANG=C.UTF-8", "LC_ALL=C.UTF-8",
        "/usr/bin/python3", "-B", CONTAINED_ENTRYPOINT,
        "--control", "quota-inspect-root", "--project", project,
    ], "/")
    result = _bounded_process(
        command, float(timeout_seconds), cwd="/", metadata=(request_id, project, FIXED_DISTRO)
    )
    _raise_if_wsl_transport_failed(result)
    if result.returncode != 0 or result.timed_out:
        raise WslAdapterError("quota_inspection", "trusted Linux quota inspection failed")
    try:
        value = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WslAdapterError("quota_inspection", "trusted Linux quota inspection returned malformed JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != "symphony-pilot-quota-inspection/v1":
        raise WslAdapterError("quota_inspection", "trusted Linux quota inspection returned an unsupported shape")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, choices=sorted(PROJECT_ROOTS))
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--request-id")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    try:
        result = execute(
            args.project,
            args.cwd,
            command,
            timeout_seconds=args.timeout_seconds,
            request_id=args.request_id,
        )
    except WslAdapterError as exc:
        print(f"symphony-pilot WSL adapter stopped: {exc.kind}: {exc}", file=sys.stderr)
        return 78
    print(json.dumps(result.as_dict(), sort_keys=True))
    return result.returncode if result.returncode not in (None, 0) else (124 if result.timed_out else 0)


if __name__ == "__main__":
    raise SystemExit(main())
