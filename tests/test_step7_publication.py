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
import provision_publication_key
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
            finalized = database.finalize_publication(
                self.task["id"], head_sha=self.head, remote_branch=self.task_branch,
                github_pr_number=7, evidence={"ruleset_id": 1, "ruleset_fingerprint": "a" * 64,
                                              "deploy_key_id": 2, "deploy_key_fingerprint": "SHA256:x"},
            )
            self.assertEqual(finalized["task"]["state"], "READY_FOR_HUMAN_MERGE")
            self.assertEqual(finalized["task"]["published_head"], self.head)
            self.assertEqual(database.read_publication(self.task["id"])["publication_status"], "published")
            events = [event["event_type"] for event in database.list_events(self.task["id"])]
            self.assertIn("publication_finished", events)
            self.assertIn("ready_for_human_merge", events)

    def test_fail_publication_cannot_downgrade_successful_finalization(self):
        with control_db.open_database(self.database_path) as database:
            database.start_publication(self.task["id"], head_sha=self.head, remote_branch=self.task_branch)
            finalized = database.finalize_publication(
                self.task["id"], head_sha=self.head, remote_branch=self.task_branch,
                github_pr_number=7, evidence={"ruleset_id": 1, "ruleset_fingerprint": "a" * 64,
                                              "deploy_key_id": 2, "deploy_key_fingerprint": "SHA256:x"},
            )
            with self.assertRaises(control_db.StateConflict):
                database.fail_publication(
                    self.task["id"], detail="late observation failure", head_sha=self.head,
                    remote_branch=self.task_branch, github_pr_number=7,
                )
            self.assertEqual(finalized["task"]["state"], "READY_FOR_HUMAN_MERGE")
            self.assertEqual(database.read_task(self.task["id"])["published_head"], self.head)
            self.assertEqual(database.read_publication(self.task["id"])["publication_status"], "published")
            events = [event["event_type"] for event in database.list_events(self.task["id"])]
            self.assertEqual(events.count("publication_finished"), 1)
            self.assertEqual(events.count("ready_for_human_merge"), 1)

    def test_local_bare_remote_exact_publication(self):
        snapshots = iter(({"ruleset_id": 9, "fingerprint": "a" * 64},
                          {"ruleset_id": 9, "fingerprint": "a" * 64}))

        @contextlib.contextmanager
        def key(*_args):
            with tempfile.NamedTemporaryFile() as stream:
                yield pathlib.Path(stream.name), {"id": 4, "fingerprint": "SHA256:key"}

        pr = {"number": 7}
        exact_pr = {"number": 7, "state": "open", "draft": True,
                    "head": {"ref": self.task_branch, "sha": self.head,
                             "repo": {"full_name": self.profile.repository}},
                    "base": {"ref": "master"}}
        def api(_profile, _token, method, path, body=None):
            self.assertEqual((method, path), ("GET", "/pulls/7"))
            return exact_pr
        with mock.patch.object(publication, "canonical_publication_remote", return_value=str(self.remote)), \
             mock.patch.object(publication, "verified_private_key", side_effect=key), \
             mock.patch.object(publication, "read_secret", return_value="api-token"), \
             mock.patch.object(publication, "reconcile_pull_request", return_value=pr), \
             mock.patch.object(publication, "_ruleset_snapshot", side_effect=lambda *args: next(snapshots)):
            result = publication.publish_task(
                self.profile, self.task["id"], database_path=self.database_path,
                deployment_check=lambda profile: None,
                github_call=api,
            )
        self.assertEqual(result["task"]["state"], "READY_FOR_HUMAN_MERGE")
        self.assertEqual(self.git(self.remote, "rev-parse", self.task_branch), self.head)

    def test_failure_after_finalization_cannot_downgrade_or_block_retry(self):
        snapshots = iter(({"ruleset_id": 9, "fingerprint": "a" * 64},
                          {"ruleset_id": 9, "fingerprint": "a" * 64}))
        exact_pr = {"number": 7, "state": "open", "draft": True,
                    "head": {"ref": self.task_branch, "sha": self.head,
                             "repo": {"full_name": self.profile.repository}},
                    "base": {"ref": "master"}}

        @contextlib.contextmanager
        def key(*_args):
            with tempfile.NamedTemporaryFile() as stream:
                yield pathlib.Path(stream.name), {"id": 4, "fingerprint": "SHA256:key"}

        def api(_profile, _token, method, path, body=None):
            self.assertEqual((method, path), ("GET", "/pulls/7"))
            return exact_pr

        original = control_db.ControlPlaneDatabase.finalize_publication
        def commit_then_raise(database, *args, **kwargs):
            original(database, *args, **kwargs)
            raise RuntimeError("response observation failed after commit")

        with mock.patch.object(publication, "canonical_publication_remote", return_value=str(self.remote)), \
             mock.patch.object(publication, "verified_private_key", side_effect=key), \
             mock.patch.object(publication, "read_secret", return_value="token"), \
             mock.patch.object(publication, "reconcile_pull_request", return_value={"number": 7}) as reconcile, \
             mock.patch.object(publication, "_ruleset_snapshot", side_effect=lambda *args: next(snapshots)), \
             mock.patch.object(control_db.ControlPlaneDatabase, "finalize_publication", new=commit_then_raise):
            with self.assertRaises(publication.PublicationError) as caught:
                publication.publish_task(
                    self.profile, self.task["id"], database_path=self.database_path,
                    github_call=api,
                )

        with control_db.ControlPlaneDatabase.open_readonly(self.database_path) as database:
            task = database.read_task(self.task["id"])
            publication_row = database.read_publication(self.task["id"])
            self.assertEqual(publication_row["publication_status"], "published", str(caught.exception))
            self.assertEqual(task["state"], "READY_FOR_HUMAN_MERGE")
            self.assertEqual(task["published_head"], self.head)
            self.assertEqual(database.read_publication(self.task["id"])["publication_status"], "published")
            events = [event["event_type"] for event in database.list_events(self.task["id"])]
            self.assertEqual(events.count("publication_finished"), 1)
            self.assertEqual(events.count("ready_for_human_merge"), 1)
            self.assertFalse(database.connection.execute(
                "SELECT 1 FROM blockers WHERE task_id = ? AND status = 'open'",
                (self.task["id"],),
            ).fetchone())

        with mock.patch.object(publication, "_publish_branch") as push:
            result = publication.publish_task(self.profile, self.task["id"], database_path=self.database_path)
        self.assertTrue(result["idempotent"])
        push.assert_not_called()
        reconcile.assert_called_once()

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

    def test_nonempty_ruleset_exclusions_are_rejected_as_patterns(self):
        for exclusion in ("refs/heads/*", "refs/heads/**", "*", "~ALL", "refs/**"):
            ruleset = {
                "id": 1, "target": "branch", "enforcement": "active",
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": [exclusion]}},
                "bypass_actors": [], "rules": [{"type": "pull_request"}],
            }
            with self.subTest(exclusion=exclusion), self.assertRaises(rulesets.RulesetError):
                rulesets.require_default_branch_ruleset([ruleset], "master")

    def test_initial_branch_publish_uses_absence_compare_and_swap(self):
        directory, bundle = publication._generate_bundle(self.workspace)
        try:
            with tempfile.NamedTemporaryFile() as key:
                def race(_remote, _branch, _env):
                    self.git(self.remote, "update-ref", "refs/heads/" + self.task_branch, self.base)
                    return None
                with mock.patch.object(publication, "_remote_head", side_effect=race):
                    with self.assertRaises(publication.PublicationError):
                        publication._publish_branch(
                            self.profile, {"base_ref": "master", "base_sha": self.base,
                                           "branch": self.task_branch, "current_head": self.head},
                            bundle, pathlib.Path(key.name), str(self.remote),
                            allow_existing_remote=False,
                        )
        finally:
            directory.cleanup()
        self.assertEqual(self.git(self.remote, "rev-parse", self.task_branch), self.base)

    def test_publication_order_verifies_key_then_snapshot_then_push_then_rechecks(self):
        order = []
        exact_pr = {"number": 7, "state": "open", "draft": True,
                    "head": {"ref": self.task_branch, "sha": self.head,
                             "repo": {"full_name": self.profile.repository}},
                    "base": {"ref": "master"}}

        @contextlib.contextmanager
        def key(*_args):
            with tempfile.NamedTemporaryFile() as stream:
                order.append("deploy-key")
                yield pathlib.Path(stream.name), {"id": 4, "fingerprint": "SHA256:key"}

        def snapshot(*_args):
            order.append("snapshot")
            return {"ruleset_id": 9, "fingerprint": "a" * 64}

        def push(*_args, **_kwargs):
            order.append("push")

        def remote(*_args, **_kwargs):
            order.append("remote")
            return self.head

        def api(_profile, _token, method, path, body=None):
            self.assertEqual((method, path), ("GET", "/pulls/7"))
            order.append("pr")
            return exact_pr

        with mock.patch.object(publication, "canonical_publication_remote", return_value="local"), \
             mock.patch.object(publication, "verified_private_key", side_effect=key), \
             mock.patch.object(publication, "read_secret", return_value="token"), \
             mock.patch.object(publication, "_generate_bundle", return_value=(tempfile.TemporaryDirectory(), self.root / "unused.bundle")), \
             mock.patch.object(publication, "_publish_branch", side_effect=push), \
             mock.patch.object(publication, "_remote_head", side_effect=remote), \
             mock.patch.object(publication, "reconcile_pull_request", return_value={"number": 7}):
            # The mocked bundle path is not read because _publish_branch is mocked.
            result = publication.publish_task(
                self.profile, self.task["id"], database_path=self.database_path,
                github_call=api, snapshot_fn=snapshot,
            )
        self.assertEqual(result["task"]["state"], "READY_FOR_HUMAN_MERGE")
        self.assertEqual(order, ["deploy-key", "snapshot", "push", "remote", "pr", "snapshot"])

    def test_final_pr_mutations_fail_closed_and_do_not_ready(self):
        mutations = {
            "non-draft": {"draft": False},
            "closed": {"state": "closed"},
            "wrong-base": {"base": {"ref": "other"}},
            "wrong-head": {"head": {"ref": self.task_branch, "sha": "0" * 40,
                                       "repo": {"full_name": self.profile.repository}}},
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                database_path = self.root / (label + ".sqlite3")
                # Copy the fixture database so each mutation is independent.
                with control_db.open_database(database_path) as destination, \
                     control_db.ControlPlaneDatabase.open_readonly(self.database_path) as source:
                    source.connection.backup(destination.connection)
                exact = {"number": 7, "state": "open", "draft": True,
                         "head": {"ref": self.task_branch, "sha": self.head,
                                  "repo": {"full_name": self.profile.repository}},
                         "base": {"ref": "master"}}
                for key, value in mutation.items():
                    exact[key] = value
                @contextlib.contextmanager
                def key_context(*_args):
                    with tempfile.NamedTemporaryFile() as stream:
                        yield pathlib.Path(stream.name), {"id": 4, "fingerprint": "SHA256:key"}
                def api(_profile, _token, method, path, body=None):
                    return exact
                with mock.patch.object(publication, "canonical_publication_remote", return_value=str(self.remote)), \
                     mock.patch.object(publication, "verified_private_key", side_effect=key_context), \
                     mock.patch.object(publication, "read_secret", return_value="token"), \
                     mock.patch.object(publication, "reconcile_pull_request", return_value={"number": 7}), \
                     mock.patch.object(publication, "_ruleset_snapshot", return_value={"ruleset_id": 9, "fingerprint": "a" * 64}):
                    with self.assertRaises(publication.PublicationError):
                        publication.publish_task(self.profile, self.task["id"], database_path=database_path,
                                                 github_call=api)
                with control_db.ControlPlaneDatabase.open_readonly(database_path) as database:
                    self.assertEqual(database.read_task(self.task["id"])["state"], "ARCHIVIST")
                    self.assertTrue(database.connection.execute(
                        "SELECT 1 FROM blockers WHERE task_id = ? AND status = 'open'",
                        (self.task["id"],),
                    ).fetchone())

    def test_handled_failures_compensate_and_keyboard_interrupt_retains_started(self):
        database_path = self.root / "crash.sqlite3"
        with control_db.open_database(database_path) as destination, \
             control_db.ControlPlaneDatabase.open_readonly(self.database_path) as source:
            source.connection.backup(destination.connection)

        with mock.patch.object(publication, "read_secret", side_effect=RuntimeError("missing credential")):
            with self.assertRaises(publication.PublicationError):
                publication.publish_task(self.profile, self.task["id"], database_path=self.database_path)
        with control_db.ControlPlaneDatabase.open_readonly(self.database_path) as database:
            self.assertEqual(database.read_publication(self.task["id"])["publication_status"], "failed")
            self.assertTrue(database.connection.execute(
                "SELECT 1 FROM blockers WHERE task_id = ? AND status = 'open'",
                (self.task["id"],),
            ).fetchone())

        with mock.patch.object(publication, "read_secret", return_value="token"), \
             mock.patch.object(publication, "_generate_bundle", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                publication.publish_task(self.profile, self.task["id"], database_path=database_path)
        with control_db.ControlPlaneDatabase.open_readonly(database_path) as database:
            self.assertEqual(database.read_publication(self.task["id"])["publication_status"], "started")

    def test_operational_failures_after_intent_are_durable(self):
        def fresh_db(name):
            path = self.root / name
            with control_db.open_database(path) as destination, \
                 control_db.ControlPlaneDatabase.open_readonly(self.database_path) as source:
                source.connection.backup(destination.connection)
            return path

        failures = (
            ("snapshot", {"read_secret": mock.DEFAULT, "snapshot_fn": True}),
            ("key", {"read_secret": mock.DEFAULT, "key": True}),
            ("timeout", {"read_secret": mock.DEFAULT, "timeout": True}),
        )
        for name, options in failures:
            with self.subTest(name=name):
                path = fresh_db(name + ".sqlite3")
                patches = [mock.patch.object(publication, "read_secret", return_value="token")]
                if options.get("snapshot_fn"):
                    snapshot = lambda *_args: (_ for _ in ()).throw(OSError("GitHub transport"))
                    patches.append(mock.patch.object(publication, "verified_private_key", side_effect=lambda *_args: (_ for _ in ()).throw(OSError("key API"))))
                    # The key failure occurs before Snapshot A; cover Snapshot A
                    # separately with a verified key context below.
                    patches.pop()
                    @contextlib.contextmanager
                    def verified(*_args):
                        with tempfile.NamedTemporaryFile() as stream:
                            yield pathlib.Path(stream.name), {"id": 4, "fingerprint": "SHA256:key"}
                    patches.append(mock.patch.object(publication, "verified_private_key", side_effect=verified))
                    patches.append(mock.patch.object(publication, "_ruleset_snapshot", side_effect=snapshot))
                elif options.get("key"):
                    patches.append(mock.patch.object(publication, "verified_private_key", side_effect=OSError("key API")))
                else:
                    patches.append(mock.patch.object(
                        publication, "_generate_bundle",
                        side_effect=subprocess.TimeoutExpired("git", 1),
                    ))
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    with self.assertRaises(publication.PublicationError):
                        publication.publish_task(self.profile, self.task["id"], database_path=path)
                with control_db.ControlPlaneDatabase.open_readonly(path) as database:
                    self.assertEqual(database.read_publication(self.task["id"])["publication_status"], "failed")
                    self.assertTrue(database.connection.execute(
                        "SELECT 1 FROM blockers WHERE task_id = ? AND status = 'open'",
                        (self.task["id"],),
                    ).fetchone())

    def test_pre_intent_failure_records_blocker_without_publication_intent(self):
        with self.assertRaises(publication.PublicationError):
            publication.publish_task(
                self.profile, self.task["id"], database_path=self.database_path,
                deployment_check=lambda _profile: (_ for _ in ()).throw(RuntimeError("coherence")),
            )
        with control_db.ControlPlaneDatabase.open_readonly(self.database_path) as database:
            self.assertIsNone(database.read_publication(self.task["id"]))
            self.assertTrue(database.connection.execute(
                "SELECT 1 FROM blockers WHERE task_id = ? AND status = 'open'",
                (self.task["id"],),
            ).fetchone())

    def test_key_adoption_uses_the_stable_private_key_boundary(self):
        key_path = self.root / "publication-ssh-key"
        key_path.write_bytes(b"existing-key")
        stable_path = self.root / "stable-key"
        with stable_path.open("wb") as stream:
            stream.write(b"stable-key")
        @contextlib.contextmanager
        def stable(_profile):
            yield stable_path
        with mock.patch.object(provision_publication_key, "resolve_project", return_value=self.profile), \
             mock.patch.object(provision_publication_key, "publication_key_path", return_value=key_path), \
             mock.patch.object(provision_publication_key, "stable_private_key", side_effect=stable), \
             mock.patch.object(provision_publication_key, "public_key_from_private", return_value="ssh-ed25519 AAAA") as derive, \
             mock.patch.object(provision_publication_key, "public_key_fingerprint", return_value="SHA256:key"):
            self.assertEqual(provision_publication_key.main(["--project", "demo", "--adopt"]), 0)
        derive.assert_called_once_with(stable_path)

    @unittest.skipUnless(os.name != "nt", "mode and special-file probes require POSIX semantics")
    def test_key_boundary_rejects_unsafe_adoption_inputs(self):
        key_path = self.root / "publication-ssh-key"
        key_path.write_bytes(b"not-a-key")
        with mock.patch.object(publication_key, "publication_key_path", return_value=key_path):
            os.chmod(key_path, 0o644)
            with self.assertRaises(publication_key.PublicationKeyError):
                with publication_key.stable_private_key(self.profile):
                    pass
            os.chmod(key_path, 0o600)
            with publication_key.stable_private_key(self.profile) as stable:
                with self.assertRaises(publication_key.PublicationKeyError):
                    publication_key.public_key_from_private(stable)
            link_path = self.root / "symlink-key"
            link_path.symlink_to(key_path)
            with mock.patch.object(publication_key, "publication_key_path", return_value=link_path):
                with self.assertRaises(publication_key.PublicationKeyError):
                    with publication_key.stable_private_key(self.profile):
                        pass
            fifo_path = self.root / "fifo-key"
            os.mkfifo(fifo_path)
            with mock.patch.object(publication_key, "publication_key_path", return_value=fifo_path):
                with self.assertRaises(publication_key.PublicationKeyError):
                    with publication_key.stable_private_key(self.profile):
                        pass

    def test_start_does_not_read_publication_api_credential(self):
        import project
        with mock.patch.object(project, "verify_deployment", side_effect=project.PreparationError("x", "fixture")), \
             mock.patch.object(project, "read_secret", side_effect=AssertionError("startup read API credential")):
            self.assertNotEqual(project.start(self.profile), 78)


if __name__ == "__main__":
    unittest.main()
