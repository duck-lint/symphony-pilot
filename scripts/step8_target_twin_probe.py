#!/usr/bin/env python3
"""Run the bounded Step-8 storage acceptance probe on the disposable twin.

This harness is intentionally fixed to the registered canary profile and the
deployed supervisor.  It does not accept a caller-selected project, path,
quota ID, limit, device, or command.  The runner is disposable, so the probe
may create one PREPARED task and one full-task reservation, then proves the
same trusted reclamation/release chain before returning.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys


PROJECT = "symphony-canary"
DEPLOYMENT_ROOT = pathlib.Path(
    "/home/duck-lint/.local/share/symphony-pilot/deployments/symphony-canary"
)
SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Import the exact deployed control implementation, not an independently
# edited source copy.  The deployment manifest is verified by each control
# entry point before it uses the setuid helper.
sys.path.insert(0, str(DEPLOYMENT_ROOT / "runtime"))
from control_db import ControlPlaneDatabase, default_database_path  # noqa: E402
from prepare_workspace import load_profile  # noqa: E402
from storage import (  # noqa: E402
    storage_release_proof_from_evidence,
    task_quota_binding_from_evidence,
    verify_storage_evidence,
)
from workspace_boundary import (  # noqa: E402
    create_empty_task_workspace,
    reclaim_task_workspace,
)
import wsl_contained_exec as deployed_supervisor  # noqa: E402


def _git_head() -> str:
    return subprocess.run(
        ["git", "-C", str(SOURCE_ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def main() -> int:
    profile = load_profile(DEPLOYMENT_ROOT / "projects" / PROJECT / "profile.toml")
    database_path = default_database_path()
    task: dict[str, object] | None = None
    task_root: pathlib.Path | None = None
    admitted = False
    released = False

    with ControlPlaneDatabase.open(database_path) as database:
        # A disposable runner must be reset between runs.  Refusing stale
        # control state is safer than silently reusing an old T-N or quota.
        if database.list_tasks(project_slug=PROJECT):
            raise RuntimeError("target-twin control database contains stale canary tasks")
        if database.read_storage_pool() is not None:
            raise RuntimeError("target-twin control database contains stale storage identity")

        identifier = None
        try:
            pool_evidence = deployed_supervisor._quota_inspection(PROJECT)
            domain = verify_storage_evidence(
                PROJECT,
                pool_evidence,
                profile.storage_policy,
                expected_target=str(profile.workspace_root.parent),
            )
            task = database.create_task(
                project_slug=PROJECT,
                title="Step-8 disposable storage probe",
                objective="Prove the reviewed quota and reclamation chain",
                base_ref="main",
                base_sha=_git_head(),
                state="PREPARED",
            )
            identifier = str(task["identifier"])
            reservation = database.reserve_storage_capacity(
                task["id"], project_slug=PROJECT, domain=domain,
                policy=profile.storage_policy,
            )

            task_root = create_empty_task_workspace(profile.workspace_root, identifier)
            # The fixed quota-admit-task control returns and the validator
            # requires the project ID, FS_XFLAG_PROJINHERIT descendant proof,
            # and both byte/inode EDQUOT results. No synthetic evidence is
            # accepted here.
            admission = deployed_supervisor._quota_task_admission(
                PROJECT,
                identifier,
                profile.storage_policy.task_bytes,
                profile.storage_policy.task_inodes,
            )
            binding = task_quota_binding_from_evidence(
                admission,
                project=PROJECT,
                identifier=identifier,
                policy=profile.storage_policy,
            )
            admitted = True

            # Leave a real unprivileged file behind so reclamation proves
            # removal of a non-empty exact task tree.
            (task_root / "target-twin-proof.txt").write_text(
                "disposable Step-8 proof\n", encoding="ascii",
            )
            reclaimed = reclaim_task_workspace(
                pathlib.Path(profile.workspace_root).parent, PROJECT, identifier,
            )
            if reclaimed != task_root or task_root.exists():
                raise RuntimeError("target-twin workspace reclamation did not prove absence")

            # The fixed quota-release-task control is called only after the
            # exact unprivileged reclamation primitive proves path absence.
            release_evidence = deployed_supervisor._quota_task_release(PROJECT, identifier)
            release_proof = storage_release_proof_from_evidence(
                release_evidence, project=PROJECT, identifier=identifier,
            )
            reservation_after = database.release_storage_reservation(
                task["id"], proof=release_proof,
            )
            released = reservation_after["status"] == "released"
            if not released:
                raise RuntimeError("target-twin storage reservation was not released")

            print(json.dumps({
                "schema": "symphony-pilot-target-twin-storage-probe/v1",
                "git_sha": _git_head(),
                "project": PROJECT,
                "identifier": identifier,
                "reservation_before": reservation,
                "task_quota": admission["task_quota"],
                "validated_binding": {
                    "project_id": binding.quota_id,
                    "workspace_path": binding.workspace_path,
                    "byte_limit": binding.byte_limit,
                    "inode_limit": binding.inode_limit,
                },
                "workspace_reclaimed": True,
                "workspace_absent": not task_root.exists(),
                "release": release_evidence,
                "reservation_after": reservation_after,
            }, sort_keys=True))
        finally:
            # Failure cleanup never deletes a task tree without the same
            # descriptor-safe primitive, and never releases a reservation
            # until the fixed helper proves zero usage and removed limits.
            cleanup_errors: list[str] = []
            if task_root is not None and os.path.lexists(task_root):
                try:
                    reclaim_task_workspace(
                        pathlib.Path(profile.workspace_root).parent, PROJECT, identifier,
                    )
                except Exception as exc:  # pragma: no cover - target-only failure path
                    cleanup_errors.append(f"workspace cleanup: {type(exc).__name__}")
            if admitted and not released and identifier is not None and task is not None:
                if task_root is not None and os.path.lexists(task_root):
                    cleanup_errors.append("quota cleanup was withheld because workspace remains")
                else:
                    try:
                        cleanup_evidence = deployed_supervisor._quota_task_release(PROJECT, identifier)
                        cleanup_proof = storage_release_proof_from_evidence(
                            cleanup_evidence, project=PROJECT, identifier=identifier,
                        )
                        database.release_storage_reservation(task["id"], proof=cleanup_proof)
                    except Exception as exc:  # pragma: no cover - target-only failure path
                        cleanup_errors.append(f"quota cleanup: {type(exc).__name__}")
            if cleanup_errors:
                raise RuntimeError("target-twin probe cleanup failed: " + "; ".join(cleanup_errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
