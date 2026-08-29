"""Focused infrastructure regression tests for the reusable pilot runtime."""
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
import prepare_workspace as pw
sys.path.insert(0, str(ROOT / "scripts"))
import project as project_control

def make_profile(root: pathlib.Path) -> pw.Profile:
    return pw.Profile("demo", "example/project", "git@example:project.git",
        root / "work", root / "state", root / "logs", "github.token",
        ("symphony:auto",), "symphony:human", "demo", 4040, 1, 8, 5000,
        300000, "gpt-5.6-luna", "high", None, root / "deployment")

def git_repo(path: pathlib.Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    (path / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)

class InfrastructureTests(unittest.TestCase):
    def test_idle_stop_is_allowed_after_runtime_state_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(pathlib.Path(directory))
            pid_path, _ = project_control.state_paths(profile)
            pid_path.write_text("123\n", encoding="ascii")
            with mock.patch.object(project_control, "pid_alive", side_effect=[True, False, False]), \
                    mock.patch.object(project_control, "runtime_state", return_value={
                        "running": [], "retrying": []}), \
                    mock.patch.object(project_control.os, "kill") as kill:
                self.assertEqual(project_control.stop(profile), 0)
            kill.assert_called_once_with(123, project_control.signal.SIGTERM)
            self.assertFalse(pid_path.exists())

    def test_active_stop_remains_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(pathlib.Path(directory))
            pid_path, _ = project_control.state_paths(profile)
            pid_path.write_text("123\n", encoding="ascii")
            with mock.patch.object(project_control, "pid_alive", return_value=True), \
                    mock.patch.object(project_control, "runtime_state", return_value={
                        "running": [{"issue_identifier": "GH-7"}], "retrying": []}), \
                    mock.patch.object(project_control.os, "kill") as kill:
                self.assertEqual(project_control.stop(profile), 2)
            kill.assert_not_called()
            self.assertTrue(pid_path.exists())

    def test_profile_rejects_credential_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "bad.toml"
            path.write_text('slug="demo"\nrepository="a/b"\ngit_remote="x"\nworkspace_root="/home/x"\n'
                'state_root="/tmp/x"\nlog_root="/tmp/x"\nsecret_reference="token"\n'
                'dispatch_labels=["auto"]\nblocked_label="human"\nservice_identity="x"\n'
                'max_concurrent_agents=1\nmax_turns=1\npoll_interval_ms=1000\n'
                'max_retry_backoff_ms=1\ncodex_model="x"\ncodex_reasoning_effort="high"\n'
                'token=""\n', encoding="utf-8")
            with self.assertRaises(pw.PreparationError):
                pw.load_profile(path)

    def test_secret_reference_cannot_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            p = make_profile(pathlib.Path(directory))
            bad = p.__class__(**{**p.__dict__, "secret_reference": "../token"})
            with self.assertRaises(pw.PreparationError):
                pw.secret_path(bad)

    def test_dirty_tree_matches_commit_and_unique_delta_differs(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            git_repo(repo)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True,
                capture_output=True, check=True).stdout.strip()
            self.assertTrue(pw.dirty_equals_remote(repo, sha))
            (repo / "tracked.txt").write_text("unique\n", encoding="utf-8")
            self.assertFalse(pw.dirty_equals_remote(repo, sha))

    def test_marker_records_continuation_without_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "work" / "GH-7"
            repo.mkdir(parents=True)
            git_repo(repo)
            p = make_profile(root)
            facts = pw.IssueFacts(7, "codex/gh-7-work", "a"*40, "origin/main", "a"*40,
                "main", "a"*40, "continuation", "a"*40, 12, [])
            pw.marker(p, repo, facts, {"kind": None, "commands": {}, "target_directory": None})
            data = json.loads((repo / ".git/symphony-preparation.json").read_text())
            self.assertEqual(data["mode"], "continuation")
            self.assertNotIn("token", json.dumps(data).lower())

    def test_workpad_compaction_removes_duplicates_and_bounds_size(self):
        body = "<!-- symphony-workpad:v1 -->\n## Symphony Workpad\n\n## Evidence\nsame\n## Evidence\nsame\n"
        compact = pw.compact_workpad(body, limit=1000)
        bounded = pw.compact_workpad(body + "x"*30000, limit=1000)
        self.assertLessEqual(len(compact), 1000)
        self.assertEqual(compact.count("same"), 1)
        self.assertLessEqual(len(bounded), 1000)

    def test_blocker_fingerprint_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "work" / "GH-4"
            repo.mkdir(parents=True)
            git_repo(repo)
            p = make_profile(root)
            facts = pw.IssueFacts(4, "branch", "b"*40, None, None, None, None, "initial", None, None, [])
            self.assertEqual(pw.blocker_fingerprint(p, repo, facts, "toolchain", "missing"),
                pw.blocker_fingerprint(p, repo, facts, "toolchain", "missing"))

    def test_continuation_uses_remote_issue_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "work" / "GH-8"
            repo.mkdir(parents=True)
            p = make_profile(root)
            old_github, old_git = pw.github, pw.git
            try:
                def fake_github(_p, _t, _m, path, _body=None):
                    if path.startswith("/issues/8") and "comments" not in path and "labels" not in path:
                        return {"body": "required starting commit: " + "a"*40}
                    if path.startswith("/pulls"):
                        return []
                    return []
                pw.github = fake_github
                pw.git = lambda *_args, **_kwargs: "b"*40
                facts = pw.issue_facts(p, repo, "secret")
                self.assertEqual((facts.mode, facts.target_sha), ("continuation", "b"*40))
            finally:
                pw.github, pw.git = old_github, old_git

    def test_launcher_unsets_tracker_variables(self):
        launcher = (ROOT / "runtime/launch_codex.sh").read_text(encoding="utf-8")
        self.assertIn("unset SYMPHONY_PILOT_GITHUB_TOKEN", launcher)
        self.assertIn("app-server", launcher)

    def test_schema_has_no_credential_property(self):
        schema = json.loads((ROOT / "schemas/project-profile.schema.json").read_text())
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("token", schema["properties"])

    def test_recovery_manifest_excludes_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "work" / "GH-9"
            repo.mkdir(parents=True)
            git_repo(repo)
            (repo / "dirty.txt").write_text("recover\n", encoding="utf-8")
            p = make_profile(root)
            facts = pw.IssueFacts(9, "branch", "c"*40, None, None, None, None, "initial", None, None, [])
            archive = pw.archive_recovery(p, repo, facts, "?? dirty.txt")
            import tarfile
            with tarfile.open(archive) as tar:
                names = tar.getnames()
            self.assertIn("RECOVERY-MANIFEST.json", names)
            self.assertFalse(any(name.startswith(".git") for name in names))

    def test_initial_facts_use_licensed_sha_when_remote_branch_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "work" / "GH-10"
            repo.mkdir(parents=True)
            p = make_profile(root)
            old_github, old_git = pw.github, pw.git
            try:
                def fake_github(_p, _t, _m, path, _body=None):
                    if path == "/issues/10":
                        return {"body": "required starting commit: " + "d"*40}
                    if path.startswith("/pulls"):
                        return []
                    return []
                pw.github = fake_github
                pw.git = lambda *_args, **_kwargs: ""
                facts = pw.issue_facts(p, repo, "secret")
                self.assertEqual((facts.mode, facts.target_sha), ("initial", "d"*40))
            finally:
                pw.github, pw.git = old_github, old_git

    def test_repository_identity_rejects_stale_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            git_repo(root)
            subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/other.git"], cwd=root, check=True)
            with self.assertRaises(pw.PreparationError):
                pw.verify_repository(make_profile(root), root)

    def test_profile_enforces_one_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / "bad.toml"
            path.write_text((ROOT / "projects/cleanroom/profile.toml").read_text().replace(
                'slug = "cleanroom"', 'slug = "demo"').replace(
                'max_concurrent_agents = 1', 'max_concurrent_agents = 2').replace(
                'workspace_root = "/home/duck-lint/symphony-workspaces/cleanroom"',
                'workspace_root = "/home/duck-lint/demo"'), encoding="utf-8")
            with self.assertRaises(pw.PreparationError):
                pw.load_profile(path)

    def test_project_secret_paths_are_isolated(self):
        one = make_profile(pathlib.Path("/tmp/one"))
        two = one.__class__(**{**one.__dict__, "slug": "other"})
        self.assertNotEqual(str(pw.secret_path(one)), str(pw.secret_path(two)))

    def test_generated_workflow_has_official_boundary_fields(self):
        from render_workflow import render
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            policy = root / "policy.md"
            policy.write_text("policy\n", encoding="utf-8")
            text = render(make_profile(root), pathlib.PurePosixPath("/home/demo/deploy"), policy)
            self.assertIn("required_labels:", text)
            self.assertIn("terminal_states:", text)
            self.assertIn("thread_sandbox: workspace-write", text)
            self.assertNotIn("Bearer", text)

    def test_deployment_dry_run_is_available_without_secret(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/deploy.py"),
            "--profile", str(ROOT / "projects/cleanroom/profile.toml"), "--dry-run"],
            text=True, capture_output=True, check=True)
        self.assertIn('"profile": "cleanroom"', result.stdout)

if __name__ == "__main__":
    unittest.main(verbosity=2)
