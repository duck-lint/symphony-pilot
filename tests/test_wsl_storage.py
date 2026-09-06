from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "scripts"))

import storage_cli
import task
import wsl_storage
import deployment_contract


class WslNativeStorageTransportTests(unittest.TestCase):
    def _fixture(self, directory: pathlib.Path, body: str) -> pathlib.Path:
        path = directory / "supervisor-fixture.py"
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def _run_fixture(self, fixture: pathlib.Path, callback, *, timeout: float = 1):
        with mock.patch.object(wsl_storage.os, "name", "posix"), \
             mock.patch.object(wsl_storage, "PYTHON", pathlib.Path(sys.executable)), \
             mock.patch.object(wsl_storage, "SUPERVISOR", fixture):
            return callback(timeout)

    def test_subprocess_harness_uses_fixed_supervisor_argv_and_sterile_environment(self):
        self.assertIn("scripts/wsl_storage.py", deployment_contract.CONTRACT_FILES)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            argv_record = root / "argv.json"
            env_record = root / "env.json"
            fixture = self._fixture(root, f"""
                import json
                import os
                import sys
                from pathlib import Path
                Path({str(argv_record)!r}).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
                Path({str(env_record)!r}).write_text(json.dumps(dict(os.environ)), encoding="utf-8")
                print(json.dumps({{"schema": "symphony-pilot-quota-inspection/v1"}}))
            """)
            result = self._run_fixture(
                fixture,
                lambda timeout: wsl_storage.inspect_quota(
                    "symphony-pilot", request_id="transport-test", timeout_seconds=timeout,
                ),
            )
            self.assertEqual(result["schema"], "symphony-pilot-quota-inspection/v1")
            self.assertEqual(json.loads(argv_record.read_text(encoding="utf-8")), [
                "--control", "quota-inspect-root", "--project", "symphony-pilot",
            ])
            self.assertEqual(json.loads(env_record.read_text(encoding="utf-8")), {
                "HOME": "/home/duck-lint", "USER": "duck-lint", "LOGNAME": "duck-lint",
                "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            })

    def test_transport_source_change_invalidates_contract_digest(self):
        transport_path = (ROOT / "scripts/wsl_storage.py").resolve()
        original_read_bytes = pathlib.Path.read_bytes

        def changed_bytes(path):
            data = original_read_bytes(path)
            return data + b"\n# synthetic transport change\n" if path.resolve() == transport_path else data

        before = deployment_contract.contract_digest(ROOT)
        with mock.patch.object(pathlib.Path, "read_bytes", changed_bytes):
            after = deployment_contract.contract_digest(ROOT)
        self.assertNotEqual(before, after)

    def test_admission_and_release_validate_exact_wire_shapes_and_fixed_controls(self):
        admission = {
            "schema": "symphony-pilot-task-quota-admission/v1",
            "project": "symphony-pilot", "pool": {}, "task_quota": {},
        }
        release = {
            "schema": "symphony-pilot-task-quota-release/v1",
            "project": "symphony-pilot", "identifier": "T-000001",
            "workspace_path": "/home/duck-lint/symphony-workspaces/symphony-pilot/T-000001",
            "project_id": 1_000_001, "workspace_state": "destroyed",
            "quota_state": "removed", "growth_possible": False,
            "remaining_bytes": 0, "remaining_inodes": 0,
        }
        results = iter((
            wsl_storage._ProcessResult(0, json.dumps(admission), ""),
            wsl_storage._ProcessResult(0, json.dumps(release), ""),
        ))
        with mock.patch.object(wsl_storage, "_bounded_process", side_effect=lambda *args: next(results)) as run:
            self.assertEqual(
                wsl_storage.admit_task_quota(
                    "symphony-pilot", "T-000001", byte_limit=8, inode_limit=250_000,
                    request_id="admission-test",
                ), admission,
            )
            self.assertEqual(
                wsl_storage.release_task_quota(
                    "symphony-pilot", "T-000001", request_id="release-test",
                ), release,
            )
        self.assertEqual(run.call_args_list[0].args[0], (
            "--control", "quota-admit-task", "--project", "symphony-pilot",
            "--identifier", "T-000001", "--byte-limit", "8", "--inode-limit", "250000",
        ))
        self.assertEqual(run.call_args_list[1].args[0], (
            "--control", "quota-release-task", "--project", "symphony-pilot",
            "--identifier", "T-000001",
        ))

    def test_fixed_public_controls_and_posix_storage_cli_do_not_use_windows_adapter(self):
        self.assertNotIn("wsl_adapter", (ROOT / "scripts/storage_cli.py").read_text(encoding="utf-8"))
        self.assertNotIn("wsl_adapter", (ROOT / "scripts/task.py").read_text(encoding="utf-8"))
        self.assertFalse(hasattr(wsl_storage, "execute"))
        with mock.patch.dict(sys.modules, {"wsl_adapter": None}):
            profile = SimpleNamespace(
                slug="symphony-canary",
                workspace_root=pathlib.PurePosixPath("/home/duck-lint/symphony-workspaces/symphony-canary"),
                storage_policy=SimpleNamespace(allocatable_pool_bytes=63, pool_bytes=64),
            )
            database = mock.MagicMock()
            database.__enter__.return_value = database
            database.record_storage_domain.return_value = {"domain": "verified"}
            database.storage_reservation_totals.return_value = {"reserved_bytes": 0, "reserved_inodes": 0}
            with mock.patch.object(storage_cli, "resolve_project", return_value=profile), \
                 mock.patch.object(storage_cli, "default_database_path", return_value=pathlib.Path("/tmp/control.sqlite3")), \
                 mock.patch.object(storage_cli.ControlPlaneDatabase, "open", return_value=database), \
                 mock.patch.object(storage_cli.wsl_storage, "inspect_quota", return_value={"schema": "ok"}) as inspect, \
                 mock.patch.object(storage_cli, "verify_storage_evidence", return_value={"verified": True}), \
                 mock.patch.object(storage_cli, "capacity_snapshot", return_value={"capacity": "ok"}):
                self.assertEqual(storage_cli.verify(SimpleNamespace(project="symphony-canary")), 0)
            inspect.assert_called_once_with("symphony-canary", request_id="storage-symphony-canary-verify")

    def test_task_pool_and_admission_routes_use_wsl_native_transport(self):
        profile = SimpleNamespace(
            slug="symphony-canary",
            workspace_root=pathlib.PurePosixPath("/home/duck-lint/symphony-workspaces/symphony-canary"),
            storage_policy=SimpleNamespace(task_bytes=8, task_inodes=250_000),
        )
        database = mock.Mock()
        database.read_storage_reservation.return_value = {
            "status": "reserved", "project_slug": "symphony-canary",
        }
        with mock.patch.object(task.wsl_storage, "inspect_quota", return_value={"schema": "pool"}) as inspect, \
             mock.patch.object(task, "verify_storage_evidence", return_value="domain"):
            self.assertEqual(task.verify_profile_storage_pool(profile), "domain")
        inspect.assert_called_once_with(
            "symphony-canary", request_id="storage-symphony-canary-pool-admission",
        )

        admission = {
            "schema": "symphony-pilot-task-quota-admission/v1",
            "project": "symphony-canary", "pool": {}, "task_quota": {},
        }
        with mock.patch.object(task.wsl_storage, "admit_task_quota", return_value=admission) as admit, \
             mock.patch.object(task, "verify_storage_evidence", return_value="domain"), \
             mock.patch.object(task, "task_quota_binding_from_evidence", return_value="binding"):
            result = task.verify_profile_storage(
                profile, "T-000001", database=database, task_id="11111111-1111-1111-1111-111111111111",
            )
        self.assertEqual(result.domain, "domain")
        self.assertEqual(result.binding, "binding")
        admit.assert_called_once_with(
            "symphony-canary", "T-000001", byte_limit=8, inode_limit=250_000,
            request_id="storage-symphony-canary-T-000001-admission",
        )

    def test_nonzero_child_exit_malformed_json_schema_and_bounds_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            cases = (
                ("failure", "import sys; sys.stderr.write('failed\\n'); sys.exit(7)", "quota_inspection"),
                ("malformed", "print('not-json')", "quota_inspection"),
                ("schema", "print('{\"schema\":\"wrong\"}')", "quota_inspection"),
            )
            for name, body, kind in cases:
                with self.subTest(name=name):
                    fixture = self._fixture(root, body)
                    with self.assertRaises(wsl_storage.WslStorageError) as raised:
                        self._run_fixture(
                            fixture,
                            lambda timeout: wsl_storage.inspect_quota("symphony-pilot", timeout_seconds=timeout),
                        )
                    self.assertEqual(raised.exception.kind, kind)

            timeout_fixture = self._fixture(root, "import time; time.sleep(2)")
            with self.assertRaisesRegex(wsl_storage.WslStorageError, "did not complete|failed"):
                self._run_fixture(
                    timeout_fixture,
                    lambda timeout: wsl_storage.inspect_quota("symphony-pilot", timeout_seconds=0.05),
                )

            output_fixture = self._fixture(root, "print('x' * 100)")
            with mock.patch.object(wsl_storage, "MAX_OUTPUT_BYTES", 32):
                with self.assertRaisesRegex(wsl_storage.WslStorageError, "output exceeded"):
                    self._run_fixture(
                        output_fixture,
                        lambda timeout: wsl_storage.inspect_quota("symphony-pilot", timeout_seconds=timeout),
                    )

    def test_public_controls_reject_unapproved_identity_and_limits_before_child(self):
        with mock.patch.object(wsl_storage, "_bounded_process") as process:
            with self.assertRaises(wsl_storage.WslStorageError):
                wsl_storage.inspect_quota("arbitrary")
            with self.assertRaises(wsl_storage.WslStorageError):
                wsl_storage.admit_task_quota("symphony-pilot", "T-1", byte_limit=1, inode_limit=1)
            with self.assertRaises(wsl_storage.WslStorageError):
                wsl_storage.admit_task_quota("symphony-pilot", "T-000001", byte_limit=0, inode_limit=1)
            with self.assertRaises(wsl_storage.WslStorageError):
                wsl_storage.release_task_quota("symphony-pilot", "T-000001/other")
            process.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
