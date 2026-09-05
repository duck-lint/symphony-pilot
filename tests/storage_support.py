"""Synthetic host proof used by lifecycle tests that bypass Linux admission."""
from __future__ import annotations

import json

from storage import GIB, StorageAdmissionProof, StoragePolicy, TaskQuotaBinding, VerifiedStorageDomain, derive_quota_id


def admission_proof(domain: VerifiedStorageDomain, *, identifier: str) -> StorageAdmissionProof:
    project = domain.project
    quota_id = derive_quota_id(identifier)
    binding = TaskQuotaBinding(
        project=project, identifier=identifier,
        workspace_path=f"/home/duck-lint/symphony-workspaces/{project}/{identifier}",
        quota_id=quota_id, byte_limit=8 * GIB, inode_limit=250_000,
        proof_json=json.dumps({
            "schema": "symphony-pilot-task-quota-proof/v1",
            "identifier": identifier,
            "workspace_path": f"/home/duck-lint/symphony-workspaces/{project}/{identifier}",
            "project_id": quota_id, "workspace_project_id": quota_id,
            "byte_hard_limit": 8 * GIB, "inode_hard_limit": 250_000,
            "usage": {"bytes": 0, "inodes": 1},
            "byte_probe": {"attempted": True, "result": "EDQUOT"},
            "inode_probe": {"attempted": True, "result": "EDQUOT"},
        }),
    )
    return StorageAdmissionProof(domain=domain, binding=binding)


def queue_task(database, task: dict[str, object]) -> dict[str, object]:
    """Seed a verified shared-pool proof before exercising lifecycle code.

    These tests intentionally do not exercise the Linux adapter. The
    production queue command obtains the same shape only from the fixed,
    deployed host capability and cannot call this helper.
    """
    project = str(task["project_slug"])
    domain = VerifiedStorageDomain(
        project=project,
        source="/dev/test-symphony-pool",
        target="/home/duck-lint/symphony-workspaces",
        fstype="ext4",
        options="rw,relatime,prjquota",
        pool_bytes=64 * GIB,
        pool_inodes=100_000_000,
        free_bytes=60 * GIB,
        free_inodes=99_000_000,
        evidence_json='{"synthetic_test_proof":true}',
    )
    database.reserve_storage_capacity(
        task["id"], project_slug=project, domain=domain, policy=StoragePolicy(),
    )
    return database.queue_task_with_storage(
        task["id"], project_slug=project, domain=domain, policy=StoragePolicy(),
        assignment=admission_proof(domain, identifier=str(task["identifier"])).binding,
    )
