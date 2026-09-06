"""Machine-readable Step-8 target contract and bounded host evidence helpers."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import platform
import re
import subprocess
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "host-contract" / "symphony-target-v1.json"
CONTRACT_SCHEMA = "symphony-pilot-host-contract/v1"


class HostContractError(ValueError):
    """The versioned host contract or observed evidence is malformed."""


def load_contract(path: pathlib.Path = CONTRACT_PATH) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HostContractError("host contract cannot be read") from exc
    if not isinstance(document, dict) or document.get("schema") != CONTRACT_SCHEMA:
        raise HostContractError("host contract schema is not accepted")
    if document.get("version") != 1:
        raise HostContractError("host contract version is not accepted")
    for key in ("exact_identity", "compatible_buildability", "informational"):
        if not isinstance(document.get(key), dict):
            raise HostContractError(f"host contract field is malformed: {key}")
    return document


def contract_digest(path: pathlib.Path = CONTRACT_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _walk_exact(expected: Any, observed: Any, path: str = "") -> list[str]:
    if isinstance(expected, dict):
        if not isinstance(observed, dict):
            return [path or "root"]
        failures: list[str] = []
        for key, value in expected.items():
            child = f"{path}.{key}" if path else key
            if key not in observed:
                failures.append(child)
            else:
                failures.extend(_walk_exact(value, observed[key], child))
        return failures
    if expected != observed:
        return [path]
    return []


def verify_exact_identity(contract: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    """Return missing/mismatched exact fields without accepting extra authority."""
    return _walk_exact(contract["exact_identity"], observed)


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value))


def _contains(value: Any, fragment: str) -> bool:
    return isinstance(value, str) and fragment.lower() in value.lower()


def verify_ubuntu_buildability(contract: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    """Check hosted Ubuntu compatibility, not WSL-kernel equivalence."""
    expected = contract["compatible_buildability"]["ubuntu_linux"]
    failures: list[str] = []
    os_release = observed.get("os_release", {})
    if os_release.get("ID") != expected["os_id"]:
        failures.append("os_release.ID")
    if not str(os_release.get("VERSION_ID", "")).startswith(expected["version_prefix"]):
        failures.append("os_release.VERSION_ID")
    if observed.get("architecture") != expected["architecture"]:
        failures.append("architecture")
    compiler = observed.get("compiler", {})
    if compiler.get("path") != expected["compiler_path"] or not compiler.get("executable"):
        failures.append("compiler.path")
    compiler_output = compiler.get("version_output", "")
    if not (_contains(compiler_output, expected["compiler_family"]) or
            _contains(compiler_output, "cc (")):
        failures.append("compiler.family")
    version = _version_tuple(compiler_output)
    if not version or version[0] != expected["compiler_major"]:
        failures.append("compiler.major")
    if not _contains(observed.get("glibc"), expected["glibc_family"]):
        failures.append("glibc.family")
    if not _contains(observed.get("e2fsprogs"), expected["e2fsprogs_family"]):
        failures.append("e2fsprogs.family")
    minimum = _version_tuple(expected["util_linux_minimum"])
    for name in ("mount", "findmnt"):
        value = observed.get("util_linux", {}).get(name, "")
        if not _contains(value, "util-linux"):
            failures.append(f"util_linux.{name}.identity")
        if _version_tuple(value) < minimum:
            failures.append(f"util_linux.{name}.version")
    if observed.get("quotactl_fd") != expected["quotactl_fd"]:
        failures.append("quotactl_fd")
    return failures


def _run(argv: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr


def _parse_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in pathlib.Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def _version_output(argv: list[str]) -> str:
    _, stdout, stderr = _run(argv)
    return (stdout + stderr).strip()


def _quotactl_fd_header_value() -> int | None:
    candidates = (
        pathlib.Path("/usr/include/x86_64-linux-gnu/asm/unistd_64.h"),
        pathlib.Path("/usr/include/asm-generic/unistd.h"),
    )
    for path in candidates:
        if not path.is_file():
            continue
        match = re.search(r"#define __NR_quotactl_fd\s+(\d+)", path.read_text(encoding="utf-8"))
        if match:
            return int(match.group(1))
    return None


def collect_ubuntu_observation() -> dict[str, Any]:
    compiler = pathlib.Path("/usr/bin/cc")
    return {
        "platform": {"system": platform.system(), "release": platform.release(), "uname": platform.uname()._asdict()},
        "architecture": platform.machine(),
        "os_release": _parse_os_release(),
        "compiler": {
            "path": str(compiler),
            "executable": compiler.is_file() and os.access(compiler, os.X_OK),
            "version_output": _version_output([str(compiler), "--version"]) if compiler.exists() else "",
        },
        "glibc": _version_output(["ldd", "--version"]),
        "e2fsprogs": _version_output(["mke2fs", "-V"]),
        "util_linux": {
            "mount": _version_output(["mount", "--version"]),
            "findmnt": _version_output(["findmnt", "--version"]),
        },
        "quotactl_fd": _quotactl_fd_header_value(),
    }
