from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

import control_db
from storage import (GIB, StorageContractError, StoragePolicy,
                     VerifiedStorageDomain, capacity_snapshot,
                     derive_quota_id, verify_storage_evidence)


class StorageContractTests(unittest.TestCase):
    def policy(self) -> StoragePolicy:
        return StoragePolicy(
            pool_bytes=64 * GIB, task_bytes=8 * GIB, task_inodes=250_000,
            emergency_reserve_bytes=8 * GIB, emergency_reserve_inodes=250_000,
        )

    def evidence(self, *, target: str = "/home/duck-lint/symphony-workspaces/demo") -> dict:
        return {
            "schema": "symphony-pilot-quota-inspection/v1",
            "project": "demo",
            "scope": "persistent_project_workspace_root",
            "filesystem": {
                "target": target, "source": "/dev/vdb", "fstype": "ext4",
                "options": "rw,relatime,prjquota",
                "statvfs": {
                    "block_size": 4096, "blocks": 16_777_216,
                    "free_blocks": 15_000_000, "inodes": 10_000_000,
                    "free_inodes": 9_900_000,
                },
            },
            "quota": {
                "backend": "ext4-project-quota", "identity_applicable": True,
                "byte_hard_limit_enforced": True, "inode_hard_limit_enforced": True,
            },
            "ownership": {"trusted": True},
        }

    def domain(self) -> VerifiedStorageDomain:
        return verify_storage_evidence(
            "demo", self.evidence(), self.policy(),
            expected_target="/home/duck-lint/symphony-workspaces/demo",
        )

    def test_unqualified_current_root_is_rejected(self):
        evidence = self.evidence(target="/")
        evidence["filesystem"]["source"] = "/dev/sdd"
        evidence["filesystem"]["options"] = "rw,relatime"
        evidence["quota"] = None
        with self.assertRaises(StorageContractError):
            verify_storage_evidence("demo", evidence, self.policy(), expected_target="/")

    def test_missing_enforcement_proof_is_rejected(self):
        evidence = self.evidence()
        evidence["quota"]["inode_hard_limit_enforced"] = False
        with self.assertRaisesRegex(StorageContractError, "inode hard-limit"):
            verify_storage_evidence(
                "demo", evidence, self.policy(),
                expected_target="/home/duck-lint/symphony-workspaces/demo",
            )

    def test_capacity_snapshot_exposes_usage_and_reservations(self):
        values = capacity_snapshot(
            {"pool_bytes": 100, "pool_inodes": 1000, "free_bytes": 40, "free_inodes": 400},
            20, 100,
        )
        self.assertEqual(values["used_bytes"], 60)
        self.assertEqual(values["available_bytes"], 20)
        self.assertEqual(values["used_inodes"], 600)
        self.assertEqual(values["available_inodes"], 300)

    def test_queue_reserves_full_allowance_and_preserves_host_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "control.sqlite3"
            with control_db.open_database(path) as database:
                task = database.create_task(
                    project_slug="demo", title="Storage", objective="Bounded storage",
                    base_ref="main", base_sha="a" * 40,
                    task_id="11111111-1111-1111-1111-111111111111",
                    identifier="T-000001",
                )
                queued = database.queue_task_with_storage(
                    task["id"], project_slug="demo", domain=self.domain(), policy=self.policy(),
                )
                reservation = database.read_storage_reservation(task["id"])
                self.assertEqual(queued["state"], "QUEUED")
                self.assertEqual(reservation["reserved_bytes"], 8 * GIB)
                self.assertEqual(reservation["reserved_inodes"], 250_000)
                self.assertEqual(reservation["quota_id"], derive_quota_id("T-000001"))
                self.assertEqual(database.read_storage_domain("demo")["source"], "/dev/vdb")

    def test_reservation_cannot_consume_emergency_reserve(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "control.sqlite3"
            with control_db.open_database(path) as database:
                for number in range(1, 8):
                    task = database.create_task(
                        project_slug="demo", title="Storage", objective="Bounded storage",
                        base_ref="main", base_sha="a" * 40,
                        identifier=f"T-{number:06d}",
                    )
                    if number == 7:
                        with self.assertRaises(control_db.StateConflict):
                            database.queue_task_with_storage(
                                task["id"], project_slug="demo", domain=self.domain(), policy=self.policy(),
                            )
                    else:
                        database.queue_task_with_storage(
                            task["id"], project_slug="demo", domain=self.domain(), policy=self.policy(),
                        )

    def test_verified_domain_requires_exact_project_mount_and_identity_proof(self):
        evidence = self.evidence()
        evidence["filesystem"]["target"] = "/home/duck-lint/symphony-workspaces/other"
        with self.assertRaises(StorageContractError):
            verify_storage_evidence(
                "demo", evidence, self.policy(),
                expected_target="/home/duck-lint/symphony-workspaces/demo",
            )
        evidence = self.evidence()
        evidence["quota"]["identity_applicable"] = False
        with self.assertRaises(StorageContractError):
            verify_storage_evidence(
                "demo", evidence, self.policy(),
                expected_target="/home/duck-lint/symphony-workspaces/demo",
            )


if __name__ == "__main__":
    unittest.main()
