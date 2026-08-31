from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "scripts"))

import containment
import outbox
import prepare_workspace as pw
import protection
import publication
import project_registry
import runtime_lock
import task_admission


class InfrastructureTests(unittest.TestCase):
    def profile(self, root: pathlib.Path, slug: str = "demo", repository: str = "example/demo", port: int = 4040):
        return pw.Profile(
            slug=slug, repository=repository, git_remote="git@example:" + repository + ".git",
            workspace_root=root / "work", state_root=root / "state", log_root=root / "logs",
            secret_reference="github.token", dispatch_labels=("symphony:auto",),
            blocked_label="symphony:human", service_identity="symphony-pilot-" + slug,
            dashboard_port=port, max_concurrent_agents=1, max_turns=8,
            poll_interval_ms=1000, max_retry_backoff_ms=1000,
            codex_model="gpt-5.6-luna", codex_reasoning_effort="high", toolchain=None,
        )

    def runtime_identity(self):
        item = {"executable": "/reviewed/tool", "version": "tool 1", "sha256": "a" * 64}
        return {"symphony": item, "codex": item, "containment": item}

    def task(self):
        return task_admission.create_task(task_admission.ServerAdmission(
            repository="example/project", project_slug="demo", issue_number=10,
            trusted_dispatcher="host", default_ref="master", base_sha="b" * 40,
            workpad_comment_id=42, runtime_identity=self.runtime_identity(),
        ), task_id="c" * 32)

    def test_canonical_registry_is_arbitrary_and_collision_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            paths = []
            for index in range(5):
                path = root / ("p" + str(index))
                path.mkdir()
                (path / "profile.toml").write_text(
                    f'slug = "p{index}"\nrepository = "owner/r{index}"\n'
                    'git_remote = "git@example:repo.git"\nsecret_reference = "github.token"\n'
                    'dispatch_labels = ["auto"]\nblocked_label = "human"\n'
                    f'dashboard_port = {4100 + index}\nmax_concurrent_agents = 1\n'
                    'max_turns = 1\npoll_interval_ms = 1000\nmax_retry_backoff_ms = 0\n'
                    'codex_model = "model"\ncodex_reasoning_effort = "high"\n', encoding="utf-8")
                paths.append(path)
            self.assertEqual(len(project_registry.validate_registry(root)), 5)
            (root / "p5").mkdir()
            (root / "p5" / "profile.toml").write_text((paths[0] / "profile.toml").read_text().replace("p0", "p5"), encoding="utf-8")
            with self.assertRaises(pw.PreparationError):
                project_registry.validate_registry(root)

    def test_profiles_have_no_credential_values(self):
        schema = json.loads((ROOT / "schemas/project-profile.schema.json").read_text())
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("token", schema["properties"])

    def test_task_admission_ignores_prose_and_derives_branch(self):
        task = self.task()
        self.assertEqual(task["issue_branch"], "codex/gh-10-cccccccccccc")
        with self.assertRaises(task_admission.TaskAdmissionError):
            task_admission.validate_task_record(dict(task, issue_branch="master"))

    def test_task_record_is_strict_and_host_owned(self):
        task = self.task()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "tasks" / "GH-10" / "task.json"
            task_admission.write_task(path, task)
            self.assertEqual(task_admission.read_task(path), task)
            with self.assertRaises(task_admission.TaskAdmissionError):
                task_admission.validate_task_record(dict(task, attacker_control="master"))

    def test_outbox_and_protection_fail_closed(self):
        task = self.task()
        request = {"schema": outbox.OUTBOX_SCHEMA, "task_id": task["task_id"],
                   "action": "publish", "head": "d" * 40, "summary": "ok"}
        self.assertEqual(outbox.validate_request(request, task), request)
        with self.assertRaises(outbox.OutboxError):
            outbox.validate_request(dict(request, branch="master"), task)
        good = {"protected": True, "required_pull_request": True,
                "automation_can_bypass": False, "human_merge_actor": "human"}
        protection.require_protected_default(good, "automation")
        with self.assertRaises(protection.ProtectionError):
            protection.require_protected_default(dict(good, automation_can_bypass=True), "automation")

    def test_workflow_removes_parent_write_root_and_open_network(self):
        from render_workflow import render
        profile = self.profile(pathlib.Path("/tmp"))
        with tempfile.TemporaryDirectory() as directory:
            policy = pathlib.Path(directory) / "policy.md"
            policy.write_text("policy\n", encoding="utf-8")
            rendered = render(profile, pathlib.Path(directory), policy)
        self.assertIn("type: externalSandbox", rendered)
        self.assertIn("networkAccess: restricted", rendered)
        self.assertNotIn("networkAccess: true", rendered)
        self.assertNotIn(str(profile.workspace_root), rendered.split("turn_sandbox_policy:", 1)[1])

    def test_launcher_is_minimal_and_fail_closed(self):
        text = (ROOT / "runtime/launch_codex.sh").read_text(encoding="utf-8")
        self.assertNotIn("ORIGINAL_CODEX_HOME", text)
        self.assertNotIn("ln -s", text)
        self.assertIn("CODEX_API_KEY", text)
        self.assertIn("exit 78", text)

    def test_containment_backend_has_explicit_auth_blocker(self):
        with mock.patch.object(containment, "require_backend", return_value=mock.Mock()):
            with self.assertRaises(containment.ContainmentError) as raised:
                containment.require_execution_capability()
        self.assertEqual(raised.exception.kind, "codex_auth_boundary")

    def test_publication_drops_task_credential_channels(self):
        with mock.patch.dict("os.environ", {"SSH_AUTH_SOCK": "/tmp/socket", "GIT_CONFIG_GLOBAL": "bad"}, clear=True):
            env = publication.safe_git_environment()
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")

    def test_runtime_lock_is_strict(self):
        lock = {"schema": runtime_lock.LOCK_SCHEMA, **self.runtime_identity()}
        self.assertEqual(runtime_lock.validate_lock(lock), lock)
        with self.assertRaises(runtime_lock.RuntimeLockError):
            runtime_lock.validate_lock(dict(lock, extra=True))

    def test_deployment_dry_run_is_available_without_secret(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/deploy.py"),
                                 "--project", "cleanroom", "--dry-run"],
                                text=True, capture_output=True, check=True)
        self.assertIn('"profile": "cleanroom"', result.stdout)

    def test_python_and_shell_contracts_compile(self):
        result = subprocess.run([sys.executable, "-m", "compileall", "-q", "runtime", "scripts", "tests"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
