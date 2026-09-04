from __future__ import annotations

import contextlib
import io
import json
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
import deployment_contract
import deploy
import prepare_workspace as pw
import project
import render_workflow
import task


class Step5SchedulerCutoverTests(unittest.TestCase):
    def profile(self, root: pathlib.Path, slug: str = "alpha",
                git_remote: str | None = None) -> pw.Profile:
        return pw.Profile(
            slug=slug, repository=f"owner/{slug}",
            git_remote=git_remote or f"git@example:{slug}.git",
            workspace_root=root / "work", state_root=root / "state" / slug,
            log_root=root / "logs" / slug, secret_reference="github.token",
            trusted_dispatchers=("duck-lint",), dispatch_labels=("symphony:auto",),
            blocked_label="symphony:human", service_identity=f"symphony-pilot-{slug}",
            dashboard_port=4400 if slug == "alpha" else 4401,
            max_concurrent_agents=1, max_turns=8, poll_interval_ms=1000,
            max_retry_backoff_ms=1000, codex_model="model",
            codex_reasoning_effort="high", toolchain=None,
        )

    @staticmethod
    def git(cwd: pathlib.Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=cwd, text=True, capture_output=True, check=False,
        )
        if result.returncode:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
        return result.stdout.strip()

    def local_remote(self, root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, str]:
        source = root / "source"
        remote = root / "remote.git"
        source.mkdir()
        self.git(source, "init", "--initial-branch=main")
        self.git(source, "config", "user.email", "tests@example.invalid")
        self.git(source, "config", "user.name", "Step 5 tests")
        (source / "README.md").write_text("A\n", encoding="utf-8")
        self.git(source, "add", "README.md")
        self.git(source, "commit", "-m", "A")
        base_sha = self.git(source, "rev-parse", "HEAD")
        self.git(root, "clone", "--bare", str(source), str(remote))
        return source, remote, base_sha

    def task_workspace(self, root: pathlib.Path, remote: pathlib.Path, base_sha: str,
                       *, current_head: str | None = None) -> tuple[pw.Profile, pathlib.Path, dict]:
        profile = self.profile(root, git_remote=str(remote))
        database_path = root / "state" / "control.sqlite3"
        with control_db.open_database(database_path) as database:
            record = database.create_task(
                project_slug=profile.slug, title="Git preparation", objective="Exercise preparation",
                base_ref="main", base_sha=base_sha, current_head=current_head,
            )
        workspace = profile.workspace_root / str(record["identifier"])
        workspace.parent.mkdir(parents=True, exist_ok=True)
        self.git(root, "clone", str(remote), str(workspace))
        return profile, workspace, record

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
        self.assertNotIn('env["SYMPHONY_PILOT_GITHUB_TOKEN"]', source)
        self.assertNotIn("dispatchable issues exceed", source)

    def test_after_run_reports_step6_boundary(self):
        import after_run

        with mock.patch.object(after_run, "load_profile"):
            with mock.patch.object(after_run, "print") as output:
                result = after_run.main(["--profile", "profile.toml", "--workspace", "T-000001"])
        self.assertEqual(result, 78)
        output.assert_called_once()
        self.assertIn("Step 6", output.call_args.args[0])
        after_run_source = (ROOT / "runtime/after_run.py").read_text(encoding="utf-8")
        architecture = (ROOT / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
        findings = (ROOT / "docs/SECURITY_FINDINGS.md").read_text(encoding="utf-8")
        self.assertIn("best-effort", after_run_source)
        self.assertIn("not an activation barrier", " ".join(architecture.split()))
        self.assertIn("STEP-6-BEFORE-ACTIVATION", findings)
        self.assertIn("not a substitute for this ordering", findings)
        self.assertNotIn("after_run hook fails\nclosed", architecture)

    def test_deployment_generator_and_verifier_share_exact_runtime_inventory(self):
        def clean_git_result(args, **kwargs):
            stdout = "a" * 40 + "\n" if args[1:3] == ["rev-parse", "HEAD"] else ""
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                deploy.subprocess, "run", side_effect=clean_git_result):
            target = deploy.deploy(
                ROOT / "projects/symphony-canary/profile.toml",
                pathlib.Path(directory) / "deployment",
                False,
            )
            profile = pw.load_profile(ROOT / "projects/symphony-canary/profile.toml")
            manifest = json.loads((target / "DEPLOYMENT.json").read_text(encoding="utf-8"))
            runtime_files = {
                relative for relative in manifest["files"] if relative.startswith("runtime/")
            }
            self.assertEqual(runtime_files, set(deployment_contract.DEPLOYED_RUNTIME_FILES))
            with mock.patch.object(project, "install_root", return_value=target):
                project.verify_deployment(profile)
            self.assertFalse((target / "runtime/broker.py").exists())

    def test_operator_contract_digest_changes_when_task_intake_changes(self):
        self.assertIn("scripts/task.py", deployment_contract.CONTRACT_FILES)
        task_path = (ROOT / "scripts/task.py").resolve()
        original_read_bytes = pathlib.Path.read_bytes

        def changed_task_bytes(path):
            data = original_read_bytes(path)
            return data + b"\n# synthetic intake change\n" if path.resolve() == task_path else data

        before = deployment_contract.contract_digest(ROOT)
        with mock.patch.object(pathlib.Path, "read_bytes", changed_task_bytes):
            after = deployment_contract.contract_digest(ROOT)
        self.assertNotEqual(before, after)

        def clean_git_result(args, **kwargs):
            stdout = "a" * 40 + "\n" if args[1:3] == ["rev-parse", "HEAD"] else ""
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                deploy.subprocess, "run", side_effect=clean_git_result):
            target = deploy.deploy(
                ROOT / "projects/symphony-canary/profile.toml",
                pathlib.Path(directory) / "deployment",
                False,
            )
            profile = pw.load_profile(ROOT / "projects/symphony-canary/profile.toml")
            with mock.patch.object(project, "install_root", return_value=target):
                project.verify_deployment(profile)
                with mock.patch.object(pathlib.Path, "read_bytes", changed_task_bytes):
                    with self.assertRaises(pw.PreparationError) as raised:
                        project.verify_deployment(profile)
        self.assertIn("operator/runtime contract differs", str(raised.exception))

    def test_runtime_environment_scrubs_all_retired_tracker_credentials(self):
        seeded = {
            "SYMPHONY_PILOT_GITHUB_TOKEN": "pilot",
            "GITHUB_TOKEN": "github",
            "GH_TOKEN": "gh",
        }
        with mock.patch.dict(project.os.environ, seeded, clear=True):
            environment = project.runtime_environment(
                pathlib.Path("/deployment"), pathlib.Path("/workflow")
            )
        for name in seeded:
            self.assertNotIn(name, environment)
        self.assertEqual(
            environment["SYMPHONY_PROFILE"],
            str(pathlib.Path("/deployment") / "profile.toml"),
        )

    def test_task_cli_missing_identifier_and_uuid_are_bounded(self):
        profile = self.profile(pathlib.Path("/tmp"))
        with tempfile.TemporaryDirectory() as directory:
            database_path = pathlib.Path(directory) / "control.sqlite3"
            for selector in ("T-999999", "11111111-1111-1111-1111-111111111111"):
                stderr = io.StringIO()
                with (
                    mock.patch.object(task, "_profile", return_value=profile),
                    mock.patch.object(task, "default_database_path", return_value=database_path),
                    contextlib.redirect_stderr(stderr),
                ):
                    result = task.main(["show", "--project", "alpha", "--task", selector])
                self.assertEqual(result, 78)
                self.assertIn("symphony-pilot task command stopped:", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_initial_task_starts_at_unchanged_recorded_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _, remote, base_sha = self.local_remote(root)
            profile, workspace, record = self.task_workspace(root, remote, base_sha)
            pw.prepare(profile, workspace)
            self.assertEqual(self.git(workspace, "rev-parse", "HEAD"), base_sha)
            self.assertEqual(self.git(workspace, "branch", "--show-current"), record["branch"])

    def test_initial_task_tolerates_default_branch_fast_forward(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source, remote, base_sha = self.local_remote(root)
            profile, workspace, _ = self.task_workspace(root, remote, base_sha)
            (source / "README.md").write_text("A\nB\n", encoding="utf-8")
            self.git(source, "add", "README.md")
            self.git(source, "commit", "-m", "B")
            tip = self.git(source, "rev-parse", "HEAD")
            self.git(source, "push", str(remote), "main")
            pw.prepare(profile, workspace)
            self.assertEqual(self.git(workspace, "rev-parse", "HEAD"), base_sha)
            self.assertNotEqual(base_sha, tip)

    def test_initial_task_rejects_base_history_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _, remote, base_sha = self.local_remote(root)
            replacement = root / "replacement"
            replacement.mkdir()
            self.git(replacement, "init", "--initial-branch=main")
            self.git(replacement, "config", "user.email", "tests@example.invalid")
            self.git(replacement, "config", "user.name", "Step 5 tests")
            (replacement / "REPLACED.md").write_text("C\n", encoding="utf-8")
            self.git(replacement, "add", "REPLACED.md")
            self.git(replacement, "commit", "-m", "C")
            self.git(replacement, "push", "--force", str(remote), "main")
            profile, workspace, _ = self.task_workspace(root, remote, base_sha)
            with self.assertRaises(pw.PreparationError) as raised:
                pw.prepare(profile, workspace)
            self.assertEqual(raised.exception.kind, "base_history_rewritten")

    def test_continuation_requires_exact_host_owned_head(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source, remote, base_sha = self.local_remote(root)
            (source / "README.md").write_text("A\nB\n", encoding="utf-8")
            self.git(source, "add", "README.md")
            self.git(source, "commit", "-m", "B")
            continuation_tip = self.git(source, "rev-parse", "HEAD")
            profile, workspace, record = self.task_workspace(
                root, remote, base_sha, current_head=base_sha,
            )
            self.git(source, "push", str(remote), f"HEAD:refs/heads/{record['branch']}")
            with self.assertRaises(pw.PreparationError) as raised:
                pw.prepare(profile, workspace)
            self.assertEqual(raised.exception.kind, "server_ref_changed")
            self.assertNotEqual(continuation_tip, base_sha)


if __name__ == "__main__":
    unittest.main(verbosity=2)
