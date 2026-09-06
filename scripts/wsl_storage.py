"""WSL-native transport for the three trusted storage capabilities.

The normal ``wsl_adapter`` is intentionally a Windows-host bridge and must
remain Windows-only.  These host lifecycle CLIs run inside WSL, so this
module invokes the already-deployed Linux supervisor directly.  The public
surface contains only the fixed storage controls; it is not a general argv
or path transport.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass


SUPERVISOR = pathlib.Path(
    "/home/duck-lint/.local/share/symphony-pilot/deployments/"
    "symphony-canary/runtime/wsl_contained_exec.py"
)
PYTHON = pathlib.PurePosixPath("/usr/bin/python3")
WORKING_DIRECTORY = "/"
MAX_TIMEOUT_SECONDS = 60 * 60
DEFAULT_TIMEOUT_SECONDS = 30
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
STORAGE_PROJECTS = frozenset({
    "cleanroom", "symphony-pilot", "symphony-runtime", "symphony-canary",
})
REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
TASK_IDENTIFIER = re.compile(r"T-[0-9]{6}\Z")


class WslStorageError(RuntimeError):
    """A fail-closed WSL-native storage transport error."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


def _validate_timeout(timeout_seconds: float) -> None:
    if (isinstance(timeout_seconds, bool) or
            not isinstance(timeout_seconds, (int, float)) or
            not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS):
        raise WslStorageError("invalid_timeout", "timeout is outside the bounded storage range")


def _validate_project(project: str) -> None:
    if project not in STORAGE_PROJECTS:
        raise WslStorageError("unknown_storage_project", "project is not approved for storage inspection")


def _validate_request_id(request_id: str) -> None:
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise WslStorageError("invalid_request_id", "request identity is malformed")


def _validate_identifier(identifier: str) -> None:
    if not isinstance(identifier, str) or not TASK_IDENTIFIER.fullmatch(identifier):
        raise WslStorageError("invalid_task_identity", "task identifier is malformed")


def _validate_limits(byte_limit: int, inode_limit: int) -> None:
    if (isinstance(byte_limit, bool) or not isinstance(byte_limit, int) or byte_limit <= 0 or
            isinstance(inode_limit, bool) or not isinstance(inode_limit, int) or inode_limit <= 0):
        raise WslStorageError("invalid_quota_policy", "task quota limits are malformed")


def _read_bounded(stream: object, buffer: bytearray, overflow: threading.Event,
                  failed: threading.Event) -> None:
    try:
        while True:
            chunk = stream.read(8192)  # type: ignore[union-attr]
            if not chunk:
                return
            remaining = MAX_OUTPUT_BYTES - len(buffer)
            if remaining <= 0:
                overflow.set()
                return
            buffer.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow.set()
                return
    except (OSError, ValueError):
        failed.set()


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WslStorageError("malformed_output", "WSL storage control returned malformed output") from exc


def _bounded_process(arguments: tuple[str, ...], timeout_seconds: float) -> _ProcessResult:
    """Run one fixed supervisor argv with bounded pipes and lifetime."""
    _validate_timeout(timeout_seconds)
    if os.name == "nt":
        raise WslStorageError("host_platform", "WSL-native storage transport requires a Linux host")
    if not SUPERVISOR.is_absolute() or SUPERVISOR.is_symlink():
        raise WslStorageError("supervisor_identity", "the fixed deployed storage supervisor is unavailable")

    command = (str(PYTHON), "-B", str(SUPERVISOR), *arguments)
    environment = {
        "HOME": "/home/duck-lint",
        "USER": "duck-lint",
        "LOGNAME": "duck-lint",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=WORKING_DIRECTORY,
            env=environment,
            shell=False,
            close_fds=True,
        )
    except (OSError, ValueError) as exc:
        raise WslStorageError("supervisor_unavailable", "the fixed storage supervisor could not start") from exc

    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    overflow = threading.Event()
    reader_failed = threading.Event()
    stdout_thread = threading.Thread(
        target=_read_bounded, args=(process.stdout, stdout_buffer, overflow, reader_failed), daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_bounded, args=(process.stderr, stderr_buffer, overflow, reader_failed), daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    deadline = time.monotonic() + float(timeout_seconds)
    timed_out = False
    try:
        while process.poll() is None:
            if reader_failed.is_set() or overflow.is_set():
                process.kill()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                process.kill()
                break
            time.sleep(0.01)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise WslStorageError(
                "termination_failed", "the storage supervisor did not terminate after bounded cleanup"
            ) from exc
    finally:
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        raise WslStorageError("termination_failed", "storage supervisor output reader did not terminate")
    if reader_failed.is_set():
        raise WslStorageError("output_read", "storage supervisor output could not be read safely")
    if overflow.is_set():
        raise WslStorageError("output_limit", "storage supervisor output exceeded its bound")
    return _ProcessResult(
        returncode=process.returncode,
        stdout=_decode(bytes(stdout_buffer)),
        stderr=_decode(bytes(stderr_buffer)),
        timed_out=timed_out,
    )


def _run_control(arguments: tuple[str, ...], timeout_seconds: float, kind: str) -> dict[str, object]:
    result = _bounded_process(arguments, timeout_seconds)
    if result.timed_out or result.returncode != 0:
        raise WslStorageError(kind, "trusted Linux storage control failed")
    try:
        value = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WslStorageError(kind, "trusted Linux storage control returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise WslStorageError(kind, "trusted Linux storage control returned an unsupported shape")
    return value


def inspect_quota(
    project: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    request_id: str | None = None,
) -> dict[str, object]:
    """Inspect the shared pool through the fixed deployed supervisor."""
    _validate_timeout(timeout_seconds)
    _validate_project(project)
    request_id = request_id or uuid.uuid4().hex
    _validate_request_id(request_id)
    value = _run_control(
        ("--control", "quota-inspect-root", "--project", project),
        timeout_seconds, "quota_inspection",
    )
    if value.get("schema") != "symphony-pilot-quota-inspection/v1":
        raise WslStorageError("quota_inspection", "trusted Linux quota inspection returned an unsupported shape")
    return value


def admit_task_quota(
    project: str, identifier: str, *, byte_limit: int, inode_limit: int,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, request_id: str | None = None,
) -> dict[str, object]:
    """Admit one host-derived task through the fixed quota capability."""
    _validate_timeout(timeout_seconds)
    _validate_project(project)
    _validate_identifier(identifier)
    _validate_limits(byte_limit, inode_limit)
    request_id = request_id or uuid.uuid4().hex
    _validate_request_id(request_id)
    value = _run_control(
        ("--control", "quota-admit-task", "--project", project,
         "--identifier", identifier, "--byte-limit", str(byte_limit),
         "--inode-limit", str(inode_limit)),
        timeout_seconds, "quota_admission",
    )
    if (set(value) != {"schema", "project", "pool", "task_quota"} or
            value.get("schema") != "symphony-pilot-task-quota-admission/v1" or
            value.get("project") != project):
        raise WslStorageError("quota_admission", "trusted task quota admission returned an unsupported shape")
    return value


def release_task_quota(
    project: str, identifier: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    request_id: str | None = None,
) -> dict[str, object]:
    """Obtain exact cleanup evidence through the fixed quota capability."""
    _validate_timeout(timeout_seconds)
    _validate_project(project)
    _validate_identifier(identifier)
    request_id = request_id or uuid.uuid4().hex
    _validate_request_id(request_id)
    value = _run_control(
        ("--control", "quota-release-task", "--project", project,
         "--identifier", identifier),
        timeout_seconds, "quota_cleanup",
    )
    expected_path = f"/home/duck-lint/symphony-workspaces/{project}/{identifier}"
    expected_id = 1_000_000 + int(identifier[2:])
    if (set(value) != {
            "schema", "project", "identifier", "workspace_path", "project_id",
            "workspace_state", "quota_state", "growth_possible", "remaining_bytes",
            "remaining_inodes"
        } or value.get("schema") != "symphony-pilot-task-quota-release/v1" or
            value.get("project") != project or value.get("identifier") != identifier or
            value.get("workspace_path") != expected_path or value.get("project_id") != expected_id or
            value.get("workspace_state") != "destroyed" or value.get("quota_state") != "removed" or
            value.get("growth_possible") is not False or value.get("remaining_bytes") != 0 or
            value.get("remaining_inodes") != 0):
        raise WslStorageError("quota_cleanup", "trusted task quota cleanup returned an unsupported shape")
    return value
