from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

import control_db
from tests.storage_support import admission_proof
from storage import (GIB, StorageContractError, StoragePolicy,
                     StorageReleaseProof, VerifiedStorageDomain, capacity_snapshot,
                     TASK_QUOTA_ADMISSION_SCHEMA, derive_quota_id,
                     task_quota_binding_from_evidence, verify_storage_evidence)


class StorageContractTests(unittest.TestCase):
    def policy(self) -> StoragePolicy:
        return StoragePolicy(
            pool_bytes=64 * GIB, task_bytes=8 * GIB, task_inodes=250_000,
            emergency_reserve_bytes=8 * GIB, emergency_reserve_inodes=250_000,
        )

    def evidence(self, *, target: str = "/home/duck-lint/symphony-workspaces") -> dict:
        return {
            "schema": "symphony-pilot-quota-inspection/v1",
            "project": "demo",
            "scope": "persistent_symphony_workspace_pool",
            "filesystem": {
                "target": target, "source": "/dev/vdb", "fstype": "ext4",
                "options": "rw,relatime,prjquota",
                "statvfs": {
                    "block_size": 4096, "blocks": 16_777_216,
                    "free_blocks": 15_000_000, "inodes": 10_000_000,
                    "available_blocks": 14_900_000,
                    "free_inodes": 9_900_000, "available_inodes": 9_800_000,
                },
            },
            "quota": {
                "backend": "ext4-project-quota", "mount_support": True,
            },
            "ownership": {"trusted": True},
        }

    def domain(self) -> VerifiedStorageDomain:
        return verify_storage_evidence(
            "demo", self.evidence(), self.policy(),
            expected_target="/home/duck-lint/symphony-workspaces",
        )

    def queue_with_reservation(self, database, task, domain=None):
        domain = domain or self.domain()
        database.reserve_storage_capacity(
            task["id"], project_slug=str(task["project_slug"]),
            domain=domain, policy=self.policy(),
        )
        return database.queue_task_with_storage(
            task["id"], project_slug=str(task["project_slug"]), domain=domain,
            policy=self.policy(),
            assignment=admission_proof(domain, identifier=str(task["identifier"])).binding,
        )

    def test_unqualified_current_root_is_rejected(self):
        evidence = self.evidence(target="/")
        evidence["filesystem"]["source"] = "/dev/sdd"
        evidence["filesystem"]["options"] = "rw,relatime"
        evidence["quota"] = None
        with self.assertRaises(StorageContractError):
            verify_storage_evidence("demo", evidence, self.policy(), expected_target="/")

    def test_task_enforcement_proof_is_separate_from_pool_proof(self):
        evidence = self.evidence()
        self.assertIsNotNone(verify_storage_evidence(
            "demo", evidence, self.policy(),
            expected_target="/home/duck-lint/symphony-workspaces",
        ))
        task_evidence = {
            "schema": TASK_QUOTA_ADMISSION_SCHEMA,
            "project": "demo",
            "task_quota": json.loads(admission_proof(self.domain(), identifier="T-000001").binding.proof_json),
        }
        task_evidence["task_quota"]["inode_probe"]["result"] = "EIO"
        with self.assertRaisesRegex(StorageContractError, "hard-limit"):
            task_quota_binding_from_evidence(
                task_evidence, project="demo", identifier="T-000001", policy=self.policy(),
            )

    def test_capacity_snapshot_exposes_usage_and_reservations(self):
        values = capacity_snapshot(
            {"pool_bytes": 100, "pool_inodes": 1000, "free_bytes": 40, "free_inodes": 400},
            20, 100,
            configured_backing_bytes=120,
        )
        self.assertEqual(values["used_bytes"], 60)
        self.assertEqual(values["available_bytes"], 40)
        self.assertEqual(values["used_inodes"], 600)
        self.assertEqual(values["available_inodes"], 400)
        self.assertEqual(values["backing_pool_bytes"], 120)

    def test_legacy_queue_transition_cannot_bypass_storage_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            with control_db.open_database(pathlib.Path(directory) / "control.sqlite3") as database:
                task = database.create_task(
                    project_slug="demo", title="Storage", objective="Bounded storage",
                    base_ref="main", base_sha="a" * 40,
                )
                with self.assertRaisesRegex(control_db.StateConflict, "storage reservation"):
                    database.queue_task(task["id"], project_slug="demo")
                self.assertEqual(database.read_task(task["id"])["state"], "PREPARED")

    def test_queue_finalization_requires_preexisting_durable_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            with control_db.open_database(pathlib.Path(directory) / "control.sqlite3") as database:
                task = database.create_task(
                    project_slug="demo", title="Storage", objective="Two phase admission",
                    base_ref="main", base_sha="a" * 40, identifier="T-000001",
                )
                with self.assertRaisesRegex(control_db.StateConflict, "durable storage reservation"):
                    database.queue_task_with_storage(
                        task["id"], project_slug="demo", domain=self.domain(), policy=self.policy(),
                        assignment=admission_proof(self.domain(), identifier="T-000001").binding,
                    )
                self.assertIsNone(database.read_storage_reservation(task["id"]))

    def test_reservation_survives_failed_quota_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            with control_db.open_database(pathlib.Path(directory) / "control.sqlite3") as database:
                task = database.create_task(
                    project_slug="demo", title="Storage", objective="Crash recovery",
                    base_ref="main", base_sha="a" * 40, identifier="T-000001",
                )
                database.reserve_storage_capacity(
                    task["id"], project_slug="demo", domain=self.domain(), policy=self.policy(),
                )
                bad = dataclasses.replace(
                    admission_proof(self.domain(), identifier="T-000001").binding,
                    quota_id=1,
                )
                with self.assertRaises(StorageContractError):
                    database.queue_task_with_storage(
                        task["id"], project_slug="demo", domain=self.domain(), policy=self.policy(),
                        assignment=bad,
                    )
                self.assertEqual(database.read_task(task["id"])["state"], "PREPARED")
                self.assertEqual(database.read_storage_reservation(task["id"])["status"], "reserved")

    def test_reservation_retry_is_idempotent_but_identity_conflicts_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            with control_db.open_database(pathlib.Path(directory) / "control.sqlite3") as database:
                task = database.create_task(
                    project_slug="demo", title="Storage", objective="Retry",
                    base_ref="main", base_sha="a" * 40, identifier="T-000001",
                )
                first = database.reserve_storage_capacity(
                    task["id"], project_slug="demo", domain=self.domain(), policy=self.policy(),
                )
                second = database.reserve_storage_capacity(
                    task["id"], project_slug="demo", domain=self.domain(), policy=self.policy(),
                )
                self.assertEqual(first["task_id"], second["task_id"])
                conflict = dataclasses.replace(self.policy(), task_bytes=4 * GIB)
                with self.assertRaisesRegex(control_db.StateConflict, "conflicting"):
                    database.reserve_storage_capacity(
                        task["id"], project_slug="demo", domain=self.domain(), policy=conflict,
                    )

    def test_task_quota_binding_is_separate_and_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            with control_db.open_database(pathlib.Path(directory) / "control.sqlite3") as database:
                task = database.create_task(
                    project_slug="demo", title="Storage", objective="Bounded storage",
                    base_ref="main", base_sha="a" * 40, identifier="T-000001",
                )
                domain = self.domain()
                binding = dataclasses.replace(
                    admission_proof(domain, identifier="T-000001").binding,
                    quota_id=1,
                )
                with self.assertRaisesRegex(StorageContractError, "identity"):
                    database.queue_task_with_storage(
                        task["id"], project_slug="demo", domain=domain,
                        policy=self.policy(), assignment=binding,
                    )
                self.assertEqual(database.read_task(task["id"])["state"], "PREPARED")

    def test_reservations_are_accounted_against_one_shared_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            with control_db.open_database(pathlib.Path(directory) / "control.sqlite3") as database:
                for project, identifier in (("demo", "T-000001"), ("other", "T-000002")):
                    task = database.create_task(
                        project_slug=project, title="Storage", objective="Shared pool",
                        base_ref="main", base_sha="a" * 40, identifier=identifier,
                    )
                    domain = dataclasses.replace(self.domain(), project=project)
                    self.queue_with_reservation(database, task, domain)
                totals = database.storage_reservation_totals("demo")
                self.assertEqual(totals["reserved_bytes"], 16 * GIB)
                self.assertEqual(totals["project_reserved_bytes"], 8 * GIB)

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
                queued = self.queue_with_reservation(database, task)
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
                            self.queue_with_reservation(database, task)
                    else:
                        self.queue_with_reservation(database, task)

    def test_reservation_release_requires_trusted_cleanup_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            with control_db.open_database(pathlib.Path(directory) / "control.sqlite3") as database:
                task = database.create_task(
                    project_slug="demo", title="Storage", objective="Cleanup proof",
                    base_ref="main", base_sha="a" * 40, identifier="T-000001",
                )
                self.queue_with_reservation(database, task)
                with self.assertRaisesRegex(StorageContractError, "cleanup proof"):
                    database.release_storage_reservation(task["id"], proof=None)
                evidence = {
                    "schema": "symphony-pilot-task-quota-release/v1",
                    "project_id": derive_quota_id("T-000001"),
                    "workspace_path": "/home/duck-lint/symphony-workspaces/demo/T-000001",
                    "workspace_state": "destroyed", "quota_state": "removed",
                    "growth_possible": False, "remaining_bytes": 0, "remaining_inodes": 0,
                }
                proof = StorageReleaseProof(
                    project="demo", identifier="T-000001",
                    workspace_path=evidence["workspace_path"],
                    quota_id=evidence["project_id"], proof_json=json.dumps(evidence),
                )
                released = database.release_storage_reservation(task["id"], proof=proof)
                self.assertEqual(released["status"], "released")

    def test_released_reservation_does_not_hide_retained_physical_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            with control_db.open_database(pathlib.Path(directory) / "control.sqlite3") as database:
                constrained = dataclasses.replace(self.domain(), free_bytes=16 * GIB)
                first = database.create_task(
                    project_slug="demo", title="Storage", objective="Retained usage",
                    base_ref="main", base_sha="a" * 40, identifier="T-000001",
                )
                self.queue_with_reservation(database, first, constrained)
                evidence = {
                    "schema": "symphony-pilot-task-quota-release/v1",
                    "project_id": derive_quota_id("T-000001"),
                    "workspace_path": "/home/duck-lint/symphony-workspaces/demo/T-000001",
                    "workspace_state": "destroyed", "quota_state": "removed",
                    "growth_possible": False, "remaining_bytes": 0, "remaining_inodes": 0,
                }
                proof = StorageReleaseProof(
                    project="demo", identifier="T-000001",
                    workspace_path=evidence["workspace_path"],
                    quota_id=evidence["project_id"], proof_json=json.dumps(evidence),
                )
                database.release_storage_reservation(first["id"], proof=proof)
                second = database.create_task(
                    project_slug="demo", title="Storage", objective="Retained usage",
                    base_ref="main", base_sha="a" * 40, identifier="T-000002",
                )
                tighter = dataclasses.replace(constrained, free_bytes=15 * GIB)
                with self.assertRaisesRegex(control_db.StateConflict, "emergency reserve"):
                    self.queue_with_reservation(database, second, tighter)
    def test_verified_domain_requires_exact_shared_pool_mount_and_identity_proof(self):
        evidence = self.evidence()
        evidence["filesystem"]["target"] = "/home/duck-lint/symphony-workspaces/other"
        with self.assertRaises(StorageContractError):
            verify_storage_evidence(
                "demo", evidence, self.policy(),
                expected_target="/home/duck-lint/symphony-workspaces",
            )
        evidence = self.evidence()
        evidence["quota"]["mount_support"] = False
        with self.assertRaises(StorageContractError):
            verify_storage_evidence(
                "demo", evidence, self.policy(),
                expected_target="/home/duck-lint/symphony-workspaces",
            )

    def test_usable_capacity_may_be_below_nominal_backing(self):
        evidence = self.evidence()
        evidence["filesystem"]["statvfs"]["blocks"] = 16_600_000
        evidence["filesystem"]["statvfs"]["free_blocks"] = 15_500_000
        domain = verify_storage_evidence(
            "demo", evidence, self.policy(),
            expected_target="/home/duck-lint/symphony-workspaces",
        )
        self.assertLess(domain.pool_bytes, self.policy().pool_bytes)
        self.assertGreaterEqual(domain.pool_bytes, self.policy().allocatable_pool_bytes)

    def test_task_usable_capacity_uses_f_bavail_not_f_bfree(self):
        evidence = self.evidence()
        evidence["filesystem"]["statvfs"]["free_blocks"] = 15_000_000
        evidence["filesystem"]["statvfs"]["available_blocks"] = 14_000_000
        domain = self.domain_from(evidence)
        self.assertEqual(domain.free_bytes, 4096 * 14_000_000)
        self.assertGreater(
            int(json.loads(domain.evidence_json)["filesystem"]["statvfs"]["free_blocks"]),
            int(json.loads(domain.evidence_json)["filesystem"]["statvfs"]["available_blocks"]),
        )

    def test_f_bfree_above_f_bavail_can_preserve_emergency_reserve(self):
        evidence = self.evidence()
        evidence["filesystem"]["statvfs"]["free_blocks"] = 16_500_000
        evidence["filesystem"]["statvfs"]["available_blocks"] = 3_900_000
        domain = self.domain_from(evidence)
        with tempfile.TemporaryDirectory() as directory:
            with control_db.open_database(pathlib.Path(directory) / "control.sqlite3") as database:
                task = database.create_task(
                    project_slug="demo", title="Storage", objective="Task capacity",
                    base_ref="main", base_sha="a" * 40, identifier="T-000001",
                )
                with self.assertRaisesRegex(control_db.StateConflict, "emergency reserve"):
                    # Physical f_bfree must not authorize an unprivileged
                    # admission when f_bavail cannot preserve the reserve.
                    database.reserve_storage_capacity(
                        task["id"], project_slug="demo", domain=domain, policy=self.policy(),
                    )

    def domain_from(self, evidence):
        return verify_storage_evidence(
            "demo", evidence, self.policy(),
            expected_target="/home/duck-lint/symphony-workspaces",
        )

    def test_usable_capacity_below_allocatable_policy_is_rejected(self):
        evidence = self.evidence()
        evidence["filesystem"]["statvfs"]["blocks"] = 16_000_000
        with self.assertRaisesRegex(StorageContractError, "below allocatable"):
            verify_storage_evidence(
                "demo", evidence, self.policy(),
                expected_target="/home/duck-lint/symphony-workspaces",
            )


if __name__ == "__main__":
    unittest.main()
