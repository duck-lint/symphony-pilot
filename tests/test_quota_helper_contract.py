from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import types
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
        self.assertIn("mkfs.ext4 -m 0 -i 65536 -I 256 -J size=64", recipe)
        self.assertIn("-O project,quota -E quotatype=prjquota", recipe)
        self.assertIn("mount -t ext4 -o prjquota", recipe)
        self.assertIn('chown duck-lint:duck-lint "$POOL_ROOT"', recipe)
        self.assertIn('chmod 0750 "$POOL_ROOT"', recipe)
        self.assertIn("/etc/fstab", recipe)
        self.assertNotIn("/etc/fstab.d", recipe)
        self.assertIn("f_bavail", recipe)
        self.assertIn("f_favail", recipe)
        self.assertIn("quota-admit-task.c", recipe)
        self.assertIn("ACTUAL_SOURCE_SHA256", recipe)
        self.assertIn("EXPECTED_SOURCE_SHA256", recipe)
        self.assertIn("64 * 1024 * 1024 * 1024", recipe)
        self.assertIn("/home/duck-lint/symphony-workspaces", recipe)
        self.assertIn("/dev/sdd", recipe)
        helper = (ROOT / "provisioning" / "quota-admit-task.c").read_text()
        self.assertNotIn("system(", helper)
        self.assertNotIn("popen(", helper)
        self.assertNotIn("execvp(", helper)
        self.assertIn("FS_IOC_FSSETXATTR", helper)
        self.assertIn("FS_XFLAG_PROJINHERIT", helper)
        self.assertIn("Q_GETQUOTA", helper)
        self.assertIn("Q_SETQUOTA", helper)
        self.assertIn("struct dqblk", helper)
        self.assertIn("SYS_quotactl_fd", helper)
        self.assertNotIn("Q_XGETQUOTA", helper)
        self.assertNotIn("Q_XSETQLIM", helper)

    def test_task_helper_proves_project_inheritance(self):
        helper = (ROOT / "provisioning" / "quota-admit-task.c").read_text()
        self.assertIn("attrs.fsx_xflags |= FS_XFLAG_PROJINHERIT", helper)
        self.assertIn("attrs.fsx_projid != project_id", helper)
        self.assertIn("!(attrs.fsx_xflags & FS_XFLAG_PROJINHERIT)", helper)
        self.assertIn("inheritance_probe", helper)

    def test_provisioning_source_digest_is_verified_before_compile(self):
        recipe = (ROOT / "scripts" / "provision_storage_domain.sh").read_text()
        self.assertLess(recipe.index("ACTUAL_SOURCE_SHA256=$(sha256sum"), recipe.index("cc -std=c11"))
        self.assertIn('[ "$ACTUAL_SOURCE_SHA256" = "$EXPECTED_SOURCE_SHA256" ]', recipe)
        self.assertIn('stat -c \'%a\' "$HELPER"', recipe)
        self.assertIn('HELPER_UID=$(stat -c \'%u\' "$HELPER")', recipe)
        self.assertIn('HELPER_GID=$(stat -c \'%g\' "$HELPER")', recipe)

    def test_fstab_update_is_exact_and_preserves_unrelated_entries(self):
        recipe = (ROOT / "scripts" / "provision_storage_domain.sh").read_text()
        self.assertIn('NF >= 2 && $2 == root', recipe)
        self.assertIn('if ($0 != desired) conflict = 1', recipe)
        self.assertIn('if (found == 0) print desired', recipe)
        self.assertIn('if (conflict || found > 1) exit 42', recipe)

    def test_runtime_requires_actual_setuid_owner_and_group_state(self):
        binary = b"reviewed-helper"
        identity = {
            "schema": "symphony-pilot-quota-helper/v1",
            "source_sha256": wsl_contained_exec.QUOTA_HELPER_SOURCE_SHA256,
            "helper_sha256": hashlib.sha256(binary).hexdigest(),
            "group": wsl_contained_exec.QUOTA_HELPER_GROUP,
            "privilege": "setuid-root",
        }
        good_parent = types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        good_helper = types.SimpleNamespace(
            st_mode=stat.S_IFREG | stat.S_ISUID | 0o755, st_uid=0, st_gid=4242,
        )
        good_identity = types.SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0)
        fake_grp = types.SimpleNamespace(
            getgrnam=lambda name: types.SimpleNamespace(gr_gid=4242),
        )

        def run_case(helper=good_helper, parent=good_parent, document=identity):
            seen = [False]

            def helper_read(fd, size):
                if fd == 8:
                    return json.dumps(document).encode("ascii")
                if not seen[0]:
                    seen[0] = True
                    return binary
                return b""

            with mock.patch.dict(sys.modules, {"grp": fake_grp}), \
                 mock.patch.object(wsl_contained_exec.os, "O_CLOEXEC", 0, create=True), \
                 mock.patch.object(wsl_contained_exec.os, "stat", return_value=parent), \
                 mock.patch.object(wsl_contained_exec.os, "open", side_effect=[7, 8]), \
                 mock.patch.object(wsl_contained_exec.os, "fstat", side_effect=[helper, good_identity]), \
                 mock.patch.object(wsl_contained_exec.os, "read", side_effect=helper_read), \
                 mock.patch.object(wsl_contained_exec.os, "lseek"), \
                 mock.patch.object(wsl_contained_exec.os, "close"):
                return wsl_contained_exec._quota_helper_fd()

        self.assertEqual(run_case(), 7)
        for bad_helper, bad_parent, bad_document in (
            (types.SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0, st_gid=4242), good_parent, identity),
            (types.SimpleNamespace(st_mode=stat.S_IFREG | stat.S_ISUID | 0o755, st_uid=1, st_gid=4242), good_parent, identity),
            (types.SimpleNamespace(st_mode=stat.S_IFREG | stat.S_ISUID | 0o755, st_uid=0, st_gid=99), good_parent, identity),
            (good_helper, good_parent, {**identity, "group": "wrong"}),
            (good_helper, types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o775, st_uid=0), identity),
        ):
            with self.subTest(helper=bad_helper, parent=bad_parent, document=bad_document):
                with self.assertRaises(wsl_contained_exec.ContainmentError):
                    run_case(bad_helper, bad_parent, bad_document)

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("cc"),
                         "native Linux compiler unavailable")
    def test_helper_compiles_with_strict_linux_warnings(self):
        source = ROOT / "provisioning" / "quota-admit-task.c"
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "quota-admit-task"
            result = subprocess.run(
                ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(output)],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

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
