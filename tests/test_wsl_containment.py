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
