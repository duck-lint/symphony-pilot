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
STORAGE_SCOPE = "persistent_symphony_workspace_pool"
STORAGE_POOL_ROOT = "/home/duck-lint/symphony-workspaces"
TASK_QUOTA_ADMISSION_SCHEMA = "symphony-pilot-task-quota-admission/v1"
TASK_QUOTA_RELEASE_SCHEMA = "symphony-pilot-task-quota-release/v1"
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
    allocatable_pool_bytes: int = 63 * GIB
    task_bytes: int = 8 * GIB
    task_inodes: int = 250_000
    emergency_reserve_bytes: int = 8 * GIB
    emergency_reserve_inodes: int = 250_000

    def validate(self) -> "StoragePolicy":
        byte_values = (
            self.pool_bytes, self.allocatable_pool_bytes, self.task_bytes,
            self.emergency_reserve_bytes,
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
        if self.allocatable_pool_bytes > self.pool_bytes:
            raise StorageContractError("allocatable storage exceeds nominal backing capacity")
        if self.emergency_reserve_bytes < self.task_bytes:
            raise StorageContractError("emergency byte reserve must cover one full task")
        if self.emergency_reserve_inodes < self.task_inodes:
            raise StorageContractError("emergency inode reserve must cover one full task")
        if self.task_bytes + self.emergency_reserve_bytes > self.allocatable_pool_bytes:
            raise StorageContractError("task plus emergency reserve exceeds allocatable capacity")
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


@dataclasses.dataclass(frozen=True)
class TaskQuotaBinding:
    """Host-derived task binding proved by the kernel quota capability."""

    project: str
    identifier: str
    workspace_path: str
    quota_id: int
    byte_limit: int
    inode_limit: int
    proof_json: str


@dataclasses.dataclass(frozen=True)
class StorageAdmissionProof:
    """Pool proof plus one independently verified task quota binding."""

    domain: VerifiedStorageDomain
    binding: TaskQuotaBinding


@dataclasses.dataclass(frozen=True)
class StorageReleaseProof:
    """Trusted evidence that a task can no longer grow in the pool."""

    project: str
    identifier: str
    workspace_path: str
    quota_id: int
    proof_json: str


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
    """Accept only the shared pool mount and its bounded physical evidence."""
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
        raise StorageContractError("storage pool is not mounted on the expected shared target")
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
    if pool_bytes > policy.pool_bytes:
        raise StorageContractError("filesystem usable capacity exceeds nominal backing capacity")
    if pool_bytes < policy.allocatable_pool_bytes:
        raise StorageContractError("filesystem usable capacity is below allocatable policy capacity")
    if inodes < policy.task_inodes + policy.emergency_reserve_inodes:
        raise StorageContractError("dedicated storage pool lacks inode headroom")

    quota = evidence.get("quota")
    if not isinstance(quota, Mapping):
        raise StorageContractError("storage evidence has no quota enforcement proof")
    if quota.get("backend") != "ext4-project-quota":
        raise StorageContractError("unsupported storage quota backend")
    if quota.get("mount_support") is not True:
        raise StorageContractError("storage project-quota mount support is not proven")
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


def validate_task_quota_binding(
    binding: object, *, project: str, identifier: str, policy: StoragePolicy,
) -> TaskQuotaBinding:
    """Require concrete identity, limit, and kernel-probe evidence.

    The proof is intentionally separate from pool mount evidence. A numeric
    reservation identity alone cannot authorize a task workspace.
    """
    if not isinstance(binding, TaskQuotaBinding):
        raise StorageContractError("task quota binding proof is missing")
    expected_id = derive_quota_id(identifier)
    expected_path = f"{STORAGE_POOL_ROOT}/{project}/{identifier}"
    if (binding.project != project or binding.identifier != identifier or
            binding.workspace_path != expected_path or binding.quota_id != expected_id):
        raise StorageContractError("task quota binding identity is not host-derived")
    if binding.byte_limit != policy.task_bytes or binding.inode_limit != policy.task_inodes:
        raise StorageContractError("task quota binding limits do not match policy")
    try:
        proof = json.loads(binding.proof_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageContractError("task quota binding proof is malformed") from exc
    if not isinstance(proof, Mapping) or proof.get("schema") != "symphony-pilot-task-quota-proof/v1":
        raise StorageContractError("task quota binding proof schema is unsupported")
    if proof.get("project_id") != expected_id or proof.get("workspace_project_id") != expected_id:
        raise StorageContractError("task quota identity is not proven on the workspace")
    if proof.get("byte_hard_limit") != policy.task_bytes or proof.get("inode_hard_limit") != policy.task_inodes:
        raise StorageContractError("task quota hard limits do not match policy")
    byte_probe = proof.get("byte_probe")
    inode_probe = proof.get("inode_probe")
    if (not isinstance(byte_probe, Mapping) or byte_probe.get("result") != "EDQUOT" or
            not isinstance(inode_probe, Mapping) or inode_probe.get("result") != "EDQUOT"):
        raise StorageContractError("task quota hard-limit probes did not prove kernel enforcement")
    return binding


def task_quota_binding_from_evidence(
    evidence: Mapping[str, object], *, project: str, identifier: str,
    policy: StoragePolicy,
) -> TaskQuotaBinding:
    """Construct a binding only from the fixed adapter's bounded proof."""
    policy.validate()
    if evidence.get("schema") != TASK_QUOTA_ADMISSION_SCHEMA:
        raise StorageContractError("task quota admission evidence schema is unsupported")
    if evidence.get("project") != project:
        raise StorageContractError("task quota admission belongs to another project")
    task = evidence.get("task_quota")
    if not isinstance(task, Mapping):
        raise StorageContractError("task quota admission evidence is missing task proof")
    binding = TaskQuotaBinding(
        project=project,
        identifier=identifier,
        workspace_path=task.get("workspace_path", ""),
        quota_id=task.get("project_id", -1),
        byte_limit=task.get("byte_hard_limit", -1),
        inode_limit=task.get("inode_hard_limit", -1),
        proof_json=json.dumps(dict(task), sort_keys=True, separators=(",", ":")),
    )
    if task.get("identifier") != identifier:
        raise StorageContractError("task quota admission identifier is not host-derived")
    return validate_task_quota_binding(binding, project=project, identifier=identifier, policy=policy)


def validate_storage_release_proof(
    proof: object, *, project: str, identifier: str,
) -> StorageReleaseProof:
    """Require destruction/sealing evidence before releasing commitment."""
    if not isinstance(proof, StorageReleaseProof):
        raise StorageContractError("storage reservation release requires trusted cleanup proof")
    expected_id = derive_quota_id(identifier)
    expected_path = f"{STORAGE_POOL_ROOT}/{project}/{identifier}"
    if (proof.project != project or proof.identifier != identifier or
            proof.workspace_path != expected_path or proof.quota_id != expected_id):
        raise StorageContractError("storage cleanup proof identity is not host-derived")
    try:
        evidence = json.loads(proof.proof_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageContractError("storage cleanup proof is malformed") from exc
    if not isinstance(evidence, Mapping) or evidence.get("schema") != TASK_QUOTA_RELEASE_SCHEMA:
        raise StorageContractError("storage cleanup proof schema is unsupported")
    if (evidence.get("workspace_state") != "destroyed" or
            evidence.get("quota_state") != "removed" or
            evidence.get("growth_possible") is not False or
            evidence.get("remaining_bytes") != 0 or
            evidence.get("remaining_inodes") != 0):
        raise StorageContractError("storage cleanup proof does not stop future growth")
    if evidence.get("project_id") != expected_id or evidence.get("workspace_path") != expected_path:
        raise StorageContractError("storage cleanup proof does not match the task")
    return proof


def storage_release_proof_from_evidence(
    evidence: Mapping[str, object], *, project: str, identifier: str,
) -> StorageReleaseProof:
    """Materialize cleanup authority only from fixed-capability evidence."""
    if evidence.get("schema") != TASK_QUOTA_RELEASE_SCHEMA or evidence.get("project") != project:
        raise StorageContractError("storage cleanup evidence is unsupported")
    expected_id = derive_quota_id(identifier)
    expected_path = f"{STORAGE_POOL_ROOT}/{project}/{identifier}"
    proof = StorageReleaseProof(
        project=project,
        identifier=identifier,
        workspace_path=evidence.get("workspace_path", ""),
        quota_id=evidence.get("project_id", -1),
        proof_json=json.dumps(dict(evidence), sort_keys=True, separators=(",", ":")),
    )
    if proof.quota_id != expected_id or proof.workspace_path != expected_path:
        raise StorageContractError("storage cleanup evidence identity is not host-derived")
    return proof


def capacity_snapshot(
    domain: Mapping[str, object], reserved_bytes: int, reserved_inodes: int,
    *, configured_pool_bytes: int | None = None,
    configured_backing_bytes: int | None = None,
) -> dict[str, int | str]:
    """Calculate non-overlapping physical and reservation capacity values.

    Physical free space already includes bytes consumed by reserved tasks, so
    admission availability is bounded by the lesser of physical free space
    and capacity not already committed to reservations.  It must not subtract
    both quantities from the pool a second time.
    """
    observed_pool_bytes = int(domain["pool_bytes"])
    pool_bytes = observed_pool_bytes if configured_pool_bytes is None else configured_pool_bytes
    backing_bytes = observed_pool_bytes if configured_backing_bytes is None else configured_backing_bytes
    pool_inodes = int(domain["pool_inodes"])
    free_bytes = int(domain["free_bytes"])
    free_inodes = int(domain["free_inodes"])
    used_bytes = observed_pool_bytes - free_bytes
    used_inodes = pool_inodes - free_inodes
    uncommitted_bytes = max(0, pool_bytes - reserved_bytes)
    uncommitted_inodes = max(0, pool_inodes - reserved_inodes)
    return {
        "pool_bytes": pool_bytes,
        "backing_pool_bytes": backing_bytes,
        "filesystem_usable_bytes": observed_pool_bytes,
        "allocatable_pool_bytes": pool_bytes,
        "pool_inodes": pool_inodes,
        "used_bytes": used_bytes,
        "used_inodes": used_inodes,
        "physical_used_bytes": used_bytes,
        "physical_used_inodes": used_inodes,
        "physical_free_bytes": free_bytes,
        "physical_free_inodes": free_inodes,
        "reserved_bytes": reserved_bytes,
        "reserved_inodes": reserved_inodes,
        "uncommitted_available_bytes": min(free_bytes, uncommitted_bytes),
        "uncommitted_available_inodes": min(free_inodes, uncommitted_inodes),
        "available_bytes": min(free_bytes, uncommitted_bytes),
        "available_inodes": min(free_inodes, uncommitted_inodes),
    }
