from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "scripts"))

import control_db
import prepare_workspace as pw
import render_workflow
import task


class Step5SchedulerCutoverTests(unittest.TestCase):
    def profile(self, root: pathlib.Path, slug: str = "alpha") -> pw.Profile:
        return pw.Profile(
            slug=slug, repository=f"owner/{slug}", git_remote=f"git@example:{slug}.git",
            workspace_root=root / "work", state_root=root / "state" / slug,
            log_root=root / "logs" / slug, secret_reference="github.token",
            trusted_dispatchers=("duck-lint",), dispatch_labels=("symphony:auto",),
            blocked_label="symphony:human", service_identity=f"symphony-pilot-{slug}",
            dashboard_port=4400 if slug == "alpha" else 4401,
            max_concurrent_agents=1, max_turns=8, poll_interval_ms=1000,
            max_retry_backoff_ms=1000, codex_model="model",
            codex_reasoning_effort="high", toolchain=None,
        )

    def test_remote_head_uses_structured_argv_and_fails_closed_on_ambiguity(self):
        response = subprocess.CompletedProcess(
            ["git"], 0, "ref: refs/heads/main\tHEAD\n" + "a" * 40 + "\tHEAD\n", ""
        )
        with mock.patch.object(task.subprocess, "run", return_value=response) as run:
            self.assertEqual(task.resolve_remote_head("git@example:repo.git"), ("main", "a" * 40))
        self.assertEqual(run.call_args.args[0], ["git", "ls-remote", "--symref", "git@example:repo.git", "HEAD"])
        ambiguous = subprocess.CompletedProcess(["git"], 0, "ref: refs/heads/main\tHEAD\n", "")
        with mock.patch.object(task.subprocess, "run", return_value=ambiguous):
            with self.assertRaises(task.TaskCommandError):
                task.resolve_remote_head("git@example:repo.git")

    def test_create_queues_through_local_db_and_branch_is_not_caller_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            database_path = root / "control.sqlite3"
            profile = self.profile(root)
            with mock.patch.object(task, "_profile", return_value=profile), \
                 mock.patch.object(task, "resolve_remote_head", return_value=("main", "a" * 40)), \
                 mock.patch.object(task, "default_database_path", return_value=database_path):
                self.assertEqual(task.create(Namespace(project="alpha", title="Local task", objective="Do it")), 0)
                with control_db.open_database(database_path) as database:
                    created = database.read_task_by_identifier("T-000001", project_slug="alpha")
                    self.assertEqual(created["state"], "PREPARED")
                    self.assertEqual(created["branch"], "codex/t-000001-" + created["id"].replace("-", "")[:12])
                    with self.assertRaises(TypeError):
                        database.create_task(
                            project_slug="alpha", title="bad", objective="bad",
                            base_ref="main", base_sha="a" * 40, branch="operator-choice",
                        )
                self.assertEqual(task.queue(Namespace(project="alpha", task="T-000001")), 0)
                with self.assertRaises(control_db.StateConflict):
                    task.queue(Namespace(project="alpha", task="T-000001"))
                with control_db.open_database_readonly(database_path) as database:
                    events = database.list_events(created["id"])
                    self.assertEqual([event["event_type"] for event in events], ["task_created", "queued"])

    def test_local_task_lookup_checks_project_and_preparation_facts_use_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            database_path = root / "control.sqlite3"
            with control_db.open_database(database_path) as database:
                created = database.create_task(
                    project_slug="alpha", title="Local task", objective="Prepare it",
                    base_ref="main", base_sha="b" * 40,
                )
            profile = self.profile(root)
            workspace = profile.workspace_root / str(created["identifier"])
            with mock.patch.object(pw, "control_database_path", return_value=database_path):
                facts, record = pw.local_task_facts(profile, workspace)
            self.assertEqual(facts.identifier, "T-000001")
            self.assertEqual(facts.task_uuid, created["id"])
            self.assertEqual(facts.selected_head, "b" * 40)
            self.assertEqual(facts.branch, created["branch"])
            self.assertEqual(record["project_slug"], "alpha")
            with mock.patch.object(pw, "control_database_path", return_value=database_path):
                with self.assertRaises(pw.PreparationError):
                    pw.local_task_facts(self.profile(root, "beta"), workspace)

    def test_rendered_workflow_has_sqlite_scheduler_and_no_github_scheduler_surface(self):
        profile = self.profile(pathlib.Path("/home/operator"))
        with tempfile.TemporaryDirectory() as directory:
            policy = pathlib.Path(directory) / "policy.md"
            policy.write_text("policy\n", encoding="utf-8")
            rendered = render_workflow.render(profile, pathlib.Path(directory), policy)
        self.assertIn("kind: sqlite", rendered)
        self.assertIn("database_path: /home/operator/state/control.sqlite3", rendered)
        self.assertIn("project_slug: alpha", rendered)
        self.assertIn("- QUEUED", rendered)
        self.assertIn("- READY_FOR_HUMAN_MERGE", rendered)
        for forbidden in (
            "kind: github", "SYMPHONY_PILOT_GITHUB_TOKEN", "required_labels", "- open", "- closed",
            "admit_task.py", "GH-", "/issues?",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_scheduler_start_has_no_github_issue_workload_query_or_runtime_token(self):
        source = (ROOT / "scripts/project.py").read_text(encoding="utf-8")
        self.assertNotIn("def active_issues", source)
        self.assertNotIn("/issues?state=open", source)
        self.assertNotIn("SYMPHONY_PILOT_GITHUB_TOKEN", source)
        self.assertNotIn("dispatchable issues exceed", source)

    def test_after_run_is_an_explicit_step6_fail_closed_boundary(self):
        import after_run

        with mock.patch.object(after_run, "load_profile"):
            with mock.patch.object(after_run, "print") as output:
                result = after_run.main(["--profile", "profile.toml", "--workspace", "T-000001"])
        self.assertEqual(result, 78)
        output.assert_called_once()
        self.assertIn("Step 6", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
