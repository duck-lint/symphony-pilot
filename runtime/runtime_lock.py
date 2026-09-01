#!/usr/bin/env python3
"""Reviewed executable identities for unattended host startup."""
from __future__ import annotations

import dataclasses
import hashlib
import pathlib
import shutil
import subprocess
import re


LOCK_SCHEMA = "symphony-pilot-runtime-lock/v1"


class RuntimeLockError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class ExecutableIdentity:
    executable: str
    version: str
    sha256: str

    def as_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


def identify(executable: str, version_args: tuple[str, ...] = ("--version",)) -> ExecutableIdentity:
    path = pathlib.Path(executable).resolve()
    if not path.is_file() or not path.stat().st_mode & 0o111:
        raise RuntimeLockError(f"runtime executable is not executable: {path}")
    try:
        result = subprocess.run([str(path), *version_args], capture_output=True, text=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeLockError(f"cannot identify runtime executable: {path}") from exc
    output = (result.stdout or result.stderr).strip().splitlines()
    if not output:
        raise RuntimeLockError(f"runtime executable did not report a version: {path}")
    return ExecutableIdentity(str(path), output[0], hashlib.sha256(path.read_bytes()).hexdigest())


def validate_lock(lock: object) -> dict[str, object]:
    if not isinstance(lock, dict) or set(lock) != {"schema", "symphony", "codex", "containment"}:
        raise RuntimeLockError("runtime lock fields are invalid")
    if lock["schema"] != LOCK_SCHEMA:
        raise RuntimeLockError("runtime lock schema is not accepted")
    for name in ("symphony", "codex", "containment"):
        value = lock[name]
        if not isinstance(value, dict) or set(value) != {"executable", "version", "sha256"}:
            raise RuntimeLockError(f"runtime lock entry is invalid: {name}")
        if not isinstance(value["executable"], str) or not value["executable"]:
            raise RuntimeLockError(f"runtime lock executable is invalid: {name}")
        if not isinstance(value["version"], str) or not value["version"]:
            raise RuntimeLockError(f"runtime lock version is invalid: {name}")
        if not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
            raise RuntimeLockError(f"runtime lock digest is invalid: {name}")
    return lock


def verify_entry(expected: dict[str, str], actual: ExecutableIdentity, name: str) -> None:
    if expected != actual.as_dict():
        raise RuntimeLockError(f"{name} runtime identity differs from the accepted lock")


def build_lock(symphony: str, codex: str) -> dict[str, object]:
    from containment import backend_identity

    containment = backend_identity()
    return {
        "schema": LOCK_SCHEMA,
        "symphony": identify(symphony).as_dict(),
        "codex": identify(codex).as_dict(),
        "containment": {
            "executable": containment.executable,
            "version": containment.version,
            "sha256": containment.sha256,
        },
    }


def discover(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise RuntimeLockError(f"official {name} executable was not found on PATH")
    return value
