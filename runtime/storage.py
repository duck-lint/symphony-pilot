"""Trusted storage-domain policy, evidence, and reservation accounting.

This module deliberately separates three facts that are easy to conflate:
the configured reservation policy, the kernel/filesystem evidence returned by
the fixed Linux capability, and the SQLite admission ledger.  A reservation
is never treated as quota enforcement; admission requires verified hard-limit
evidence first.
"""
from __future__ import annotations

import dataclasses
import json
import re
from typing import Mapping


GIB = 1024 ** 3
STORAGE_INSPECTION_SCHEMA = "symphony-pilot-quota-inspection/v1"
STORAGE_SCOPE = "persistent_project_workspace_root"
STORAGE_RESERVATION_STATUS = "reserved"
STORAGE_BYTES_MIN = 1
STORAGE_INODES_MIN = 1


class StorageContractError(RuntimeError):
    """Storage evidence or admission failed closed."""


@dataclasses.dataclass(frozen=True)
class StoragePolicy:
    """Profile-owned bounded capacity policy.

    The values are loaded from the registered profile.  Defaults exist only
    for older unit-test constructors; production profiles must spell them out
    and are validated by ``load_profile``.
    """

    pool_bytes: int = 64 * GIB
    task_bytes: int = 8 * GIB
    task_inodes: int = 250_000
    emergency_reserve_bytes: int = 8 * GIB
    emergency_reserve_inodes: int = 250_000

    def validate(self) -> "StoragePolicy":
        byte_values = (
            self.pool_bytes, self.task_bytes, self.emergency_reserve_bytes,
        )
        inode_values = (self.task_inodes, self.emergency_reserve_inodes)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < STORAGE_BYTES_MIN
               for value in byte_values):
            raise StorageContractError("storage byte policy values must be positive integers")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < STORAGE_INODES_MIN
               for value in inode_values):
            raise StorageContractError("storage inode policy values must be positive integers")
        if self.task_bytes > self.pool_bytes:
            raise StorageContractError("task storage bytes exceed the fixed storage pool")
        if self.emergency_reserve_bytes < self.task_bytes:
            raise StorageContractError("emergency byte reserve must cover one full task")
        if self.emergency_reserve_inodes < self.task_inodes:
            raise StorageContractError("emergency inode reserve must cover one full task")
        if self.task_bytes + self.emergency_reserve_bytes > self.pool_bytes:
            raise StorageContractError("task plus emergency reserve exceeds the fixed storage pool")
        return self


@dataclasses.dataclass(frozen=True)
class VerifiedStorageDomain:
    project: str
    source: str
    target: str
    fstype: str
    options: str
    pool_bytes: int
    pool_inodes: int
    free_bytes: int
    free_inodes: int
    evidence_json: str

    @property
    def used_bytes(self) -> int:
        return self.pool_bytes - self.free_bytes

    @property
    def used_inodes(self) -> int:
        return self.pool_inodes - self.free_inodes


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageContractError(f"storage evidence field {field} is not a non-negative integer")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or any(char in value for char in "\x00\r\n"):
        raise StorageContractError(f"storage evidence field {field} is malformed")
    return value


def verify_storage_evidence(
    project: str,
    evidence: Mapping[str, object],
    policy: StoragePolicy,
    *,
    expected_target: str,
) -> VerifiedStorageDomain:
    """Accept only a dedicated domain with demonstrated hard quota limits."""
    policy.validate()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", project):
        raise StorageContractError("storage project identity is malformed")
    if evidence.get("schema") != STORAGE_INSPECTION_SCHEMA or evidence.get("scope") != STORAGE_SCOPE:
        raise StorageContractError("storage evidence schema or scope is unsupported")
    if evidence.get("project") != project:
        raise StorageContractError("storage evidence belongs to another project")
    filesystem = evidence.get("filesystem")
    if not isinstance(filesystem, Mapping):
        raise StorageContractError("storage filesystem evidence is missing")
    target = _text(filesystem.get("target"), "target")
    source = _text(filesystem.get("source"), "source")
    fstype = _text(filesystem.get("fstype"), "fstype")
    options = _text(filesystem.get("options"), "options")
    if target != expected_target:
        raise StorageContractError("persistent workspace is not mounted on the expected dedicated target")
    if target == "/" or source == "/dev/sdd":
        raise StorageContractError("the ordinary Ubuntu root filesystem is not a storage domain")
    if fstype != "ext4":
        raise StorageContractError("storage domain must be ext4")
    option_set = frozenset(options.split(","))
    if not option_set.intersection({"prjquota", "pquota"}):
        raise StorageContractError("storage domain is not mounted with project quotas")
    statvfs = filesystem.get("statvfs")
    if not isinstance(statvfs, Mapping):
        raise StorageContractError("storage statvfs evidence is missing")
    block_size = _positive_integer(statvfs.get("block_size"), "block_size")
    blocks = _positive_integer(statvfs.get("blocks"), "blocks")
    free_blocks = _positive_integer(statvfs.get("free_blocks"), "free_blocks")
    inodes = _positive_integer(statvfs.get("inodes"), "inodes")
    free_inodes = _positive_integer(statvfs.get("free_inodes"), "free_inodes")
    if not block_size or free_blocks > blocks or free_inodes > inodes:
        raise StorageContractError("storage statvfs capacities are inconsistent")
    pool_bytes = block_size * blocks
    free_bytes = block_size * free_blocks
    if pool_bytes < policy.pool_bytes:
        raise StorageContractError("dedicated storage pool is smaller than its configured policy")
    if inodes < policy.task_inodes + policy.emergency_reserve_inodes:
        raise StorageContractError("dedicated storage pool lacks inode headroom")

    quota = evidence.get("quota")
    if not isinstance(quota, Mapping):
        raise StorageContractError("storage evidence has no quota enforcement proof")
    if quota.get("backend") != "ext4-project-quota":
        raise StorageContractError("unsupported storage quota backend")
    if quota.get("identity_applicable") is not True:
        raise StorageContractError("task project quota identity is not proven applicable")
    if quota.get("byte_hard_limit_enforced") is not True:
        raise StorageContractError("task byte hard-limit enforcement is not proven")
    if quota.get("inode_hard_limit_enforced") is not True:
        raise StorageContractError("task inode hard-limit enforcement is not proven")
    ownership = evidence.get("ownership")
    if not isinstance(ownership, Mapping) or ownership.get("trusted") is not True:
        raise StorageContractError("storage-domain ownership is not trusted")

    bounded = {
        "schema": STORAGE_INSPECTION_SCHEMA,
        "project": project,
        "scope": STORAGE_SCOPE,
        "filesystem": dict(filesystem),
        "quota": dict(quota),
        "ownership": dict(ownership),
    }
    return VerifiedStorageDomain(
        project=project, source=source, target=target, fstype=fstype, options=options,
        pool_bytes=pool_bytes, pool_inodes=inodes, free_bytes=free_bytes,
        free_inodes=free_inodes,
        evidence_json=json.dumps(bounded, sort_keys=True, separators=(",", ":")),
    )


def derive_quota_id(task_identifier: str) -> int:
    """Derive a stable numeric project-quota identity from host task identity."""
    match = re.fullmatch(r"T-([0-9]{6})", task_identifier)
    if not match:
        raise StorageContractError("quota identity requires a host task identifier")
    return 1_000_000 + int(match.group(1))


def capacity_snapshot(
    domain: Mapping[str, object], reserved_bytes: int, reserved_inodes: int,
    *, configured_pool_bytes: int | None = None,
) -> dict[str, int | str]:
    """Calculate UI/accounting values from one verified domain snapshot."""
    observed_pool_bytes = int(domain["pool_bytes"])
    pool_bytes = observed_pool_bytes if configured_pool_bytes is None else configured_pool_bytes
    pool_inodes = int(domain["pool_inodes"])
    free_bytes = int(domain["free_bytes"])
    free_inodes = int(domain["free_inodes"])
    used_bytes = observed_pool_bytes - free_bytes
    used_inodes = pool_inodes - free_inodes
    return {
        "pool_bytes": pool_bytes,
        "pool_inodes": pool_inodes,
        "used_bytes": used_bytes,
        "used_inodes": used_inodes,
        "reserved_bytes": reserved_bytes,
        "reserved_inodes": reserved_inodes,
        "available_bytes": max(0, pool_bytes - used_bytes - reserved_bytes),
        "available_inodes": max(0, pool_inodes - used_inodes - reserved_inodes),
    }
