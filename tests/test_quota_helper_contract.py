from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

import wsl_contained_exec


class QuotaHelperContractTests(unittest.TestCase):
    def test_source_identity_is_pinned_by_supervisor(self):
        source = ROOT / "provisioning" / "quota-admit-task.c"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).hexdigest(),
            wsl_contained_exec.QUOTA_HELPER_SOURCE_SHA256,
        )

    def test_provisioning_recipe_is_fixed_and_not_a_command_broker(self):
        recipe = (ROOT / "scripts" / "provision_storage_domain.sh").read_text()
        self.assertIn("mkfs.ext4 -O project", recipe)
        self.assertIn("mount -o prjquota", recipe)
        self.assertIn("64 * 1024 * 1024 * 1024", recipe)
        self.assertIn("/home/duck-lint/symphony-workspaces", recipe)
        self.assertIn("/dev/sdd", recipe)
        helper = (ROOT / "provisioning" / "quota-admit-task.c").read_text()
        self.assertNotIn("system(", helper)
        self.assertNotIn("popen(", helper)
        self.assertNotIn("execvp(", helper)
        self.assertIn("FS_IOC_FSSETXATTR", helper)
        self.assertIn("Q_XSETQLIM", helper)

    def test_cleanup_capability_accepts_only_exact_zero_growth_proof(self):
        evidence = {
            "schema": "symphony-pilot-task-quota-release/v1",
            "project": "symphony-pilot", "identifier": "T-000001",
            "workspace_path": "/home/duck-lint/symphony-workspaces/symphony-pilot/T-000001",
            "project_id": 1_000_001, "workspace_state": "destroyed",
            "quota_state": "removed", "growth_possible": False,
            "remaining_bytes": 0, "remaining_inodes": 0,
        }
        result = mock.Mock(returncode=0, stdout=json.dumps(evidence))
        with mock.patch.object(wsl_contained_exec, "_quota_helper_fd", return_value=7), \
             mock.patch.object(wsl_contained_exec.os, "close"), \
             mock.patch.object(wsl_contained_exec.subprocess, "run", return_value=result):
            self.assertEqual(
                wsl_contained_exec._quota_task_release("symphony-pilot", "T-000001"),
                evidence,
            )


if __name__ == "__main__":
    unittest.main()
