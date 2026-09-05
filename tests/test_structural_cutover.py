from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

import containment
import control_db
import lifecycle
import outbox
import prepare_workspace as pw
import rulesets
import publication
import runtime_lock
import task_admission


class StructuralCutoverTests(unittest.TestCase):
    def git(self, cwd, *args):
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def runtime_identity(self):
        item = {"executable": "/reviewed/tool", "version": "tool 1", "sha256": "a" * 64}
        return {"symphony": item, "codex": item, "containment": item}

    def task(self):
        return task_admission.create_task(task_admission.ServerAdmission(
            repository="example/project", project_slug="demo", issue_number=10,
            dispatch_provenance=[{"label": "symphony:auto", "actor": "duck-lint", "event_id": 1, "created_at": "2026-01-01T00:00:00Z"}],
            default_ref="master", base_sha="b" * 40,
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
                   "head": "d" * 40, "workpad_body": "ok",
                   "disposition": "ready_for_human_merge", "summary": "ok"}
        self.assertEqual(outbox.validate_request(request, task), request)
        with self.assertRaises(outbox.OutboxError):
            outbox.validate_request(dict(request, branch="master"), task)
        with self.assertRaises(outbox.OutboxError):
            outbox.validate_request(dict(request, task_id="e" * 32), task)
        with self.assertRaises(outbox.OutboxError):
            outbox.validate_request(dict(request, disposition="publish"), task)

    def test_ruleset_contract_is_real_shape_and_fail_closed(self):
        good = {"id": 7, "target": "branch", "enforcement": "active",
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                "bypass_actors": [], "rules": [{"type": "pull_request"}]}
        self.assertEqual(rulesets.require_default_branch_ruleset([good], "master"), good)
        for bad in (dict(good, enforcement="disabled"),
                     dict(good, bypass_actors=[{"actor_type": "Integration"}]),
                     dict(good, rules=[])):
            with self.assertRaises(rulesets.RulesetError):
                rulesets.require_default_branch_ruleset([bad], "master")

    def test_rendered_workflow_has_no_parent_write_root_or_open_network(self):
        from render_workflow import render
        profile = pw.Profile(
            slug="demo", repository="example/project", git_remote="git@example:project.git",
            workspace_root=pathlib.Path("/home/operator/symphony-workspaces/demo"),
            state_root=pathlib.Path("/home/operator/.local/state/symphony-pilot/demo"),
            log_root=pathlib.Path("/home/operator/.local/state/symphony-pilot/demo/logs"),
            secret_reference="github.token", trusted_dispatchers=("duck-lint",), dispatch_labels=("symphony:auto",),
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

    def test_publication_does_not_consume_legacy_model_bundle(self):
        source = (ROOT / "runtime/publication.py").read_text(encoding="utf-8")
        self.assertNotIn("from outbox", source)
        self.assertNotIn("task_bundle_path", source)
        self.assertNotIn("ready_for_human_merge", source)

    def test_runtime_lock_is_strict(self):
        lock = {"schema": runtime_lock.LOCK_SCHEMA, **self.runtime_identity()}
        self.assertEqual(runtime_lock.validate_lock(lock), lock)
        with self.assertRaises(runtime_lock.RuntimeLockError):
            runtime_lock.validate_lock(dict(lock, extra=True))

    def test_blocker_detail_is_persisted_in_sqlite_host_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = pathlib.Path(directory) / "control.sqlite3"
            with control_db.open_database(database_path) as database:
                task = database.create_task(
                    project_slug="demo", title="Task", objective="Test blocker",
                    base_ref="master", base_sha="b" * 40,
                    task_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
                )
            profile = mock.Mock(state_root=pathlib.Path(directory), slug="demo")
            facts = pw.LocalTaskFacts(
                task_uuid=task["id"], identifier=task["identifier"], branch=task["branch"],
                selected_head=task["base_sha"], base_ref=task["base_ref"], base_sha=task["base_sha"],
                mode="initial", current_head=None, published_head=None,
            )
            with mock.patch.object(pw, "control_database_path", return_value=database_path):
                pw.record_blocker(profile, ROOT, facts, "test_blocker", "exact diagnostic detail")
            with control_db.open_database_readonly(database_path) as database:
                blockers = database.read_projection(task["id"])["blockers"]
        self.assertEqual(blockers[0]["kind"], "infrastructure")
        self.assertIn("exact diagnostic detail", blockers[0]["body"])

    def test_step6_lifecycle_broker_has_no_github_lifecycle_surface(self):
        source = (ROOT / "runtime/lifecycle.py").read_text(encoding="utf-8")
        self.assertNotIn("from prepare_workspace import github", source)
        self.assertNotIn("/issues/", source)
        self.assertEqual(lifecycle.RESULT_SCHEMA, "symphony-pilot-lifecycle-result/v1")
        self.assertIn("requested_resolved_finding_ids", source)

    def test_publication_remote_is_registered_repository_derived(self):
        profile = mock.Mock(repository="owner/project")
        self.assertEqual(
            publication.canonical_publication_remote(profile),
            "git@github.com:owner/project.git",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
