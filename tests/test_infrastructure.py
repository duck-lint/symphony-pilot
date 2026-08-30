"""Focused infrastructure regression tests for the reusable pilot runtime."""
from __future__ import annotations
import json
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
import prepare_workspace as pw
import after_run
sys.path.insert(0, str(ROOT / "scripts"))
import project as project_control
import deploy
import host_integration
import process_identity

def make_profile(root: pathlib.Path) -> pw.Profile:
    return pw.Profile("demo", "example/project", "git@example:project.git",
        root / "work", root / "state", root / "logs", "github.token",
        ("symphony:auto",), "symphony:human", "demo", 4040, 1, 8, 5000,
        300000, "gpt-5.6-luna", "high", None, root / "deployment")


def process_state(pid: int = 123) -> dict:
    return {"schema": "symphony-pilot-process/v1", "identity": {
        "pid": pid, "boot_id": "boot", "start_time": "start",
        "executable": "/bin/symphony", "cmdline_sha256": "cmd"}}

def git_repo(path: pathlib.Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    (path / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)

class InfrastructureTests(unittest.TestCase):
    def profile_with(self, profile, **changes):
        return profile.__class__(**{**profile.__dict__, **changes})

    def test_idle_stop_is_allowed_after_runtime_state_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(pathlib.Path(directory))
            pid_path, _ = project_control.state_paths(profile)
            pid_path.write_text(json.dumps(process_state()) + "\n", encoding="ascii")
            with mock.patch.object(project_control, "_identity_alive", side_effect=[True, False]), \
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
            pid_path.write_text(json.dumps(process_state()) + "\n", encoding="ascii")
            with mock.patch.object(project_control, "_identity_alive", return_value=True), \
                    mock.patch.object(project_control, "runtime_state", return_value={
                        "running": [{"issue_identifier": "GH-7"}], "retrying": []}), \
                    mock.patch.object(project_control.os, "kill") as kill:
                self.assertEqual(project_control.stop(profile), 2)
            kill.assert_not_called()
            self.assertTrue(pid_path.exists())

    def test_status_maps_runtime_activity_arrays_to_safe_operator_state(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(pathlib.Path(directory))
            pid_path, _ = project_control.state_paths(profile)
            pid_path.write_text(json.dumps(process_state()) + "\n", encoding="ascii")
            output = StringIO()
            with mock.patch.object(project_control, "_identity_alive", return_value=True), \
                    mock.patch.object(project_control, "runtime_state", return_value={
                        "blocked": [],
                        "running": [{"issue_identifier": "GH-7"}],
                        "retrying": [],
                    }), redirect_stdout(output):
                self.assertEqual(project_control.status(profile), 0)
            self.assertEqual(output.getvalue().strip(), "WORKING ON #GH-7 - DO NOT SHUT DOWN")

    def test_status_after_crash_releases_awake_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = self.profile_with(make_profile(pathlib.Path(directory)),
                                        prevent_host_sleep=True)
            pid_path, _ = project_control.state_paths(profile)
            pid_path.write_text(json.dumps(process_state()) + "\n", encoding="ascii")
            with mock.patch.object(project_control, "_managed_identity", return_value=None), \
                    mock.patch.object(project_control, "release_awake_guard") as release:
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(project_control.status(profile), 0)
            release.assert_called_once_with(profile)
            self.assertIn("STOPPED - SAFE TO SHUT DOWN", output.getvalue())

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

    def test_duplicate_workpads_are_not_selected(self):
        comments = [{"id": 1, "body": "<!-- symphony-workpad:v1 --> one"},
                    {"id": 2, "body": "<!-- symphony-workpad:v1 --> two"}]
        self.assertEqual(pw.workpad_candidates(comments), comments)
        self.assertIsNone(pw.workpad(comments))

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

    def test_multiple_matching_issue_prs_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "work" / "GH-11"
            repo.mkdir(parents=True)
            p = make_profile(root)
            old_github, old_git = pw.github, pw.git
            try:
                def fake_github(_p, _t, _m, path, _body=None):
                    if path == "/issues/11":
                        return {"body": "required starting commit: " + "e"*40}
                    if path.startswith("/issues/11/comments"):
                        return []
                    if path.startswith("/pulls"):
                        return [
                            {"number": 1, "head": {"repo": {"full_name": p.repository},
                                                     "ref": "codex/gh-11-one"}},
                            {"number": 2, "head": {"repo": {"full_name": p.repository},
                                                     "ref": "codex/gh-11-two"}},
                        ]
                    return []
                pw.github = fake_github
                pw.git = lambda *_args, **_kwargs: ""
                with self.assertRaisesRegex(pw.PreparationError, "more than one matching"):
                    pw.issue_facts(p, repo, "secret")
            finally:
                pw.github, pw.git = old_github, old_git

    def test_cardinality_checks_include_later_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "work" / "GH-13"
            repo.mkdir(parents=True)
            p = make_profile(root)
            old_github, old_git = pw.github, pw.git
            try:
                pad = {"id": 9, "body": "<!-- symphony-workpad:v1 -->\nlate pad"}
                matching_pr = {"number": 4, "draft": True,
                               "head": {"repo": {"full_name": p.repository},
                                        "ref": "codex/gh-13-work"}}

                def fake_github(_p, _t, _m, path, _body=None):
                    if path == "/issues/13":
                        return {"body": "required starting commit: " + "d"*40}
                    if path.startswith("/issues/13/comments"):
                        return ([{"body": "ordinary comment"}] * 100
                                if "&page=1" in path else [pad])
                    if path.startswith("/pulls"):
                        return ([{"head": {"repo": {"full_name": "other/repo"},
                                            "ref": "unrelated"}}] * 100
                                if "&page=1" in path else [matching_pr])
                    return []

                pw.github = fake_github
                pw.git = lambda *_args, **_kwargs: ""
                facts = pw.issue_facts(p, repo, "secret")
                self.assertEqual(facts.pr_number, 4)
                self.assertIn("<!-- symphony-workpad:v1 -->", facts.comments[-1]["body"])
            finally:
                pw.github, pw.git = old_github, old_git

    def test_non_draft_issue_pr_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "work" / "GH-12"
            repo.mkdir(parents=True)
            p = make_profile(root)
            old_github, old_git = pw.github, pw.git
            try:
                def fake_github(_p, _t, _m, path, _body=None):
                    if path == "/issues/12":
                        return {"body": "required starting commit: " + "f"*40}
                    if path.startswith("/issues/12/comments"):
                        return []
                    if path.startswith("/pulls"):
                        return [{"number": 3, "draft": False,
                                 "head": {"repo": {"full_name": p.repository},
                                          "ref": "codex/gh-12-work"}}]
                    return []
                pw.github = fake_github
                pw.git = lambda *_args, **_kwargs: ""
                with self.assertRaisesRegex(pw.PreparationError, "not a draft"):
                    pw.issue_facts(p, repo, "secret")
            finally:
                pw.github, pw.git = old_github, old_git

    def test_launcher_unsets_tracker_variables(self):
        launcher = (ROOT / "runtime/launch_codex.sh").read_text(encoding="utf-8")
        self.assertIn("unset SYMPHONY_PILOT_GITHUB_TOKEN", launcher)
        self.assertLess(launcher.index("unset SYMPHONY_PILOT_GITHUB_TOKEN"),
                        launcher.index("source .git/symphony-toolchain.env"))
        self.assertGreater(launcher.rindex("unset SYMPHONY_PILOT_GITHUB_TOKEN"),
                           launcher.index("source .git/symphony-toolchain.env"))
        self.assertIn("app-server", launcher)
        self.assertIn(".codex/agents", launcher)
        self.assertIn("target-owned role policy collision", launcher)
        self.assertIn("export CODEX_HOME", launcher)
        self.assertIn('mktemp -d "/tmp/symphony-pilot-codex-home.XXXXXX"', launcher)
        self.assertIn("trap cleanup_role_home EXIT INT TERM", launcher)
        self.assertNotIn('ROLE_TARGET="$PWD/.codex/agents"', launcher)

    def test_launcher_role_setup_leaves_target_git_state_clean(self):
        bash = None
        if os.name == "nt":
            for candidate in (r"C:\Program Files\Git\bin\bash.exe",
                              r"C:\Program Files\Git\usr\bin\bash.exe"):
                if pathlib.Path(candidate).exists():
                    bash = candidate
                    break
        else:
            bash = shutil.which("bash")
        if not bash:
            self.skipTest("Bash is unavailable")

        def bash_path(path):
            value = str(path)
            if os.name == "nt" and len(value) > 2 and value[1] == ":":
                return "/" + value[0].lower() + value[2:].replace("\\", "/")
            return value

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "repo"
            repo.mkdir()
            git_repo(repo)
            (repo / ".git" / "symphony-toolchain.env").write_text(
                "export GITHUB_TOKEN=must-not-reach-child\n"
                "export TMPDIR=\"$PWD\"\n", encoding="utf-8")
            role_source = root / "role-source"
            role_source.mkdir()
            for policy in (ROOT / "workflow/agents").glob("*.toml"):
                shutil.copy2(policy, role_source / policy.name)
            original_home = root / "operator-codex"
            original_home.mkdir()
            probe = root / "probe"
            fake = root / "fake-codex"
            fake.write_text(
                "#!/usr/bin/env bash\n"
                "test -f \"$CODEX_HOME/agents/reviewer.toml\" || exit 41\n"
                "test -z \"${SYMPHONY_PILOT_GITHUB_TOKEN:-}\" || exit 42\n"
                "printf '%s' \"$CODEX_HOME\" > \"$ROLE_PROBE\"\n"
                "touch \"$ROLE_READY\"\n"
                "while [[ ! -e \"$ROLE_RELEASE\" ]]; do sleep 0.01; done\n"
                "exit 143\n", encoding="utf-8")
            fake.chmod(0o755)
            env = os.environ.copy()
            env.update({"CODEX_BIN": bash_path(fake), "CODEX_HOME": bash_path(original_home),
                        "SYMPHONY_PILOT_ROLE_POLICY_DIR": bash_path(role_source),
                        "SYMPHONY_PILOT_GITHUB_TOKEN": "must-not-reach-child",
                        "ROLE_PROBE": bash_path(probe),
                        "ROLE_READY": bash_path(root / "ready"),
                        "ROLE_RELEASE": bash_path(root / "release")})
            child = subprocess.Popen([bash, bash_path(ROOT / "runtime/launch_codex.sh")],
                                     cwd=repo, env=env, text=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    if (root / "ready").exists():
                        break
                    time.sleep(0.01)
                self.assertTrue((root / "ready").exists())
                status_while_running = subprocess.run(
                    ["git", "status", "--porcelain"], cwd=repo,
                    text=True, capture_output=True, check=True)
                self.assertEqual(status_while_running.stdout, "")
                self.assertFalse((repo / ".codex").exists())
                (root / "release").touch()
                result_code = child.wait(timeout=10)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
            self.assertEqual(result_code, 143)
            self.assertTrue(probe.exists())
            self.assertNotIn(str(repo), probe.read_text(encoding="utf-8"))
            self.assertFalse((repo / ".codex").exists())
            status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                    text=True, capture_output=True, check=True)
            self.assertEqual(status.stdout, "")

    def test_generic_role_policies_have_distinct_contracts(self):
        import tomllib

        expected = {
            "project-manager": "read-only",
            "planner": "read-only",
            "implementer": "workspace-write",
            "reviewer": "read-only",
            "adversary": "read-only",
            "archivist": "read-only",
        }
        policies = sorted((ROOT / "workflow/agents").glob("*.toml"))
        self.assertEqual({path.stem for path in policies}, set(expected))
        descriptions = set()
        for path in policies:
            with path.open("rb") as stream:
                data = tomllib.load(stream)
            self.assertEqual(data["name"], path.stem)
            self.assertEqual(data["sandbox_mode"], expected[path.stem])
            self.assertTrue(data["description"])
            self.assertTrue(data["developer_instructions"])
            descriptions.add(data["developer_instructions"])
        self.assertEqual(len(descriptions), len(expected))
        archivist = next(path for path in policies if path.stem == "archivist")
        archivist_text = archivist.read_text(encoding="utf-8")
        self.assertIn("bounded archival packet", archivist_text)
        self.assertIn("only role that persists", archivist_text)
        self.assertIn("Do not mutate the workpad", archivist_text)

    def test_architect_policy_defines_independent_gates_and_adjudication(self):
        policy = (ROOT / "workflow/architect_policy.md").read_text(encoding="utf-8")
        for phrase in (
            "ARCHITECT / ORCHESTRATOR",
            "PROJECT-MANAGER",
            "PLANNER",
            "IMPLEMENTER",
            "REVIEWER",
            "ADVERSARY",
            "ARCHIVIST",
            "licensed correction | unresolved project decision | infrastructure condition",
            "A correction invalidates every prior acceptance of the older HEAD",
            "Reviewer and adversary findings are internal orchestration state",
            "<!-- symphony-workpad:v1 -->",
            "final fresh REVIEWER",
            "final fresh ADVERSARY",
            "ARCHIVIST is a read-only continuity/closeout role",
            "alone persists accepted durable state",
        ):
            self.assertIn(phrase, policy)
        self.assertNotIn("built-in worker subagent", policy)

    def test_deployment_inventory_includes_all_generic_role_policies(self):
        self.assertEqual({path.stem for path in deploy.ROLE_POLICY_FILES},
                         deploy.EXPECTED_ROLE_NAMES)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/deploy.py"),
            "--profile", str(ROOT / "projects/cleanroom/profile.toml"), "--dry-run"],
            text=True, capture_output=True, check=True)
        data = json.loads(result.stdout)
        self.assertEqual(set(data["role_policies"]), deploy.EXPECTED_ROLE_NAMES)
        self.assertEqual(data["files"], 17)

    def test_deployed_test_requires_and_verifies_role_policy_files(self):
        source = (ROOT / "scripts/project.py").read_text(encoding="utf-8")
        for name in ("project-manager", "planner", "implementer", "reviewer", "adversary", "archivist"):
            self.assertIn(f'"{name}"', source)
        self.assertIn("deployed file does not match its manifest", source)

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

    def test_recovery_archive_excludes_secret_named_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "work" / "GH-10"
            repo.mkdir(parents=True)
            git_repo(repo)
            (repo / ".env").write_text("PRIVATE=do-not-archive\n", encoding="utf-8")
            (repo / "private.pem").write_text("PRIVATE KEY\n", encoding="utf-8")
            (repo / "safe-dir").mkdir()
            (repo / "safe-dir" / ".env").write_text("NESTED=do-not-archive\n", encoding="utf-8")
            (repo / "safe-dir" / "private.pem").write_text("PRIVATE KEY\n", encoding="utf-8")
            (repo / "safe-dir" / "credentials").mkdir()
            (repo / "safe-dir" / "credentials" / "token.txt").write_text("TOKEN\n", encoding="utf-8")
            (repo / "dirty.txt").write_text("recover\n", encoding="utf-8")
            p = make_profile(root)
            facts = pw.IssueFacts(10, "branch", "c"*40, None, None, None, None, "initial", None, None, [])
            archive = pw.archive_recovery(p, repo, facts, "?? dirty.txt")
            import tarfile
            with tarfile.open(archive) as tar:
                names = tar.getnames()
                manifest = json.loads(tar.extractfile("RECOVERY-MANIFEST.json").read())
            self.assertIn("dirty.txt", names)
            self.assertNotIn(".env", names)
            self.assertNotIn("private.pem", names)
            self.assertNotIn("safe-dir/.env", names)
            self.assertNotIn("safe-dir/private.pem", names)
            self.assertNotIn("safe-dir/credentials/token.txt", names)
            self.assertEqual(set(manifest["excluded_paths"]), {
                ".env", "private.pem", "safe-dir/.env", "safe-dir/private.pem",
                "safe-dir/credentials"})

    def test_manifest_verification_rejects_tampered_or_uninventoried_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "runtime").mkdir()
            (root / "runtime" / "launch_codex.sh").write_text("original\n", encoding="utf-8")
            manifest_path = root / "DEPLOYMENT.json"
            digest = hashlib.sha256(
                (root / "runtime" / "launch_codex.sh").read_bytes()).hexdigest()
            manifest = {"files": {"runtime/launch_codex.sh": digest}}
            project_control.verify_manifest(root, manifest_path, manifest)
            (root / "runtime" / "launch_codex.sh").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not match"):
                project_control.verify_manifest(root, manifest_path, manifest)

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

    def test_finish_already_stopped_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(pathlib.Path(directory))
            with mock.patch.object(project_control, "_safe_pid", return_value=None), \
                    mock.patch.object(project_control, "release_awake_guard"):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(project_control.finish(profile), 0)
            self.assertIn("STOPPED - SAFE TO SHUT DOWN", output.getvalue())

    def test_finish_drains_running_and_retrying_before_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(pathlib.Path(directory))
            states = [
                {"running": [{"issue_identifier": "GH-7"}], "retrying": []},
                {"running": [], "retrying": [{"issue_identifier": "GH-7"}]},
                {"running": [], "retrying": []},
            ]
            with mock.patch.object(project_control, "_safe_pid", return_value=123), \
                    mock.patch.object(project_control, "runtime_state", side_effect=states), \
                    mock.patch.object(project_control, "stop", return_value=0) as stop, \
                    mock.patch.object(project_control.time, "sleep"):
                self.assertEqual(project_control.finish(profile), 0)
            stop.assert_called_once_with(profile)

    def test_finish_fails_closed_when_runtime_state_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(pathlib.Path(directory))
            with mock.patch.object(project_control, "_safe_pid", return_value=123), \
                    mock.patch.object(project_control, "runtime_state", return_value=None), \
                    mock.patch.object(project_control, "stop") as stop:
                self.assertEqual(project_control.finish(profile), 1)
            stop.assert_not_called()

    def test_finish_rejects_unexpected_second_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(pathlib.Path(directory))
            with mock.patch.object(project_control, "_safe_pid", return_value=123), \
                    mock.patch.object(project_control, "runtime_state", return_value={
                        "running": [{"issue_identifier": "GH-7"}, {"issue_identifier": "GH-8"}],
                        "retrying": []}), \
                    mock.patch.object(project_control, "stop") as stop:
                self.assertEqual(project_control.finish(profile), 1)
            stop.assert_not_called()

    def test_finish_allows_human_blocked_issue_without_active_work(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(pathlib.Path(directory))
            with mock.patch.object(project_control, "_safe_pid", return_value=123), \
                    mock.patch.object(project_control, "runtime_state", return_value={
                        "blocked": [{"issue_identifier": "GH-7"}],
                        "running": [], "retrying": []}), \
                    mock.patch.object(project_control, "stop", return_value=0) as stop:
                self.assertEqual(project_control.finish(profile), 0)
            stop.assert_called_once_with(profile)

    def test_awake_guard_is_optional_and_records_no_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            disabled = make_profile(root)
            with mock.patch.object(host_integration.subprocess, "Popen") as popen:
                host_integration.establish_awake_guard(disabled)
                popen.assert_not_called()
            enabled = self.profile_with(disabled, prevent_host_sleep=True)
            fake = mock.Mock(pid=456)
            identity = {"pid": 456, "boot_id": "boot", "start_time": "start"}
            with mock.patch.object(host_integration.subprocess, "Popen", return_value=fake) as popen, \
                    mock.patch.object(host_integration, "capture", return_value=identity), \
                    mock.patch.object(host_integration, "_identity_alive", return_value=False):
                host_integration.establish_awake_guard(enabled)
            data = json.loads((enabled.state_root / host_integration.AWAKE_STATE).read_text())
            self.assertEqual(data["pid"], 456)
            self.assertNotIn("github", json.dumps(popen.call_args).lower())

    def test_awake_guard_releases_on_stale_or_normal_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            profile = self.profile_with(make_profile(root), prevent_host_sleep=True)
            path = profile.state_root / host_integration.AWAKE_STATE
            profile.state_root.mkdir(parents=True)
            path.write_text(json.dumps({"pid": 456, "identity": {"pid": 456}}) + "\n", encoding="utf-8")
            with mock.patch.object(host_integration, "_identity_alive", return_value=False):
                host_integration.recover_awake_guard(profile)
            self.assertFalse(path.exists())

    def test_start_failure_releases_awake_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            profile = self.profile_with(make_profile(root), prevent_host_sleep=True)
            (profile.deployment_root / "projects" / profile.slug).mkdir(parents=True)
            (profile.deployment_root / "projects" / profile.slug / "WORKFLOW.md").write_text("workflow\n")
            with mock.patch.object(project_control, "read_secret", return_value="secret"), \
                    mock.patch.object(project_control, "active_issues", return_value=[]), \
                    mock.patch.object(project_control, "establish_awake_guard"), \
                    mock.patch.object(project_control, "release_awake_guard") as release, \
                    mock.patch.object(project_control.subprocess, "Popen", side_effect=OSError("missing")):
                self.assertEqual(project_control.start(profile), 1)
            release.assert_called_once_with(profile)

    def test_start_identity_capture_failure_kills_unmanaged_child_and_retains_guard_if_needed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            profile = self.profile_with(make_profile(root), prevent_host_sleep=True)
            (profile.deployment_root / "projects" / profile.slug).mkdir(parents=True)
            (profile.deployment_root / "projects" / profile.slug / "WORKFLOW.md").write_text("workflow\n")
            child = mock.Mock(pid=123, poll=mock.Mock(return_value=None))
            child.wait.side_effect = [subprocess.TimeoutExpired("symphony", 5),
                                      subprocess.TimeoutExpired("symphony", 5)]
            with mock.patch.object(project_control, "read_secret", return_value="secret"), \
                    mock.patch.object(project_control, "active_issues", return_value=[]), \
                    mock.patch.object(project_control, "establish_awake_guard"), \
                    mock.patch.object(project_control, "release_awake_guard") as release, \
                    mock.patch.object(project_control.subprocess, "Popen", return_value=child), \
                    mock.patch.object(project_control, "capture", return_value=None):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(project_control.start(profile), 1)
            child.terminate.assert_called_once_with()
            child.kill.assert_called_once_with()
            release.assert_not_called()
            self.assertIn("awake guard was retained", output.getvalue())
            self.assertFalse(project_control.state_paths(profile)[0].exists())

    def test_start_timeout_terminates_child_before_releasing_awake_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            profile = self.profile_with(make_profile(root), prevent_host_sleep=True)
            (profile.deployment_root / "projects" / profile.slug).mkdir(parents=True)
            (profile.deployment_root / "projects" / profile.slug / "WORKFLOW.md").write_text("workflow\n")
            child = mock.Mock(pid=123, poll=mock.Mock(return_value=None))
            identity = process_state()["identity"]
            with mock.patch.object(project_control, "read_secret", return_value="secret"), \
                    mock.patch.object(project_control, "active_issues", return_value=[]), \
                    mock.patch.object(project_control, "establish_awake_guard"), \
                    mock.patch.object(project_control, "release_awake_guard") as release, \
                    mock.patch.object(project_control.subprocess, "Popen", return_value=child), \
                    mock.patch.object(project_control, "capture", return_value=identity), \
                    mock.patch.object(project_control, "dashboard", return_value=None), \
                    mock.patch.object(project_control, "_identity_alive", side_effect=[True, False]), \
                    mock.patch.object(project_control.time, "monotonic", side_effect=[0, 31, 0, 31]), \
                    mock.patch.object(project_control.os, "kill") as kill:
                self.assertEqual(project_control.start(profile), 1)
            kill.assert_called_once_with(123, project_control.signal.SIGTERM)
            release.assert_called_once_with(profile)
            self.assertFalse(project_control.state_paths(profile)[0].exists())

    def test_start_timeout_retains_pid_and_awake_guard_if_child_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            profile = self.profile_with(make_profile(root), prevent_host_sleep=True)
            (profile.deployment_root / "projects" / profile.slug).mkdir(parents=True)
            (profile.deployment_root / "projects" / profile.slug / "WORKFLOW.md").write_text("workflow\n")
            child = mock.Mock(pid=123, poll=mock.Mock(return_value=None))
            identity = process_state()["identity"]
            with mock.patch.object(project_control, "read_secret", return_value="secret"), \
                    mock.patch.object(project_control, "active_issues", return_value=[]), \
                    mock.patch.object(project_control, "establish_awake_guard"), \
                    mock.patch.object(project_control, "release_awake_guard") as release, \
                    mock.patch.object(project_control.subprocess, "Popen", return_value=child), \
                    mock.patch.object(project_control, "capture", return_value=identity), \
                    mock.patch.object(project_control, "dashboard", return_value=None), \
                    mock.patch.object(project_control, "_identity_alive", return_value=True), \
                    mock.patch.object(project_control.time, "monotonic", side_effect=[0, 31, 0, 31]), \
                    mock.patch.object(project_control.os, "kill") as kill:
                self.assertEqual(project_control.start(profile), 1)
            kill.assert_called_once_with(123, project_control.signal.SIGTERM)
            release.assert_not_called()
            self.assertTrue(project_control.state_paths(profile)[0].exists())

    def test_stop_releases_awake_guard_after_process_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = self.profile_with(make_profile(pathlib.Path(directory)), prevent_host_sleep=True)
            pid_path, _ = project_control.state_paths(profile)
            pid_path.write_text(json.dumps(process_state()) + "\n", encoding="ascii")
            with mock.patch.object(project_control, "_identity_alive", side_effect=[True, False]), \
                    mock.patch.object(project_control, "runtime_state", return_value={
                        "running": [], "retrying": []}), \
                    mock.patch.object(project_control.os, "kill"), \
                    mock.patch.object(project_control, "release_awake_guard") as release:
                self.assertEqual(project_control.stop(profile), 0)
            release.assert_called_once_with(profile)

    def test_recycled_symphony_pid_is_never_terminated(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(pathlib.Path(directory))
            pid_path, _ = project_control.state_paths(profile)
            pid_path.write_text(json.dumps(process_state()) + "\n", encoding="ascii")
            with mock.patch.object(project_control, "_identity_alive", return_value=False), \
                    mock.patch.object(project_control, "pid_alive", return_value=True), \
                    mock.patch.object(project_control.os, "kill") as kill:
                self.assertEqual(project_control.stop(profile, force=True), 1)
            kill.assert_not_called()
            self.assertTrue(pid_path.exists())

    def test_process_identity_binds_boot_and_start_time(self):
        with mock.patch.object(process_identity, "_boot_id", return_value="boot"), \
                mock.patch.object(process_identity, "_start_time", return_value="start"):
            identity = process_identity.capture(os.getpid())
        self.assertIsNotNone(identity)
        with mock.patch.object(process_identity, "capture", return_value=identity):
            self.assertTrue(process_identity.matches(identity))
        altered = {**identity, "start_time": "different"}
        with mock.patch.object(process_identity, "capture", return_value={**identity, "start_time": "other"}):
            self.assertFalse(process_identity.matches(altered))

    def test_recycled_awake_pid_is_never_terminated(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = self.profile_with(make_profile(pathlib.Path(directory)), prevent_host_sleep=True)
            path = profile.state_root / host_integration.AWAKE_STATE
            profile.state_root.mkdir(parents=True)
            path.write_text(json.dumps({"pid": 456, "identity": process_state(456)["identity"]}) + "\n")
            with mock.patch.object(host_integration, "_identity_alive", return_value=False), \
                    mock.patch.object(host_integration.os, "kill") as kill:
                host_integration.release_awake_guard(profile)
            kill.assert_not_called()
            self.assertFalse(path.exists())

    def test_notification_redacts_credential_shapes_and_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = self.profile_with(make_profile(pathlib.Path(directory)),
                                        notifications_enabled=True, display_name="Demo")
            secret_text = (
                "github_pat_ABC123 ghp_FAKE123 sk-proj-SECRET123 "
                "https://user:pass@example.invalid/x?token=QUERYSECRET "
                "-----BEGIN PRIVATE KEY-----\nPRIVATESECRET\n-----END PRIVATE KEY-----"
            )
            result = mock.Mock(returncode=0)
            with mock.patch.object(host_integration.subprocess, "run", return_value=result) as run:
                self.assertTrue(host_integration.notify(
                    profile, "infrastructure", 7, secret_text,
                    "https://user:pass@example.invalid/?api_key=URLSECRET",
                    secret_text))
            payload = run.call_args.kwargs["input"]
            self.assertNotIn("ABC123", payload)
            self.assertNotIn("FAKE123", payload)
            self.assertNotIn("SECRET123", payload)
            self.assertNotIn("PRIVATESECRET", payload)
            self.assertNotIn("URLSECRET", payload)
            self.assertNotIn("QUERYSECRET", payload)
            self.assertNotIn("pass@example.invalid", payload)
            self.assertNotIn("FAKESECRET", payload)
            state = (profile.state_root / host_integration.NOTIFICATION_STATE).read_text()
            self.assertNotIn("ABC123", state)
            self.assertNotIn("PRIVATESECRET", state)
            self.assertNotIn("URLSECRET", state)
            self.assertNotIn("QUERYSECRET", state)

    def test_notification_redacts_complete_authorization_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = self.profile_with(make_profile(pathlib.Path(directory)),
                                        notifications_enabled=True, display_name="Demo")
            message = ("Authorization: Basic FAKESECRET\n"
                       "Authorization: Foo FAKESECRET\n"
                       "X-Api-Key: FAKESECRET")
            with mock.patch.object(host_integration.subprocess, "run",
                                   return_value=mock.Mock(returncode=0)) as run:
                self.assertTrue(host_integration.notify(profile, "infrastructure", 7, message))
            payload = run.call_args.kwargs["input"]
            self.assertNotIn("FAKESECRET", payload)
            state = (profile.state_root / host_integration.NOTIFICATION_STATE).read_text()
            self.assertNotIn("FAKESECRET", state)

    def test_notification_transition_can_reblock_after_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = self.profile_with(make_profile(pathlib.Path(directory)),
                                        notifications_enabled=True, display_name="Demo")
            result = mock.Mock(returncode=0)
            with mock.patch.object(host_integration.subprocess, "run", return_value=result) as run:
                self.assertTrue(host_integration.notify(profile, "human", 7, "blocked", fingerprint="state-a"))
                self.assertFalse(host_integration.notify(profile, "human", 7, "blocked", fingerprint="state-a"))
                host_integration.clear_notification(profile, "human", 7)
                self.assertTrue(host_integration.notify(profile, "human", 7, "blocked", fingerprint="state-a"))
                self.assertTrue(host_integration.notify(profile, "human", 8, "blocked", fingerprint="state-a"))
            self.assertEqual(run.call_count, 3)

    def test_historical_infrastructure_does_not_classify_current_human_block(self):
        body = ("### Human decision required\n- decide the semantic question\n"
                "### Preserved history\n### Infrastructure blocker\n- detail: old provider failure\n")
        self.assertFalse(after_run.is_infrastructure_blocker(body))

    def test_current_infrastructure_detail_comes_only_from_active_blocker(self):
        body = ("### Infrastructure blocker\n- class: provider\n"
                "- detail: local provider unavailable\n"
                "### Preserved history\n- detail: stale credential value\n")
        self.assertTrue(after_run.is_infrastructure_blocker(body))
        self.assertIn("local provider unavailable", after_run.infrastructure_message(body))
        self.assertNotIn("stale credential", after_run.infrastructure_message(body))

    def test_blocker_kind_transition_clears_opposite_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = self.profile_with(make_profile(pathlib.Path(directory)),
                                        notifications_enabled=True, display_name="Demo")
            state_path = profile.state_root / host_integration.NOTIFICATION_STATE
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({
                "human:GH-7": "a" * 64,
                "infrastructure:GH-7": "b" * 64,
            }) + "\n", encoding="utf-8")
            body = "<!-- symphony-workpad:v1 -->\n### Human decision required\n"
            with mock.patch.object(after_run, "load_profile", return_value=profile), \
                    mock.patch.object(after_run, "read_secret", return_value="redacted"), \
                    mock.patch.object(after_run, "comments", return_value=[{"id": 1, "body": body}]), \
                    mock.patch.object(after_run, "github", side_effect=[
                        [{"name": "symphony:human"}], {"state": "open"}
                    ]), \
                    mock.patch.object(host_integration.subprocess, "run",
                                      return_value=mock.Mock(returncode=0)), \
                    mock.patch.object(sys, "argv", ["after_run", "--profile", "x",
                                                     "--workspace", "GH-7"]):
                self.assertEqual(after_run.main(), 0)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("infrastructure:GH-7", state)
            self.assertIn("human:GH-7", state)

    def test_notification_deduplication_is_project_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            profile = self.profile_with(make_profile(root), notifications_enabled=True,
                                        display_name="Demo")
            result = mock.Mock(returncode=0)
            with mock.patch.object(host_integration.subprocess, "run", return_value=result) as run:
                self.assertTrue(host_integration.notify(profile, "human", 7, "needs attention"))
                self.assertFalse(host_integration.notify(profile, "human", 7, "needs attention"))
                self.assertTrue(host_integration.notify(profile, "infrastructure", 7, "provider unavailable"))
                self.assertTrue(host_integration.notify(profile, "completed", 7, "Issue #7 completed."))
            self.assertEqual(run.call_count, 3)
            self.assertNotIn("secret", (profile.state_root / host_integration.NOTIFICATION_STATE).read_text())

    def test_deployment_contains_operator_cli_and_host_backend(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/deploy.py"),
            "--profile", str(ROOT / "projects/cleanroom/profile.toml"), "--dry-run"],
            text=True, capture_output=True, check=True)
        self.assertIn('"source_commit"', result.stdout)

if __name__ == "__main__":
    unittest.main(verbosity=2)
