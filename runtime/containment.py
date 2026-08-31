#!/usr/bin/env python3
"""The one supported Linux task-containment contract.

This module deliberately does not pretend that Codex policy is an OS boundary.
The selected backend is rootless ``unshare``.  Its capability probe is a real
namespace probe; an unavailable or incomplete backend is an infrastructure
blocker, never a reason to return to same-user execution.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import pathlib
import shutil
import subprocess
from typing import Sequence


BACKEND_SCHEMA = "symphony-pilot-containment/v1"
BACKEND_NAME = "linux-unshare"
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


if __name__ == "__main__":
    try:
        identity = require_execution_capability()
    except ContainmentError as exc:
        print(f"symphony-pilot containment blocked: {exc.kind}: {exc}")
        raise SystemExit(78)
    print(identity)
