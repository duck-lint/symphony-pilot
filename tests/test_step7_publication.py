from __future__ import annotations

import contextlib
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "scripts"))

import control_db
import publication
import publication_key
import rulesets
from prepare_workspace import Profile


class Step7PublicationTests(unittest.TestCase):
    def git(self, cwd: pathlib.Path, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.seed = self.root / "seed"
        self.remote = self.root / "remote.git"
        self.workspace_root = self.root / "work"
        self.workspace_root.mkdir()
        self.seed.mkdir()
        self.git(self.seed, "init", "-q", "-b", "master")
        self.git(self.seed, "config", "user.name", "Step 7")
        self.git(self.seed, "config", "user.email", "step7@example.invalid")
        (self.seed / "README").write_text("base\n", encoding="utf-8")
        self.git(self.seed, "add", "README")
        self.git(self.seed, "commit", "-qm", "base")
        self.base = self.git(self.seed, "rev-parse", "HEAD")
        self.git(self.root, "clone", "-q", "--bare", str(self.seed), str(self.remote))
        self.workspace = self.workspace_root / "T-000001"
        self.git(self.root, "clone", "-q", str(self.remote), str(self.workspace))
        self.git(self.workspace, "config", "user.name", "Step 7")
        self.git(self.workspace, "config", "user.email", "step7@example.invalid")
        self.task_branch = "codex/t-000001-111111111111"
        self.git(self.workspace, "switch", "-q", "-c", self.task_branch)
        (self.workspace / "README").write_text("published\n", encoding="utf-8")
        self.git(self.workspace, "commit", "-qam", "implementation")
        self.head = self.git(self.workspace, "rev-parse", "HEAD")
        self.database_path = self.root / "control.sqlite3"
        with control_db.open_database(self.database_path) as database:
            self.task = database.create_task(
                project_slug="demo", title="Publication", objective="Publish exact head",
                base_ref="master", base_sha=self.base, task_id="11111111-1111-1111-1111-111111111111",
                identifier="T-000001",
            )
            database.queue_task(self.task["id"], project_slug="demo")
            database.update_heads(self.task["id"], current_head=self.head)
            for old, new in (("QUEUED", "PLANNED"), ("PLANNED", "IMPLEMENTED"),
                             ("IMPLEMENTED", "REVIEW"), ("REVIEW", "ADVERSARIAL_REVIEW"),
                             ("ADVERSARIAL_REVIEW", "FINAL_MECHANICAL_ACCEPTANCE"),
                             ("FINAL_MECHANICAL_ACCEPTANCE", "ARCHIVIST")):
                database.transition_task(self.task["id"], expected_state=old, new_state=new,
                                         event_type="validation_passed" if new == "FINAL_MECHANICAL_ACCEPTANCE" else "task_created")
            database.record_event(self.task["id"], "review_accepted", {"head_sha": self.head})
            database.record_event(self.task["id"], "adversary_accepted", {"head_sha": self.head})
            database.record_event(self.task["id"], "validation_passed", {"head_sha": self.head})
            archivist = database.create_role_run(self.task["id"], "ARCHIVIST", 1, head_sha=self.head)
            database.finish_role_run(archivist["id"], head_sha=self.head)
        self.profile = Profile(
            slug="demo", repository="owner/demo", git_remote=str(self.remote),
            workspace_root=self.workspace_root, state_root=self.root / "state", log_root=self.root / "logs",
            secret_reference="github.token", trusted_dispatchers=("duck-lint",), dispatch_labels=("auto",),
            blocked_label="human", service_identity="symphony-pilot-demo", dashboard_port=4040,
            max_concurrent_agents=1, max_turns=1, poll_interval_ms=1000,
            max_retry_backoff_ms=1000, codex_model="model", codex_reasoning_effort="high", toolchain=None,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_atomic_finalization_requires_started_intent_and_writes_both_events(self):
        with control_db.open_database(self.database_path) as database:
            with self.assertRaises(control_db.StateConflict):
                database.finalize_publication(
                    self.task["id"], head_sha=self.head, remote_branch=self.task_branch,
                    github_pr_number=7, evidence={"ruleset_id": 1, "ruleset_fingerprint": "a" * 64,
                                                  "deploy_key_id": 2, "deploy_key_fingerprint": "SHA256:x"},
                )
            database.start_publication(self.task["id"], head_sha=self.head, remote_branch=self.task_branch)
            task = database.finalize_publication(
                self.task["id"], head_sha=self.head, remote_branch=self.task_branch,
                github_pr_number=7, evidence={"ruleset_id": 1, "ruleset_fingerprint": "a" * 64,
                                              "deploy_key_id": 2, "deploy_key_fingerprint": "SHA256:x"},
            )
            self.assertEqual(task["state"], "READY_FOR_HUMAN_MERGE")
            self.assertEqual(task["published_head"], self.head)
            self.assertEqual(database.read_publication(self.task["id"])["publication_status"], "published")
            events = [event["event_type"] for event in database.list_events(self.task["id"])]
            self.assertIn("publication_finished", events)
            self.assertIn("ready_for_human_merge", events)

    def test_local_bare_remote_exact_publication(self):
        snapshots = iter(({"ruleset_id": 9, "fingerprint": "a" * 64},
                          {"ruleset_id": 9, "fingerprint": "a" * 64}))

        @contextlib.contextmanager
        def key(*_args):
            with tempfile.NamedTemporaryFile() as stream:
                yield pathlib.Path(stream.name), {"id": 4, "fingerprint": "SHA256:key"}

        pr = {"number": 7}
        with mock.patch.object(publication, "canonical_publication_remote", return_value=str(self.remote)), \
             mock.patch.object(publication, "verified_private_key", side_effect=key), \
             mock.patch.object(publication, "read_secret", return_value="api-token"), \
             mock.patch.object(publication, "reconcile_pull_request", return_value=pr), \
             mock.patch.object(publication, "_ruleset_snapshot", side_effect=lambda *args: next(snapshots)):
            result = publication.publish_task(
                self.profile, self.task["id"], database_path=self.database_path,
                deployment_check=lambda profile: None,
            )
        self.assertEqual(result["task"]["state"], "READY_FOR_HUMAN_MERGE")
        self.assertEqual(self.git(self.remote, "rev-parse", self.task_branch), self.head)

    def test_pull_request_contract_rejects_non_draft_and_duplicate(self):
        task = {"identifier": "T-000001", "title": "Task", "branch": self.task_branch,
                "base_ref": "master", "base_sha": self.base, "current_head": self.head}
        profile = self.profile
        def api(_profile, _token, method, path, body=None):
            if method == "GET" and path.startswith("/pulls?"):
                return [{"number": 4, "state": "open", "draft": False,
                         "head": {"ref": self.task_branch, "sha": self.head, "repo": {"full_name": profile.repository}},
                         "base": {"ref": "master"}}]
            if method == "GET" and path == "/pulls/4":
                return {"number": 4, "state": "open", "draft": False,
                        "head": {"ref": self.task_branch, "sha": self.head, "repo": {"full_name": profile.repository}},
                        "base": {"ref": "master"}}
            raise AssertionError((method, path, body))
        with self.assertRaises(publication.PublicationError):
            publication.reconcile_pull_request(profile, task, "token", api)

    def test_ruleset_fingerprint_changes_only_security_relevant_fields(self):
        first = {"id": 1, "target": "branch", "enforcement": "active",
                 "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                 "bypass_actors": [], "rules": [{"type": "pull_request", "required_approving_review_count": 1}],
                 "irrelevant": "a"}
        second = dict(first, irrelevant="b")
        self.assertEqual(rulesets.security_fingerprint(first), rulesets.security_fingerprint(second))
        self.assertEqual(rulesets.require_default_branch_ruleset([first], "master"), first)
        excluded = dict(first, conditions={"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": ["~DEFAULT_BRANCH"]}})
        with self.assertRaises(rulesets.RulesetError):
            rulesets.require_default_branch_ruleset([excluded], "master")

    def test_start_does_not_read_publication_api_credential(self):
        import project
        with mock.patch.object(project, "verify_deployment", side_effect=project.PreparationError("x", "fixture")), \
             mock.patch.object(project, "read_secret", side_effect=AssertionError("startup read API credential")):
            self.assertNotEqual(project.start(self.profile), 78)


if __name__ == "__main__":
    unittest.main()
