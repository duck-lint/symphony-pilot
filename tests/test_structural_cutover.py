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
import broker
import dispatch_provenance
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

    def test_dispatch_provenance_uses_latest_server_event_and_trusted_actor(self):
        issue = {"state": "open", "labels": [{"name": "symphony:auto"}]}
        events = [
            {"id": 1, "event": "labeled", "label": {"name": "symphony:auto"},
             "actor": {"login": "duck-lint"}, "created_at": "2026-01-01T00:00:00Z"},
            {"id": 2, "event": "unlabeled", "label": {"name": "symphony:auto"},
             "actor": {"login": "attacker"}, "created_at": "2026-01-01T01:00:00Z"},
        ]
        with self.assertRaises(dispatch_provenance.DispatchProvenanceError):
            dispatch_provenance.prove_dispatch(issue, events, ("symphony:auto",), ("duck-lint",))
        events[1] = {"id": 3, "event": "labeled", "label": {"name": "symphony:auto"},
                     "actor": {"login": "duck-lint"}, "created_at": "2026-01-01T02:00:00Z"}
        proven = dispatch_provenance.prove_dispatch(issue, events, ("symphony:auto",), ("duck-lint",))
        self.assertEqual(proven[0]["event_id"], 3)

    def test_dispatch_event_fetch_is_complete_and_paginated(self):
        pages = {1: [{"id": index} for index in range(100)], 2: [{"id": 101}]}
        seen = []
        result = dispatch_provenance.fetch_all_events(lambda page: seen.append(page) or pages[page])
        self.assertEqual(seen, [1, 2])
        self.assertEqual(len(result), 101)

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
        good = {"target": "branch", "enforcement": "active",
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"]}},
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

    def test_host_broker_maps_human_block_without_task_supplied_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            profile = mock.Mock(state_root=root, dispatch_labels=("symphony:auto",), blocked_label="symphony:human")
            profile.slug = "demo"
            task = self.task()
            task_path = root / "tasks" / "GH-10" / "task.json"
            task_admission.write_task(task_path, task)
            outbox_path = outbox.task_outbox_path(task_path)
            outbox_path.parent.mkdir()
            outbox_path.write_text(json.dumps({
                "schema": outbox.OUTBOX_SCHEMA, "task_id": task["task_id"], "head": None,
                "workpad_body": "blocked", "disposition": "human_blocked", "summary": "needs human",
            }), encoding="utf-8")
            with mock.patch.object(broker, "read_secret", return_value="token"), \
                 mock.patch.object(broker, "github") as api:
                self.assertEqual(broker.process_result(profile, root / "GH-10"), 0)
        paths = [call.args[3] for call in api.call_args_list]
        self.assertIn("/issues/comments/42", paths)
        self.assertIn("/issues/10/labels/symphony%3Aauto", paths)
        self.assertIn("/issues/10/labels", paths)

    def test_host_broker_reconciles_the_exact_draft_pr(self):
        profile = mock.Mock(repository="example/project")
        task = self.task()
        matching = {"number": 7, "draft": True,
                    "head": {"ref": task["issue_branch"], "repo": {"full_name": profile.repository}},
                    "base": {"ref": task["default_ref"]}}
        with mock.patch.object(broker, "github", side_effect=[[matching], {}]) as api:
            self.assertEqual(broker.draft_pr(profile, "token", task, published_head="d" * 40), {
                "number": 7, "base_ref": "master", "head_ref": task["issue_branch"]})
        self.assertEqual(api.call_args_list[1].args[3], "/pulls/7")
        self.assertIn("d" * 40, api.call_args_list[1].args[4]["body"])

    @unittest.skipIf(os.name == "nt", "publication deploy-key mode is a native WSL contract")
    def test_publication_imports_fixed_bundle_into_sterile_repo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            remote = root / "remote.git"
            source.mkdir()
            self.git(source, "init", "-b", "master")
            self.git(source, "config", "user.email", "fixture@example.test")
            self.git(source, "config", "user.name", "fixture")
            (source / "README").write_text("base\n", encoding="utf-8")
            self.git(source, "add", "README")
            self.git(source, "commit", "-m", "base")
            base = self.git(source, "rev-parse", "HEAD")
            self.git(root, "clone", "--bare", str(source), str(remote))
            self.git(source, "switch", "-c", "task")
            (source / "README").write_text("task\n", encoding="utf-8")
            self.git(source, "commit", "-am", "task")
            head = self.git(source, "rev-parse", "HEAD")
            bundle = root / "publication.bundle"
            self.git(source, "bundle", "create", str(bundle), "HEAD")
            task = dict(self.task(), base_sha=base)
            profile = mock.Mock(git_remote=str(remote))
            key = root / "publication-ssh-key"
            key.write_text("synthetic-key", encoding="utf-8")
            key.chmod(0o600)
            with mock.patch.object(publication, "publication_key_path", return_value=key):
                self.assertEqual(publication.publish_bundle(profile, task, bundle, head), head)
            self.assertEqual(self.git(remote, "rev-parse", task["issue_branch"]), head)


if __name__ == "__main__":
    unittest.main(verbosity=2)
