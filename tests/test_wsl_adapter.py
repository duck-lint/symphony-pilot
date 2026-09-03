from __future__ import annotations

import io
import pathlib
import unittest
from unittest import mock

import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import wsl_adapter


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, running=False):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None if running else returncode
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class WslAdapterTests(unittest.TestCase):
    def test_fixed_distribution_and_user_are_structured(self):
        command = wsl_adapter._raw_command(pathlib.Path("C:/Windows/System32/wsl.exe"), ["/usr/bin/id"], "/mnt/f/PROJECT-REPOS/symphony-runtime")
        self.assertEqual(command[:8], [
            str(pathlib.Path("C:/Windows/System32/wsl.exe")), "--distribution", "Ubuntu-24.04",
            "--user", "duck-lint", "--cd", "/mnt/f/PROJECT-REPOS/symphony-runtime", "--exec",
        ])

    def test_other_distribution_is_rejected(self):
        with self.assertRaisesRegex(wsl_adapter.WslAdapterError, "only Ubuntu-24.04"):
            wsl_adapter.execute("symphony-runtime", "/mnt/f/PROJECT-REPOS/symphony-runtime", ["/usr/bin/id"], distro="Debian")

    def test_cwd_traversal_aliases_and_windows_forms_are_rejected(self):
        cases = (
            "/mnt/f/PROJECT-REPOS/symphony-runtime/../../etc",
            r"F:\\PROJECT-REPOS\\symphony-runtime",
            r"\\\\wsl.localhost\\Ubuntu-24.04\\mnt\\f",
            "/mnt/c/Users/madis",
        )
        for cwd in cases:
            with self.subTest(cwd=cwd):
                with self.assertRaises(wsl_adapter.WslAdapterError):
                    wsl_adapter._lexical_cwd("symphony-runtime", cwd)

    def test_canonical_symlink_escape_is_rejected(self):
        escape = wsl_adapter.WslExecutionResult(
            request_id="test", project="symphony-runtime", distro=wsl_adapter.FIXED_DISTRO,
            cwd="/", returncode=0, stdout="/home/duck-lint/.ssh", stderr="", timed_out=False,
            termination="completed", stdout_truncated=False, stderr_truncated=False,
            started_at="now", finished_at="now",
        )
        with mock.patch.object(wsl_adapter, "_bounded_process", return_value=escape):
            with self.assertRaisesRegex(wsl_adapter.WslAdapterError, "leaves the approved"):
                wsl_adapter._canonicalize_cwd(
                    "symphony-runtime", "/mnt/f/PROJECT-REPOS/symphony-runtime", pathlib.Path("wsl.exe"), "test"
                )
        with self.assertRaises(wsl_adapter.WslAdapterError) as raised:
            wsl_adapter._validate_project("unknown")
        self.assertEqual(raised.exception.kind, "unknown_project")

    def test_shell_metacharacters_remain_one_linux_argument(self):
        command = wsl_adapter._validate_command(["/usr/bin/printf", "safe; echo not-a-second-command"])
        self.assertEqual(command[1], "safe; echo not-a-second-command")
        self.assertEqual(wsl_adapter._validate_command(["/usr/bin/printf", "safe\nnot-a-command"])[1], "safe\nnot-a-command")
        with self.assertRaises(wsl_adapter.WslAdapterError):
            wsl_adapter._validate_command(["bash", "-lc", "echo ok; powershell.exe -c whoami"])

    def test_windows_commands_secrets_and_privilege_escalation_are_rejected(self):
        for command in (
            ["powershell.exe", "-NoProfile"],
            ["/usr/bin/cat", "/home/duck-lint/.ssh/id_rsa"],
            ["/usr/bin/sudo", "id"],
            ["/usr/bin/printf", "C:\\Users\\madis"],
        ):
            with self.subTest(command=command):
                with self.assertRaises(wsl_adapter.WslAdapterError):
                    wsl_adapter._validate_command(command)

    def test_execution_uses_sterile_environment_and_returns_structured_result(self):
        process = FakeProcess(stdout=b"ok\n", stderr=b"", returncode=7)
        with mock.patch.object(wsl_adapter, "_host_wsl_executable", return_value=pathlib.Path("C:/Windows/System32/wsl.exe")), \
             mock.patch.object(wsl_adapter, "_canonicalize_cwd", return_value="/mnt/f/PROJECT-REPOS/symphony-runtime/elixir"), \
             mock.patch.object(wsl_adapter, "_sterile_windows_environment", return_value={"SystemRoot": r"C:\Windows", "WINDIR": r"C:\Windows"}), \
             mock.patch.object(wsl_adapter.subprocess, "Popen", return_value=process) as popen, \
             mock.patch.dict(wsl_adapter.os.environ, {"SystemRoot": r"C:\Windows", "GITHUB_TOKEN": "must-not-cross"}, clear=True):
            result = wsl_adapter.execute(
                "symphony-runtime",
                "/mnt/f/PROJECT-REPOS/symphony-runtime/elixir",
                ["/usr/bin/printf", "safe;still-one-arg"],
                request_id="test-1",
                timeout_seconds=1,
            )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.distro, "Ubuntu-24.04")
        self.assertEqual(result.cwd, "/mnt/f/PROJECT-REPOS/symphony-runtime/elixir")
        self.assertEqual(result.termination, "completed")
        invocation = popen.call_args
        self.assertFalse(invocation.kwargs["shell"])
        self.assertEqual(invocation.kwargs["env"], {"SystemRoot": r"C:\Windows", "WINDIR": r"C:\Windows"})
        self.assertNotIn("GITHUB_TOKEN", invocation.kwargs["env"])
        self.assertIn("safe;still-one-arg", invocation.args[0])
        audit = result.audit_record()
        self.assertNotIn("safe;still-one-arg", audit)
        self.assertNotIn("ok\n", audit)
        self.assertEqual(audit["approval"], "none")

    def test_timeout_and_output_limits_terminate_the_process(self):
        timed_out = FakeProcess(running=True)
        with mock.patch.object(wsl_adapter.subprocess, "Popen", return_value=timed_out), \
             mock.patch.object(wsl_adapter, "_sterile_windows_environment", return_value={}):
            result = wsl_adapter._bounded_process(
                ["wsl.exe"], 0.001, cwd="/", metadata=("timeout", "symphony-runtime", wsl_adapter.FIXED_DISTRO)
            )
        self.assertTrue(result.timed_out)
        self.assertTrue(timed_out.killed)

        noisy = FakeProcess(stdout=b"x" * (wsl_adapter.MAX_OUTPUT_BYTES + 1), running=True)
        with mock.patch.object(wsl_adapter.subprocess, "Popen", return_value=noisy), \
             mock.patch.object(wsl_adapter, "_sterile_windows_environment", return_value={}):
            result = wsl_adapter._bounded_process(
                ["wsl.exe"], 1, cwd="/", metadata=("output", "symphony-runtime", wsl_adapter.FIXED_DISTRO)
            )
        self.assertEqual(result.termination, "output_limit")
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(noisy.killed)

    def test_malformed_output_fails_closed(self):
        with self.assertRaises(wsl_adapter.WslAdapterError) as raised:
            wsl_adapter._decode_output(b"\xff\xfe\xff", False)
        self.assertEqual(raised.exception.kind, "malformed_output")

    def test_missing_or_non_directory_cwd_fails_closed(self):
        missing = wsl_adapter.WslExecutionResult(
            request_id="test", project="symphony-runtime", distro=wsl_adapter.FIXED_DISTRO,
            cwd="/", returncode=1, stdout="", stderr="", timed_out=False,
            termination="completed", stdout_truncated=False, stderr_truncated=False,
            started_at="now", finished_at="now",
        )
        with mock.patch.object(wsl_adapter, "_bounded_process", return_value=missing):
            with self.assertRaisesRegex(wsl_adapter.WslAdapterError, "could not be canonicalized"):
                wsl_adapter._canonicalize_cwd(
                    "symphony-runtime", "/mnt/f/PROJECT-REPOS/symphony-runtime/missing", pathlib.Path("wsl.exe"), "test"
                )

    def test_wsl_service_failure_is_not_reported_as_cwd_failure(self):
        unavailable = wsl_adapter.WslExecutionResult(
            request_id="test", project="symphony-runtime", distro=wsl_adapter.FIXED_DISTRO,
            cwd="/", returncode=0xFFFFFFFF,
            stdout="Access is denied.\nError code: Wsl/Service/CreateInstance/E_ACCESSDENIED\n",
            stderr="", timed_out=False, termination="completed", stdout_truncated=False,
            stderr_truncated=False, started_at="now", finished_at="now",
        )
        with mock.patch.object(wsl_adapter, "_bounded_process", return_value=unavailable):
            with self.assertRaisesRegex(wsl_adapter.WslAdapterError, "fixed WSL distribution is unavailable") as raised:
                wsl_adapter._canonicalize_cwd(
                    "symphony-runtime", "/mnt/f/PROJECT-REPOS/symphony-runtime", pathlib.Path("wsl.exe"), "test"
                )
        self.assertEqual(raised.exception.kind, "wsl_unavailable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
