from __future__ import annotations

import os
import pathlib
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
                with self.assertRaisesRegex(containment.ContainmentError, "mountpoint"):
                    wsl_contained_exec._reject_mountpoint(path, "build")


@unittest.skipIf(os.name == "nt", "requires native Linux/WSL namespace capability")
class WslContainmentIntegrationTests(unittest.TestCase):
    def test_real_domain_denies_secret_and_host_namespace_paths(self):
        identity = containment.require_backend()
        with tempfile.TemporaryDirectory(prefix="symphony-pilot-acceptance-", dir="/tmp") as directory:
            base = pathlib.Path(directory)
            control = base / "pilot-control"
            project = base / "project"
            build = base / "build"
            toolchain_bin = base / "toolchain-bin"
            toolchain_data = base / "toolchain-data"
            root = base / "root"
            for path in (control, project, build, toolchain_bin, toolchain_data, root):
                path.mkdir()
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
                control,
                project,
                build,
                toolchain_bin,
                toolchain_data,
                "/project",
                ["/bin/sh", "-c", hostile],
            )
            result = containment.run_task_domain(command, wall_seconds=30)
            self.assertFalse(result.timed_out, result.stderr)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual((build / "allowed-cache").read_text(encoding="utf-8"), "x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
