from __future__ import annotations

import argparse
import pathlib
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "scripts"))

import control_db
import storage_cli
from storage import GIB, StoragePolicy, VerifiedStorageDomain
from tests.storage_support import queue_task


class StorageReleaseLifecycleTests(unittest.TestCase):
    def domain(self) -> VerifiedStorageDomain:
        return VerifiedStorageDomain(
            project="demo", source="/dev/test-symphony-pool",
            target="/home/duck-lint/symphony-workspaces", fstype="ext4",
            options="rw,relatime,prjquota", pool_bytes=64 * GIB,
            pool_inodes=100_000_000, free_bytes=60 * GIB,
            free_inodes=99_000_000, evidence_json='{"synthetic_test_proof":true}',
        )

    def policy(self) -> StoragePolicy:
        return StoragePolicy()

    def profile(self, pool: pathlib.Path):
        return types.SimpleNamespace(slug="demo", workspace_root=pool / "demo",
                                     storage_policy=self.policy())

    def task(self, database, *, state="PREPARED"):
        return database.create_task(
            project_slug="demo", title="Storage", objective="Release lifecycle",
            base_ref="main", base_sha="a" * 40, identifier="T-000001", state=state,
        )

    def reserve(self, database, task):
        return database.reserve_storage_capacity(
            task["id"], project_slug="demo", domain=self.domain(), policy=self.policy(),
        )

    def release_evidence(self):
        return {
            "schema": "symphony-pilot-task-quota-release/v1",
            "project": "demo", "identifier": "T-000001",
            "workspace_path": "/home/duck-lint/symphony-workspaces/demo/T-000001",
            "project_id": 1_000_001, "workspace_state": "destroyed",
            "quota_state": "removed", "growth_possible": False,
            "remaining_bytes": 0, "remaining_inodes": 0,
        }

    def call_release(self, database_path, profile, *, reclaim=storage_cli.reclaim_task_workspace, helper=None):
        import wsl_adapter
        args = argparse.Namespace(project="demo", task="T-000001")
        with mock.patch.object(storage_cli, "default_database_path", return_value=database_path), \
             mock.patch.object(storage_cli, "resolve_project", return_value=profile), \
             mock.patch.object(storage_cli, "reclaim_task_workspace", side_effect=reclaim) as reclaim_mock, \
             mock.patch.object(wsl_adapter, "release_task_quota", side_effect=helper) as helper_mock:
            result = storage_cli.release(args)
        return result, reclaim_mock, helper_mock

    def test_active_task_release_is_rejected_and_workspace_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            database_path = root / "control.sqlite3"
            with control_db.open_database(database_path) as database:
                task = self.task(database)
                queue_task(database, task)
            workspace = root / "pool" / "demo" / "T-000001"
            workspace.mkdir(parents=True)
            (workspace / "keep").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(control_db.StateConflict, "post-QUEUED"):
                self.call_release(database_path, self.profile(root / "pool"))
            self.assertTrue(workspace.exists())
            with control_db.ControlPlaneDatabase.open_readonly(database_path) as database:
                self.assertEqual(database.read_storage_reservation(task["id"])["status"], "reserved")

    def test_workspace_deletion_failure_never_calls_helper_or_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            database_path = root / "control.sqlite3"
            with control_db.open_database(database_path) as database:
                task = self.task(database)
                self.reserve(database, task)
            with self.assertRaises(control_db.ControlPlaneError):
                self.call_release(
                    database_path, self.profile(root / "pool"),
                    reclaim=storage_cli.WorkspaceBoundaryError("deletion failed"),
                    helper=lambda *args, **kwargs: self.release_evidence(),
                )
            with control_db.ControlPlaneDatabase.open_readonly(database_path) as database:
                self.assertEqual(database.read_storage_reservation(task["id"])["status"], "reserved")

    def test_helper_cleanup_failure_retains_reservation_after_workspace_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            database_path = root / "control.sqlite3"
            with control_db.open_database(database_path) as database:
                task = self.task(database)
                self.reserve(database, task)
            workspace = root / "pool" / "demo" / "T-000001"
            workspace.mkdir(parents=True)
            (workspace / "partial").write_text("partial", encoding="utf-8")
            import wsl_adapter
            with self.assertRaises(control_db.ControlPlaneError):
                self.call_release(
                    database_path, self.profile(root / "pool"),
                    helper=lambda *args, **kwargs: (_ for _ in ()).throw(
                        wsl_adapter.WslAdapterError("quota_cleanup", "failed")
                    ),
                )
            self.assertFalse(workspace.exists())
            with control_db.ControlPlaneDatabase.open_readonly(database_path) as database:
                self.assertEqual(database.read_storage_reservation(task["id"])["status"], "reserved")

    def test_prepared_recovery_deletes_then_proves_then_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            database_path = root / "control.sqlite3"
            with control_db.open_database(database_path) as database:
                task = self.task(database)
                self.reserve(database, task)
            workspace = root / "pool" / "demo" / "T-000001"
            workspace.mkdir(parents=True)
            (workspace / "partial").write_text("partial", encoding="utf-8")
            result, reclaim_mock, helper_mock = self.call_release(
                database_path, self.profile(root / "pool"),
                helper=lambda *args, **kwargs: self.release_evidence(),
            )
            self.assertEqual(result, 0)
            self.assertFalse(workspace.exists())
            reclaim_mock.assert_called_once()
            helper_mock.assert_called_once()
            with control_db.ControlPlaneDatabase.open_readonly(database_path) as database:
                self.assertEqual(database.read_storage_reservation(task["id"])["status"], "released")


if __name__ == "__main__":
    unittest.main(verbosity=2)
