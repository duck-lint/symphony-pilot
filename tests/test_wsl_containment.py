from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

import containment
import wsl_contained_exec


class SupervisorValidationTests(unittest.TestCase):
    def test_quota_inspection_rejects_untrusted_project(self):
        with self.assertRaisesRegex(wsl_contained_exec.ContainmentError, "not admitted"):
            wsl_contained_exec._workspace_storage_root("../../etc")

    def test_quota_storage_root_creation_does_not_create_task_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            namespace = pathlib.Path(directory) / "symphony-workspaces"
            with mock.patch.object(wsl_contained_exec, "WORKSPACE_ROOT", namespace):
                storage_root, created = wsl_contained_exec._workspace_storage_root("symphony-pilot")
            self.assertTrue(created)
            self.assertEqual(storage_root, namespace)
            self.assertTrue(storage_root.is_dir())
            self.assertFalse((storage_root / "T-000001").exists())

    def test_quota_inspection_requires_deployment_validation(self):
        with mock.patch.object(wsl_contained_exec, "_deployment_root", return_value=pathlib.Path("/trusted")), \
             mock.patch.object(wsl_contained_exec, "_validate_deployment") as validate, \
             mock.patch.object(wsl_contained_exec, "_quota_inspection", return_value={"schema": "test"}), \
             mock.patch.object(sys, "argv", [
                 "wsl_contained_exec.py", "--project", "symphony-pilot",
                 "--control", "quota-inspect-root",
             ]), \
             mock.patch("builtins.print") as output:
            self.assertEqual(wsl_contained_exec.main(), 0)
        validate.assert_called_once_with(pathlib.Path("/trusted"))
        output.assert_called_once()

    def test_quota_inspection_returns_bounded_mount_and_capacity_evidence(self):
        class Usage:
            f_frsize = 4096
            f_bsize = 4096
            f_blocks = 100
            f_bfree = 40
            f_bavail = 35
            f_files = 200
            f_ffree = 150
            f_favail = 145

        findmnt = mock.Mock(returncode=0, stdout=json.dumps({
            "filesystems": [{
                    "target": "/home/duck-lint/symphony-workspaces",
                "source": "/dev/vdb",
                "fstype": "ext4",
                "options": "rw,relatime,prjquota",
            }],
        }))
        with mock.patch.object(wsl_contained_exec, "_workspace_storage_root", return_value=(pathlib.PurePosixPath("/workspace"), False)), \
             mock.patch.object(wsl_contained_exec.subprocess, "run", return_value=findmnt) as run, \
             mock.patch.object(wsl_contained_exec.os, "statvfs", return_value=Usage(), create=True):
            evidence = wsl_contained_exec._quota_inspection("symphony-pilot")
        self.assertTrue(evidence["filesystem"]["project_quota_mount"])
        self.assertEqual(evidence["filesystem"]["statvfs"]["inodes"], 200)
        self.assertEqual(evidence["filesystem"]["statvfs"]["available_blocks"], 35)
        self.assertEqual(evidence["filesystem"]["statvfs"]["available_inodes"], 145)
        run.assert_called_once_with(
            ["/bin/findmnt", "--json", "--target", "/workspace",
             "--output", "TARGET,SOURCE,FSTYPE,OPTIONS"],
            capture_output=True, text=True, timeout=5, check=False,
        )

    def test_quota_inspection_uses_and_removes_inert_probe_for_new_root(self):
        class Usage:
            f_frsize = 4096
            f_bsize = 4096
            f_blocks = 100
            f_bfree = 40
            f_bavail = 35
            f_files = 200
            f_ffree = 150
            f_favail = 145

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "project-root"
            root.mkdir()
            findmnt = mock.Mock(returncode=0, stdout=json.dumps({
                "filesystems": [{
                    "target": str(root / wsl_contained_exec.QUOTA_PROBE_NAME),
                    "source": "/dev/vdb",
                    "fstype": "ext4",
                    "options": "rw,relatime,prjquota",
                }],
            }))
            with mock.patch.object(wsl_contained_exec, "_workspace_storage_root", return_value=(root, True)), \
                 mock.patch.object(wsl_contained_exec.subprocess, "run", return_value=findmnt), \
                 mock.patch.object(wsl_contained_exec.os, "statvfs", return_value=Usage(), create=True):
                evidence = wsl_contained_exec._quota_inspection("symphony-pilot")
            self.assertTrue(evidence["probe_created"])
            self.assertFalse((root / wsl_contained_exec.QUOTA_PROBE_NAME).exists())

    def test_task_quota_admission_returns_fixed_helper_proof(self):
        pool = {
            "schema": "symphony-pilot-quota-inspection/v1",
            "project": "symphony-pilot",
            "scope": "persistent_symphony_workspace_pool",
            "filesystem": {"target": "/home/duck-lint/symphony-workspaces"},
            "ownership": {"trusted": True},
            "quota": {"backend": "ext4-project-quota", "mount_support": True},
        }
        helper = {
            "schema": "symphony-pilot-task-quota-proof/v1",
            "identifier": "T-000001",
            "workspace_path": "/home/duck-lint/symphony-workspaces/symphony-pilot/T-000001",
            "project_id": 1_000_001, "workspace_project_id": 1_000_001,
            "byte_hard_limit": 8 * 1024 ** 3, "inode_hard_limit": 250_000,
            "usage": {"bytes": 0, "inodes": 1},
            "byte_probe": {"attempted": True, "result": "EDQUOT"},
            "inode_probe": {"attempted": True, "result": "EDQUOT"},
        }
        completed = mock.Mock(returncode=0, stdout=json.dumps(helper))
        with mock.patch.object(wsl_contained_exec, "_quota_inspection", return_value=pool), \
             mock.patch.object(wsl_contained_exec, "_quota_helper_fd", return_value=7), \
             mock.patch("wsl_contained_exec.os.close"), \
             mock.patch.object(wsl_contained_exec.subprocess, "run", return_value=completed):
            evidence = wsl_contained_exec._quota_task_admission(
                "symphony-pilot", "T-000001", 8 * 1024 ** 3, 250_000,
            )
        self.assertEqual(evidence["schema"], "symphony-pilot-task-quota-admission/v1")
        self.assertEqual(evidence["pool"], pool)
        self.assertEqual(evidence["task_quota"], helper)

    def test_writable_mountpoint_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            with mock.patch.object(wsl_contained_exec.os.path, "ismount", return_value=True):
                with self.assertRaisesRegex(wsl_contained_exec.ContainmentError, "mountpoint"):
                    wsl_contained_exec._reject_mountpoint(path, "build")


@unittest.skipIf(os.name == "nt", "requires native Linux/WSL namespace capability")
class WslContainmentIntegrationTests(unittest.TestCase):
    def test_real_domain_denies_secret_and_host_namespace_paths(self):
        identity = containment.require_backend()
        with tempfile.TemporaryDirectory(prefix="symphony-pilot-acceptance-", dir="/tmp") as directory:
            base = pathlib.Path(directory)
            project = base / "project"
            build = base / "build"
            toolchain_executable = base / "mise"
            toolchain_data = base / "toolchain-data"
            root = base / "root"
            for path in (project, build, toolchain_data, root):
                path.mkdir()
            toolchain_executable.write_text("mise", encoding="utf-8")
            (project / "read-only.txt").write_text("source", encoding="utf-8")
            (project / "bin").mkdir()
            (project / "burrito_out").mkdir()

            hostile = """
            set -eu
            test ! -e /home/duck-lint/.ssh
            test ! -e /home/duck-lint/.codex
            test ! -e /home/duck-lint/.config/symphony-pilot/secrets
            test ! -e /mnt/c
            test ! -e /etc/passwd
            test ! -e /outside-secret
            test ! -e /proc/self/root/home/duck-lint/.ssh
            test ! -e /proc/1/root/home/duck-lint/.ssh
            test -z "${GITHUB_TOKEN-}"
            test -z "${OPENAI_API_KEY-}"
            test ! -w /project/read-only.txt
            printf x > /build/allowed-cache
            ln -s /home/duck-lint/.ssh /build/escape
            test ! -e /build/escape/id_rsa
            if command -v powershell.exe || command -v cmd.exe || command -v wsl.exe; then
              exit 1
            fi
            find / -maxdepth 2 -name id_rsa -o -name auth.json | grep . && exit 1 || true
            """
            command = containment.acceptance_domain_command(
                identity,
                root,
                project,
                build,
                toolchain_executable,
                toolchain_data,
                "/project",
                ["/bin/sh", "-c", hostile],
            )
            result = containment.run_task_domain(command, wall_seconds=30)
            self.assertFalse(result.timed_out, result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual((build / "allowed-cache").read_text(encoding="utf-8"), "x")


class DeploymentIdentityTests(unittest.TestCase):
    def test_manifest_verifies_authority_files_and_identity(self):
        import hashlib
        import json

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "demo"
            root.mkdir()
            runtime = root / "runtime"
            runtime.mkdir()
            files = {}
            for relative, content in (
                ("runtime/wsl_contained_exec.py", b"supervisor"),
                ("runtime/containment.py", b"containment"),
            ):
                path = root / pathlib.PurePosixPath(relative)
                path.write_bytes(content)
                files[relative] = hashlib.sha256(content).hexdigest()
            files["profile.toml"] = hashlib.sha256(b"profile").hexdigest()
            (root / "profile.toml").write_bytes(b"profile")
            payload = {"files": files, "operator_contract_sha256": "b" * 64,
                       "profile": "demo", "profile_sha256": "c" * 64}
            identity = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            (root / "DEPLOYMENT.json").write_text(json.dumps({
                "schema": wsl_contained_exec.DEPLOYMENT_SCHEMA,
                "profile": "demo", "profile_sha256": "c" * 64,
                "operator_contract_sha256": "b" * 64,
                "deployment_identity": identity,
                "source_commit": "a" * 40, "files": files,
            }), encoding="utf-8")
            self.assertEqual(wsl_contained_exec._validate_deployment(root)["profile"], "demo")

    def test_manifest_tampering_fails_before_containment_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "runtime").mkdir()
            (root / "runtime/wsl_contained_exec.py").write_text("bad", encoding="utf-8")
            with self.assertRaisesRegex(wsl_contained_exec.ContainmentError, "manifest"):
                wsl_contained_exec._validate_deployment(root)

    def test_independently_injected_file_remains_unlisted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "demo"
            root.mkdir()
            runtime = root / "runtime"
            runtime.mkdir()
            files = {}
            for relative, content in (
                ("runtime/wsl_contained_exec.py", b"supervisor"),
                ("runtime/containment.py", b"containment"),
            ):
                path = root / pathlib.PurePosixPath(relative)
                path.write_bytes(content)
                files[relative] = hashlib.sha256(content).hexdigest()
            profile = root / "profile.toml"
            profile.write_bytes(b"profile")
            files["profile.toml"] = hashlib.sha256(profile.read_bytes()).hexdigest()
            payload = {"files": files, "operator_contract_sha256": "b" * 64,
                       "profile": "demo", "profile_sha256": "c" * 64}
            identity = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            (root / "DEPLOYMENT.json").write_text(json.dumps({
                "schema": wsl_contained_exec.DEPLOYMENT_SCHEMA,
                "profile": "demo", "profile_sha256": "c" * 64,
                "operator_contract_sha256": "b" * 64,
                "deployment_identity": identity,
                "source_commit": "a" * 40, "files": files,
            }), encoding="utf-8")
            (runtime / "__pycache__").mkdir()
            (runtime / "__pycache__" / "injected.pyc").write_bytes(b"unlisted")
            with self.assertRaisesRegex(wsl_contained_exec.ContainmentError, "unlisted file"):
                wsl_contained_exec._validate_deployment(root)

    def test_output_symlink_is_rejected_after_contained_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "bin").mkdir()
            (root / "burrito_out").mkdir()
            try:
                (root / "bin/symphony").symlink_to("/home/duck-lint/.ssh/id_rsa")
            except (OSError, NotImplementedError) as exc:
                with mock.patch.object(wsl_contained_exec.os, "lstat",
                                       return_value=mock.Mock(st_mode=stat.S_IFLNK)):
                    with self.assertRaisesRegex(wsl_contained_exec.ContainmentError, "regular file"):
                        wsl_contained_exec._validate_outputs("symphony-runtime", root)
                return
            with self.assertRaisesRegex(wsl_contained_exec.ContainmentError, "regular file"):
                wsl_contained_exec._validate_outputs("symphony-runtime", root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
