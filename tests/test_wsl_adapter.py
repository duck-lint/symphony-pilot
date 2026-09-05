from __future__ import annotations

import io
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "runtime"))

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


class FailingStream:
    def read(self, _size):
        raise OSError("synthetic stream failure")


class StreamErrorProcess(FakeProcess):
    def __init__(self):
        super().__init__(returncode=0)
        self.stdout = FailingStream()


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

    def test_quota_inspection_rejects_untrusted_task_selector(self):
        with self.assertRaises(wsl_adapter.WslAdapterError) as raised:
            wsl_adapter.inspect_quota("symphony-pilot", "../../etc")
        self.assertEqual(raised.exception.kind, "invalid_task_identifier")

    def test_quota_inspection_uses_fixed_deployed_control_operation(self):
        process = FakeProcess(stdout=b'{"schema":"symphony-pilot-quota-inspection/v1"}', returncode=0)
        with mock.patch.object(wsl_adapter, "_host_wsl_executable", return_value=pathlib.Path("C:/Windows/System32/wsl.exe")), \
             mock.patch.object(wsl_adapter, "_sterile_windows_environment", return_value={}), \
             mock.patch.object(wsl_adapter.subprocess, "Popen", return_value=process) as popen:
            result = wsl_adapter.inspect_quota("symphony-pilot", "T-000001", request_id="quota-test")
        self.assertEqual(result["schema"], "symphony-pilot-quota-inspection/v1")
        command = popen.call_args.args[0]
        self.assertIn("--control", command)
        self.assertIn("quota-inspect", command)
        self.assertIn("--identifier", command)
        self.assertNotIn("/mnt/f/PROJECT-REPOS/symphony-pilot", command)

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
        self.assertEqual(
            wsl_adapter._validate_command(["bash", "-lc", "echo ok; powershell.exe -c whoami"])[-1],
            "echo ok; powershell.exe -c whoami",
        )

    def test_only_top_level_windows_executable_selection_is_rejected(self):
        for command in (["powershell.exe", "-NoProfile"], ["cmd.exe", "/c", "whoami"]):
            with self.subTest(command=command):
                with self.assertRaises(wsl_adapter.WslAdapterError):
                    wsl_adapter._validate_command(command)

        # Secret-looking paths and privilege words are deliberately allowed as
        # data.  The Linux namespace, not a blacklist, must deny their effects.
        self.assertEqual(
            wsl_adapter._validate_command(["/usr/bin/cat", "/home/duck-lint/.ssh/id_rsa"])[1],
            "/home/duck-lint/.ssh/id_rsa",
        )
        self.assertEqual(
            wsl_adapter._validate_command(["bash", "-lc", "cat secrets/token; sudo id"])[-1],
            "cat secrets/token; sudo id",
        )

    def test_containment_domain_uses_allowlisted_mounts_and_chroot(self):
        import containment

        command = containment.acceptance_domain_command(
            containment.BackendIdentity("schema", "linux-unshare", "/usr/bin/unshare", "v", "a" * 64),
            pathlib.Path("/tmp/domain-root"),
            pathlib.Path("/mnt/f/PROJECT-REPOS/symphony-runtime"),
            pathlib.Path("/home/duck-lint/.local/state/symphony-pilot/wsl-build/runtime"),
            pathlib.Path("/home/duck-lint/.local/bin/mise"),
            pathlib.Path("/home/duck-lint/.local/share/mise"),
            "/project/elixir",
            ["bash", "-lc", "cat /home/duck-lint/.ssh/id_rsa"],
            [(pathlib.Path("/mnt/f/PROJECT-REPOS/symphony-runtime/bin"), "/project/bin")],
        )
        setup = command[-1]
        self.assertIn("mount --make-rprivate /", setup)
        self.assertIn("mount -o remount,ro,bind", setup)
        self.assertIn("exec /usr/sbin/chroot", setup)
        self.assertIn("/project", setup)
        self.assertIn("mise", setup)
        self.assertNotIn("pilot-control", setup)
        self.assertIn("--kill-child=SIGKILL", command)
        self.assertIn("--net", command)
        self.assertNotIn("/home/duck-lint/.config/symphony-pilot/secrets", setup)
        self.assertNotIn("/home/duck-lint/.codex", setup)

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
        self.assertIn(wsl_adapter.CONTAINED_ENTRYPOINT, invocation.args[0])
        self.assertNotIn("/mnt/f/PROJECT-REPOS/symphony-pilot/runtime/wsl_contained_exec.py", invocation.args[0])
        self.assertIn("-B", invocation.args[0])
        self.assertIn("--project", invocation.args[0])
        audit = result.audit_record()
        self.assertNotIn("safe;still-one-arg", audit)
        self.assertNotIn("ok\n", audit)
        self.assertEqual(audit["approval"], "none")

    def test_repeated_supervisor_invocations_use_no_bytecode_mode(self):
        """Two fixed supervisor-style runs cannot create deployment bytecode."""
        process = FakeProcess(stdout=b"ok\n", stderr=b"", returncode=0)
        with mock.patch.object(wsl_adapter, "_host_wsl_executable", return_value=pathlib.Path("C:/Windows/System32/wsl.exe")), \
             mock.patch.object(wsl_adapter, "_canonicalize_cwd", return_value="/mnt/f/PROJECT-REPOS/symphony-pilot"), \
             mock.patch.object(wsl_adapter, "_sterile_windows_environment", return_value={}), \
             mock.patch.object(wsl_adapter.subprocess, "Popen", return_value=process) as popen:
            for index in range(2):
                wsl_adapter.execute(
                    "symphony-pilot", "/mnt/f/PROJECT-REPOS/symphony-pilot", ["/usr/bin/true"],
                    request_id=f"no-bytecode-{index}", timeout_seconds=1,
                )
                command = popen.call_args.args[0]
                python_index = command.index("/usr/bin/python3")
                self.assertEqual(command[python_index + 1], "-B")

        # Exercise the interpreter property independently of the mocked WSL
        # transport: importing an authority module twice with the exact
        # adapter flag must leave the deployment tree unchanged.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "authority.py").write_text("VALUE = 1\n", encoding="utf-8")
            runner = root / "runner.py"
            runner.write_text("import authority\nassert authority.VALUE == 1\n", encoding="utf-8")
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            for _ in range(2):
                subprocess.run([sys.executable, "-B", str(runner)], cwd=root, check=True)
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            self.assertEqual(after, before)

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

    def test_output_reader_failure_fails_closed(self):
        process = StreamErrorProcess()
        with mock.patch.object(wsl_adapter.subprocess, "Popen", return_value=process), \
             mock.patch.object(wsl_adapter, "_sterile_windows_environment", return_value={}):
            with self.assertRaisesRegex(wsl_adapter.WslAdapterError, "output could not be read safely") as raised:
                wsl_adapter._bounded_process(
                    ["wsl.exe"], 1, cwd="/", metadata=("stream", "symphony-runtime", wsl_adapter.FIXED_DISTRO)
                )
        self.assertEqual(raised.exception.kind, "output_read")

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

    def test_nul_padded_wsl_service_failure_is_classified(self):
        unavailable = wsl_adapter.WslExecutionResult(
            request_id="test", project="symphony-runtime", distro=wsl_adapter.FIXED_DISTRO,
            cwd="/", returncode=0xFFFFFFFF,
            stdout="A\x00c\x00c\x00e\x00s\x00s\x00 \x00i\x00s\x00 \x00d\x00e\x00n\x00i\x00e\x00d\x00.\x00\n"
            "\x00E\x00r\x00r\x00o\x00r\x00 \x00c\x00o\x00d\x00e\x00:\x00 \x00W\x00s\x00l\x00/\x00S\x00e\x00r\x00v\x00i\x00c\x00e\x00/\x00E\x00_\x00A\x00C\x00C\x00E\x00S\x00S\x00D\x00E\x00N\x00I\x00E\x00D\x00\n\x00",
            stderr="", timed_out=False, termination="completed", stdout_truncated=False,
            stderr_truncated=False, started_at="now", finished_at="now",
        )
        with self.assertRaises(wsl_adapter.WslAdapterError) as raised:
            wsl_adapter._raise_if_wsl_transport_failed(unavailable)
        self.assertEqual(raised.exception.kind, "wsl_unavailable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
