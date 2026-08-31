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

import containment
import outbox
import prepare_workspace as pw
import protection
import publication
import runtime_lock
import task_admission


class StructuralCutoverTests(unittest.TestCase):
    def runtime_identity(self):
        item = {"executable": "/reviewed/tool", "version": "tool 1", "sha256": "a" * 64}
        return {"symphony": item, "codex": item, "containment": item}

    def task(self):
        return task_admission.create_task(task_admission.ServerAdmission(
            repository="example/project", project_slug="demo", issue_number=10,
            trusted_dispatcher="host", default_ref="master", base_sha="b" * 40,
            workpad_comment_id=42, runtime_identity=self.runtime_identity(),
        ), task_id="c" * 32)

    def test_branch_is_host_derived_and_prose_cannot_supply_it(self):
        task = self.task()
        self.assertEqual(task["issue_branch"], "codex/gh-10-cccccccccccc")
        tampered = dict(task, issue_branch="master")
        with self.assertRaises(task_admission.TaskAdmissionError):
            task_admission.validate_task_record(tampered)

    def test_task_record_is_strict_and_round_trips_outside_workspace(self):
        task = self.task()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "tasks" / "GH-10" / "task.json"
            task_admission.write_task(path, task)
            self.assertEqual(task_admission.read_task(path), task)
            malformed = dict(task, attacker_control="master")
            with self.assertRaises(task_admission.TaskAdmissionError):
                task_admission.validate_task_record(malformed)

    def test_outbox_rejects_unknown_fields_and_wrong_task(self):
        task = self.task()
        request = {"schema": outbox.OUTBOX_SCHEMA, "task_id": task["task_id"],
                   "action": "publish", "head": "d" * 40, "summary": "ok"}
        self.assertEqual(outbox.validate_request(request, task), request)
        with self.assertRaises(outbox.OutboxError):
            outbox.validate_request(dict(request, branch="master"), task)
        with self.assertRaises(outbox.OutboxError):
            outbox.validate_request(dict(request, task_id="e" * 32), task)
        with self.assertRaises(outbox.OutboxError):
            outbox.validate_request(dict(request, action="complete", head=None), task)

    def test_branch_protection_is_fail_closed(self):
        good = {"protected": True, "required_pull_request": True,
                "automation_can_bypass": False, "human_merge_actor": "human"}
        self.assertEqual(protection.require_protected_default(good, "automation"), good)
        for bad in (
            dict(good, protected=False),
            dict(good, required_pull_request=False),
            dict(good, automation_can_bypass=True),
            dict(good, human_merge_actor="automation"),
        ):
            with self.assertRaises(protection.ProtectionError):
                protection.require_protected_default(bad, "automation")

    def test_rendered_workflow_has_no_parent_write_root_or_open_network(self):
        from render_workflow import render
        profile = pw.Profile(
            slug="demo", repository="example/project", git_remote="git@example:project.git",
            workspace_root=pathlib.Path("/home/operator/symphony-workspaces/demo"),
            state_root=pathlib.Path("/home/operator/.local/state/symphony-pilot/demo"),
            log_root=pathlib.Path("/home/operator/.local/state/symphony-pilot/demo/logs"),
            secret_reference="github.token", dispatch_labels=("symphony:auto",),
            blocked_label="symphony:human", service_identity="symphony-pilot-demo",
            dashboard_port=4040, max_concurrent_agents=1, max_turns=8,
            poll_interval_ms=1000, max_retry_backoff_ms=1000,
            codex_model="gpt-5.6-luna", codex_reasoning_effort="high", toolchain=None,
        )
        with tempfile.TemporaryDirectory() as directory:
            policy = pathlib.Path(directory) / "policy.md"
            policy.write_text("policy\n", encoding="utf-8")
            text = render(profile, pathlib.Path(directory), policy)
        self.assertIn("type: externalSandbox", text)
        self.assertIn("networkAccess: restricted", text)
        self.assertNotIn("networkAccess: true", text)
        self.assertNotIn(str(profile.workspace_root), text.split("turn_sandbox_policy:", 1)[1])

    def test_launcher_does_not_reference_operator_codex_home_or_symlinks(self):
        text = (ROOT / "runtime/launch_codex.sh").read_text(encoding="utf-8")
        self.assertNotIn("ORIGINAL_CODEX_HOME", text)
        self.assertNotIn("SYMPHONY_PILOT_ROLE_POLICY_DIR", text)
        self.assertNotIn("ln -s", text)
        self.assertIn("CODEX_API_KEY", text)
        self.assertIn("exit 78", text)

    def test_auth_boundary_is_an_explicit_blocker(self):
        with mock.patch.object(containment, "require_backend", return_value=mock.Mock()):
            with self.assertRaises(containment.ContainmentError) as raised:
                containment.require_execution_capability()
        self.assertEqual(raised.exception.kind, "codex_auth_boundary")

    def test_publication_environment_drops_task_credential_channels(self):
        with mock.patch.dict("os.environ", {"SSH_AUTH_SOCK": "/tmp/socket", "GIT_CONFIG_GLOBAL": "bad"}, clear=True):
            env = publication.safe_git_environment()
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_runtime_lock_is_strict(self):
        lock = {"schema": runtime_lock.LOCK_SCHEMA, **self.runtime_identity()}
        self.assertEqual(runtime_lock.validate_lock(lock), lock)
        with self.assertRaises(runtime_lock.RuntimeLockError):
            runtime_lock.validate_lock(dict(lock, extra=True))

    def test_blocker_detail_is_persisted_in_host_state(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = mock.Mock(state_root=pathlib.Path(directory))
            profile.slug = "demo"
            pw.record_blocker(profile, ROOT,
                              pw.IssueFacts(10, "codex/gh-10-cccccccccccc", "b" * 40,
                                            "master", "b" * 40, "initial", None, None, []),
                              "test_blocker", "exact diagnostic detail")
            saved = json.loads((pathlib.Path(directory) / "blockers" / "GH-10.json").read_text())
        self.assertEqual(saved["schema"], "symphony-pilot-blocker/v1")
        self.assertEqual(saved["detail"], "exact diagnostic detail")


if __name__ == "__main__":
    unittest.main(verbosity=2)
