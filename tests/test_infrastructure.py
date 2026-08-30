"""Focused infrastructure regression tests for the reusable pilot runtime."""
from __future__ import annotations
import json
import hashlib
import os
import pathlib
import shutil
import socket
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
import project_registry
import provision_secret

def make_profile(root: pathlib.Path) -> pw.Profile:
    return pw.Profile(
        slug="demo", repository="example/project", git_remote="git@example:project.git",
        workspace_root=root / "work", state_root=root / "state", log_root=root / "logs",
        secret_reference="github.token", dispatch_labels=("symphony:auto",),
        blocked_label="symphony:human", service_identity="symphony-pilot-demo",
        dashboard_port=4040, max_concurrent_agents=1, max_turns=8,
        poll_interval_ms=5000, max_retry_backoff_ms=300000,
        codex_model="gpt-5.6-luna", codex_reasoning_effort="high", toolchain=None)


def write_registry_profile(root: pathlib.Path, slug: str, repository: str | None = None,
                           dashboard_port: int | None = None) -> pathlib.Path:
    directory = root / slug
    directory.mkdir(parents=True)
    path = directory / "profile.toml"
    if dashboard_port is None:
        dashboard_port = 4040 + len(list(root.glob("*/profile.toml")))
    path.write_text(
        f'''slug = "{slug}"
repository = "{repository or "example/" + slug}"
git_remote = "git@example:{slug}.git"
secret_reference = "github.token"
dispatch_labels = ["symphony:auto"]
blocked_label = "symphony:human"
dashboard_port = {dashboard_port}
max_concurrent_agents = 1
max_turns = 8
poll_interval_ms = 5000
max_retry_backoff_ms = 300000
codex_model = "gpt-5.6-luna"
codex_reasoning_effort = "high"
''', encoding="utf-8")
    return path


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

    def deploy_fixture(self, root: pathlib.Path, slug: str = "alpha", port: int = 4040):
        registry = root / "registry"
        profile_path = write_registry_profile(registry, slug, dashboard_port=port)
        target = root / "deployment" / slug
        source_commit = "a" * 40
        real_run = deploy.subprocess.run

        def fake_run(command, *args, **kwargs):
            if command[:3] == ["git", "rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(command, 0, source_commit + "\n", "")
            if command[:3] == ["git", "status", "--porcelain=v1"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            return real_run(command, *args, **kwargs)

        with mock.patch.object(deploy.subprocess, "run", side_effect=fake_run):
            deploy.deploy(profile_path, target, False)
        return pw.load_profile(profile_path), target

    def write_recovery_state(self, path: pathlib.Path, dashboard_port: int = 4040) -> dict:
        state = {
            "schema": "symphony-pilot-process/v2",
            "identity": {"pid": 123, "boot_id": "boot", "start_time": "start"},
            "deployment_root": "/home/duck-lint/.local/share/symphony-pilot/deployments/alpha",
            "deployment_identity": "a" * 64,
            "deployed_profile_sha256": "b" * 64,
            "dashboard_url": f"http://127.0.0.1:{dashboard_port}",
        }
        path.write_text(json.dumps(state) + "\n", encoding="utf-8")
        return state

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
        self.assertIn("reserved Codex agent name collision", launcher)
        self.assertIn("export CODEX_HOME", launcher)
        self.assertIn('mktemp -d "/tmp/symphony-pilot-codex-home.XXXXXX"', launcher)
        self.assertIn('exec "${CODEX_BIN:-codex}"', launcher)
        self.assertIn("trap - EXIT INT TERM", launcher)
        self.assertIn("symphony-pilot-role-home/v2", launcher)
        self.assertIn("tomllib", launcher)
        self.assertNotIn('ROLE_TARGET="$PWD/.codex/agents"', launcher)
        self.assertIn('cannot establish App Server process identity', launcher)

    def test_role_home_cleanup_uses_fixed_root_not_ambient_tempdir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            role_home = pathlib.Path(tempfile.mkdtemp(prefix="symphony-pilot-codex-home.fixture"))
            with mock.patch.object(pw, "ROLE_HOME_ROOT", role_home.parent), \
                    mock.patch.object(tempfile, "gettempdir", return_value=str(root / "redirected")):
                self.assertEqual(pw._role_home_path(str(role_home)), role_home.resolve())
            shutil.rmtree(role_home)

    def test_launcher_overlay_preserves_operator_policy_surface_and_target_clean(self):
        if os.name == "nt":
            self.skipTest("launcher process fixture requires POSIX process semantics")
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
            operator_hook = original_home / "harmless-hook.sh"
            operator_hook.write_text(
                "#!/usr/bin/env bash\n"
                "printf executed > \"$HOOK_OBSERVATION\"\n", encoding="utf-8")
            operator_hook.chmod(0o755)
            (original_home / "hooks.json").write_text(
                json.dumps({"harmless_hook": bash_path(operator_hook)}) + "\n", encoding="utf-8")
            (original_home / "agents").mkdir()
            (original_home / "agents" / "personal.toml").write_text(
                'name = "personal"\ndescription = "operator policy"\n'
                'developer_instructions = "operator policy"\n', encoding="utf-8")
            operator_reviewer = original_home / "agents" / "reviewer.toml"
            operator_reviewer.write_text(
                'name = "not-the-symphony-reviewer"\n'
                'description = "operator"\n'
                'developer_instructions = "operator"\n', encoding="utf-8")
            operator_reviewer_digest = hashlib.sha256(operator_reviewer.read_bytes()).hexdigest()
            probe = root / "probe"
            pid_probe = root / "pid"
            fake = root / "fake-codex"
            fake.write_text(
                "#!/usr/bin/env bash\n"
                "grep -l '^name = \"reviewer\"$' \"$CODEX_HOME\"/agents/*.toml >/dev/null || { echo reviewer-missing >&2; exit 41; }\n"
                "grep -l '^name = \"not-the-symphony-reviewer\"$' \"$CODEX_HOME\"/agents/*.toml >/dev/null || { echo operator-reviewer-missing >&2; exit 43; }\n"
                "grep -l '^name = \"personal\"$' \"$CODEX_HOME\"/agents/*.toml >/dev/null || { echo personal-missing >&2; exit 47; }\n"
                "test -f \"$CODEX_HOME/hooks.json\" || { echo hooks-missing >&2; exit 44; }\n"
                "grep -q harmless_hook \"$CODEX_HOME/hooks.json\" || { echo hook-entry-missing >&2; exit 45; }\n"
                "test -z \"${SYMPHONY_PILOT_GITHUB_TOKEN:-}\" || { echo token-leaked >&2; exit 42; }\n"
                "printf '%s' \"$CODEX_HOME\" > \"$ROLE_PROBE\"\n"
                "printf '%s' \"$$\" > \"$PID_PROBE\"\n"
                "touch \"$ROLE_READY\"\n"
                "while [[ ! -e \"$ROLE_RELEASE\" ]]; do sleep 0.01; done\n"
                "exit 143\n", encoding="utf-8")
            fake.chmod(0o755)
            env = os.environ.copy()
            env.update({"CODEX_BIN": bash_path(fake), "CODEX_HOME": bash_path(original_home),
                        "SYMPHONY_PILOT_ROLE_POLICY_DIR": bash_path(role_source),
                        "SYMPHONY_PILOT_GITHUB_TOKEN": "must-not-reach-child",
                        "ROLE_PROBE": bash_path(probe),
                        "PID_PROBE": bash_path(pid_probe),
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
                lease = json.loads((repo / ".git" / "symphony-role-home.json").read_text())
                role_home = pathlib.Path(lease["path"])
                self.assertNotIn(str(repo), str(role_home))
                if os.name == "nt":
                    self.assertTrue(str(role_home).startswith("\\tmp\\") or
                                    str(lease["path"]).startswith("/tmp/"))
                else:
                    self.assertTrue(role_home.is_dir())
                if os.name == "nt":
                    (root / "release").touch()
                else:
                    child.terminate()
                result_code = child.wait(timeout=10)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
            if os.name == "nt":
                self.assertEqual(result_code, 143)
            else:
                self.assertLess(result_code, 0)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child.pid, 0)
            self.assertTrue(probe.exists())
            self.assertNotIn(str(repo), probe.read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(operator_reviewer.read_bytes()).hexdigest(),
                             operator_reviewer_digest)
            if os.name != "nt":
                self.assertEqual(int(pid_probe.read_text()), child.pid)
            self.assertFalse((repo / ".codex").exists())
            status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                    text=True, capture_output=True, check=True)
            self.assertEqual(status.stdout, "")
            if os.name == "nt":
                subprocess.run([bash, "-c", "rm -rf -- \"$1\"", "cleanup",
                                lease["path"]], check=True)
                (repo / ".git" / "symphony-role-home.json").unlink()
            else:
                self.assertTrue(pw.reconcile_role_home(repo))
                self.assertFalse(role_home.exists())
            self.assertFalse((repo / ".git" / "symphony-role-home.json").exists())

    def test_launcher_rejects_logical_role_name_collision(self):
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
            collision = repo / ".codex" / "agents"
            collision.mkdir(parents=True)
            (collision / "not-reviewer.toml").write_text(
                'name = "reviewer"\ndescription = "collision"\n'
                'developer_instructions = "collision"\n', encoding="utf-8")
            subprocess.run(["git", "add", ".codex"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "project agent"], cwd=repo, check=True)
            role_source = root / "role-source"
            role_source.mkdir()
            for policy in (ROOT / "workflow/agents").glob("*.toml"):
                shutil.copy2(policy, role_source / policy.name)
            result = subprocess.run(
                [bash, bash_path(ROOT / "runtime/launch_codex.sh")], cwd=repo,
                env={**os.environ, "CODEX_HOME": bash_path(root / "operator-codex"),
                     "SYMPHONY_PILOT_ROLE_POLICY_DIR": bash_path(role_source),
                     "CODEX_BIN": "false"}, text=True, capture_output=True)
            self.assertEqual(result.returncode, 78)
            self.assertIn("reserved Codex agent name collision", result.stderr)
            status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                    text=True, capture_output=True, check=True)
            self.assertEqual(status.stdout, "")

    def test_launcher_fails_closed_when_process_identity_is_unavailable(self):
        if os.name != "nt":
            self.skipTest("the host provides /proc process identity")
        bash = None
        for candidate in (r"C:\Program Files\Git\bin\bash.exe",
                          r"C:\Program Files\Git\usr\bin\bash.exe"):
            if pathlib.Path(candidate).exists():
                bash = candidate
                break
        if not bash:
            self.skipTest("Bash is unavailable")

        def bash_path(path):
            value = str(path)
            if len(value) > 2 and value[1] == ":":
                return "/" + value[0].lower() + value[2:].replace("\\", "/")
            return value

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "repo"
            repo.mkdir()
            git_repo(repo)
            original_home = root / "operator-codex"
            original_home.mkdir()
            role_source = root / "role-source"
            role_source.mkdir()
            for policy in (ROOT / "workflow/agents").glob("*.toml"):
                shutil.copy2(policy, role_source / policy.name)
            result = subprocess.run(
                [bash, bash_path(ROOT / "runtime/launch_codex.sh")], cwd=repo,
                env={**os.environ, "CODEX_HOME": bash_path(original_home),
                     "SYMPHONY_PILOT_ROLE_POLICY_DIR": bash_path(role_source),
                     "CODEX_BIN": "false", "TMPDIR": bash_path(repo)},
                text=True, capture_output=True)
            self.assertEqual(result.returncode, 78)
            self.assertIn("cannot establish App Server process identity", result.stderr)
            self.assertFalse((repo / ".git" / "symphony-role-home.json").exists())
            self.assertFalse((repo / ".codex").exists())

    def test_role_home_reconciliation_removes_exact_external_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory) / "repo"
            (repo / ".git").mkdir(parents=True)
            role_home = pathlib.Path(tempfile.mkdtemp(prefix="symphony-pilot-codex-home.fixture"))
            (role_home / "agents").mkdir()
            marker = repo / ".git" / "symphony-role-home.json"
            lease = {
                "schema": "symphony-pilot-role-home/v2", "lease_id": role_home.name,
                "path": str(role_home), "workspace": str(repo.resolve()),
                "owner": {"pid": 999, "boot_id": "boot", "start_time": "start"}}
            marker.write_text(json.dumps(lease), encoding="utf-8")
            (role_home / pw.ROLE_HOME_LEASE_FILE).write_text(json.dumps(lease), encoding="utf-8")
            with mock.patch.object(pw, "ROLE_HOME_ROOT", role_home.parent):
                self.assertTrue(pw.reconcile_role_home(repo))
            self.assertFalse(role_home.exists())
            self.assertFalse(marker.exists())

    def test_role_home_lease_cannot_cross_authorize_workspaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo_a = root / "A"
            repo_b = root / "B"
            (repo_a / ".git").mkdir(parents=True)
            (repo_b / ".git").mkdir(parents=True)
            role_home = pathlib.Path(tempfile.mkdtemp(prefix="symphony-pilot-codex-home.fixture"))
            lease = {"schema": "symphony-pilot-role-home/v2", "lease_id": role_home.name,
                     "path": str(role_home), "workspace": str(repo_b.resolve()),
                     "owner": {"pid": 999, "boot_id": "boot", "start_time": "start"}}
            (role_home / pw.ROLE_HOME_LEASE_FILE).write_text(json.dumps(lease), encoding="utf-8")
            marker = repo_a / ".git" / "symphony-role-home.json"
            marker.write_text(json.dumps({**lease, "workspace": str(repo_a.resolve())}), encoding="utf-8")
            with mock.patch.object(pw, "ROLE_HOME_ROOT", role_home.parent):
                with self.assertRaisesRegex(pw.PreparationError, "does not match its workspace marker"):
                    pw.reconcile_role_home(repo_a)
            self.assertTrue(role_home.exists())
            self.assertTrue(marker.exists())

    def test_active_role_home_owner_prevents_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "repo"
            (repo / ".git").mkdir(parents=True)
            role_home = pathlib.Path(tempfile.mkdtemp(prefix="symphony-pilot-codex-home.fixture"))
            lease = {"schema": "symphony-pilot-role-home/v2", "lease_id": role_home.name,
                     "path": str(role_home), "workspace": str(repo.resolve()),
                     "owner": {"pid": 999, "boot_id": "boot", "start_time": "start"}}
            (role_home / pw.ROLE_HOME_LEASE_FILE).write_text(json.dumps(lease), encoding="utf-8")
            (repo / ".git" / "symphony-role-home.json").write_text(json.dumps(lease), encoding="utf-8")
            with mock.patch.object(pw, "ROLE_HOME_ROOT", role_home.parent), \
                    mock.patch.object(pw, "process_identity_matches", return_value=True):
                with self.assertRaisesRegex(pw.PreparationError, "App Server is active"):
                    pw.reconcile_role_home(repo)
            self.assertTrue(role_home.exists())

    def test_pid_identity_mismatch_allows_stale_own_home_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "repo"
            (repo / ".git").mkdir(parents=True)
            role_home = pathlib.Path(tempfile.mkdtemp(prefix="symphony-pilot-codex-home.fixture"))
            lease = {"schema": "symphony-pilot-role-home/v2", "lease_id": role_home.name,
                     "path": str(role_home), "workspace": str(repo.resolve()),
                     "owner": {"pid": 999, "boot_id": "old-boot", "start_time": "old-start"}}
            (role_home / pw.ROLE_HOME_LEASE_FILE).write_text(json.dumps(lease), encoding="utf-8")
            (repo / ".git" / "symphony-role-home.json").write_text(json.dumps(lease), encoding="utf-8")
            with mock.patch.object(pw, "ROLE_HOME_ROOT", role_home.parent), \
                    mock.patch.object(pw, "process_identity_matches", return_value=False):
                self.assertTrue(pw.reconcile_role_home(repo))
            self.assertFalse(role_home.exists())

    def test_absent_role_home_clears_only_stale_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "repo"
            (repo / ".git").mkdir(parents=True)
            role_home = pathlib.Path(tempfile.mkdtemp(prefix="symphony-pilot-codex-home.fixture"))
            shutil.rmtree(role_home)
            marker = repo / ".git" / "symphony-role-home.json"
            lease = {"schema": "symphony-pilot-role-home/v2", "lease_id": role_home.name,
                     "path": str(role_home), "workspace": str(repo.resolve()),
                     "owner": {"pid": 999, "boot_id": "boot", "start_time": "start"}}
            marker.write_text(json.dumps(lease), encoding="utf-8")
            with mock.patch.object(pw, "ROLE_HOME_ROOT", role_home.parent), \
                    mock.patch.object(pw, "process_identity_matches", return_value=False):
                self.assertTrue(pw.reconcile_role_home(repo))
            self.assertFalse(marker.exists())

    def test_absent_role_home_with_live_owner_keeps_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "repo"
            (repo / ".git").mkdir(parents=True)
            role_home = pathlib.Path(tempfile.mkdtemp(prefix="symphony-pilot-codex-home.fixture"))
            shutil.rmtree(role_home)
            marker = repo / ".git" / "symphony-role-home.json"
            lease = {"schema": "symphony-pilot-role-home/v2", "lease_id": role_home.name,
                     "path": str(role_home), "workspace": str(repo.resolve()),
                     "owner": {"pid": 999, "boot_id": "boot", "start_time": "start"}}
            marker.write_text(json.dumps(lease), encoding="utf-8")
            with mock.patch.object(pw, "ROLE_HOME_ROOT", role_home.parent), \
                    mock.patch.object(pw, "process_identity_matches", return_value=True):
                with self.assertRaisesRegex(pw.PreparationError, "App Server is active"):
                    pw.reconcile_role_home(repo)
            self.assertTrue(marker.exists())

    def test_after_run_reconciles_role_home_before_tracker_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "workspace"
            (repo / ".git").mkdir(parents=True)
            role_home = pathlib.Path(tempfile.mkdtemp(prefix="symphony-pilot-codex-home.fixture"))
            marker = repo / ".git" / "symphony-role-home.json"
            marker.write_text(json.dumps({
                "schema": "symphony-pilot-role-home/v2", "lease_id": role_home.name,
                "path": str(role_home), "workspace": str(repo.resolve()),
                "owner": {"pid": 999, "boot_id": "boot", "start_time": "start"}}),
                encoding="utf-8")
            (role_home / pw.ROLE_HOME_LEASE_FILE).write_text(marker.read_text(), encoding="utf-8")
            with mock.patch.object(pw, "ROLE_HOME_ROOT", role_home.parent), \
                    mock.patch.object(after_run, "load_profile", return_value=make_profile(root)), \
                    mock.patch.object(after_run, "read_secret", return_value="redacted"), \
                    mock.patch.object(pw, "process_owns_workspace", return_value=False), \
                    mock.patch.object(pw, "process_identity_matches", return_value=False), \
                    mock.patch.object(sys, "argv", ["after_run", "--profile", "x",
                                                      "--workspace", str(repo)]):
                self.assertEqual(after_run.main(), 0)
            self.assertFalse(role_home.exists())
            self.assertFalse(marker.exists())

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

    def test_canary_requires_mechanical_isolation_evidence(self):
        operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
        self.assertIn("sentinel mutation", operations)
        self.assertIn("runtime denial/error", operations)
        self.assertIn("voluntary non-editing is not sandbox evidence", operations)

    def test_deployment_inventory_includes_all_generic_role_policies(self):
        self.assertEqual({path.stem for path in deploy.ROLE_POLICY_FILES},
                         deploy.EXPECTED_ROLE_NAMES)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/deploy.py"),
            "--project", "cleanroom", "--dry-run"],
            text=True, capture_output=True, check=True)
        data = json.loads(result.stdout)
        self.assertEqual(set(data["role_policies"]), deploy.EXPECTED_ROLE_NAMES)
        self.assertEqual(data["files"], 16)

    def test_deployed_test_requires_and_verifies_role_policy_files(self):
        source = (ROOT / "scripts/project.py").read_text(encoding="utf-8")
        for name in ("project-manager", "planner", "implementer", "reviewer", "adversary", "archivist"):
            self.assertIn(f'"{name}"', source)
        self.assertIn("deployed file does not match its manifest", source)

    def test_generated_deployment_coherence_gate_accepts_valid_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, target = self.deploy_fixture(pathlib.Path(directory))
            with mock.patch.object(project_control, "install_root", return_value=target):
                verification = project_control.verify_deployment(profile)
            self.assertEqual(verification["root"], target.resolve())
            manifest = json.loads((target / "DEPLOYMENT.json").read_text())
            self.assertEqual(verification["deployment_identity"], manifest["deployment_identity"])

    def test_start_can_proceed_only_after_real_deployment_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            selected, target = self.deploy_fixture(root)
            profile = self.profile_with(selected, workspace_root=root / "work",
                                        state_root=root / "state", log_root=root / "logs")
            child = mock.Mock(pid=123, poll=mock.Mock(return_value=None))
            identity = process_state()["identity"]
            with mock.patch.object(project_control, "install_root", return_value=target), \
                    mock.patch.object(project_control, "read_secret", return_value="test-token"), \
                    mock.patch.object(project_control, "active_issues", return_value=[]), \
                    mock.patch.object(project_control, "establish_awake_guard"), \
                    mock.patch.object(project_control, "resolve_symphony_binary", return_value="symphony"), \
                    mock.patch.object(project_control.subprocess, "Popen", return_value=child), \
                    mock.patch.object(project_control, "capture", return_value=identity), \
                    mock.patch.object(project_control, "dashboard", return_value={"state": "idle"}):
                self.assertEqual(project_control.start(profile), 0)
            state = json.loads((profile.state_root / "symphony.pid").read_text())
            manifest = json.loads((target / "DEPLOYMENT.json").read_text())
            self.assertEqual(state["deployment_identity"], manifest["deployment_identity"])
            self.assertEqual(state["deployed_profile_sha256"], manifest["profile_sha256"])

    def test_unrelated_registry_membership_does_not_invalidate_existing_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            profile, target = self.deploy_fixture(root)
            write_registry_profile(root / "registry", "beta", dashboard_port=4041)
            with mock.patch.object(project_control, "install_root", return_value=target):
                verification = project_control.verify_deployment(profile)
            self.assertEqual(verification["root"], target.resolve())

    def test_deployment_coherence_gate_rejects_missing_tampered_and_incompatible_snapshots(self):
        for case in ("missing_manifest", "tampered_workflow", "profile_mismatch", "contract_mismatch"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                profile, target = self.deploy_fixture(root)
                if case == "missing_manifest":
                    (target / "DEPLOYMENT.json").unlink()
                elif case == "tampered_workflow":
                    workflow = target / "projects" / profile.slug / "WORKFLOW.md"
                    workflow.write_text(workflow.read_text() + "tampered\n", encoding="utf-8")
                elif case == "profile_mismatch":
                    profile.source_profile_path.write_text(
                        profile.source_profile_path.read_text() + "display_name = \"changed\"\n",
                        encoding="utf-8",
                    )
                else:
                    with mock.patch.object(project_control, "install_root", return_value=target), \
                            mock.patch.object(project_control, "contract_digest", return_value="different"):
                        with self.assertRaisesRegex(pw.PreparationError, "contract differs"):
                            project_control.verify_deployment(profile)
                    continue
                with mock.patch.object(project_control, "install_root", return_value=target):
                    with self.assertRaises(pw.PreparationError):
                        project_control.verify_deployment(profile)

    def test_running_process_retains_deployment_endpoint_and_identity_across_source_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            profile = self.profile_with(make_profile(root), dashboard_port=4041)
            pid_path, _ = project_control.state_paths(profile)
            pid_path.write_text(json.dumps({
                "schema": "symphony-pilot-process/v2",
                "identity": process_state()["identity"],
                "deployment_root": "/home/duck-lint/.local/share/symphony-pilot/deployments/demo",
                "deployment_identity": "old-deployment",
                "deployed_profile_sha256": "old-profile",
                "dashboard_url": "http://127.0.0.1:4040",
            }) + "\n", encoding="utf-8")
            self.assertEqual(project_control._dashboard_url(profile), "http://127.0.0.1:4040")
            with mock.patch.object(project_control, "_identity_alive", return_value=True), \
                    mock.patch.object(project_control, "runtime_state_at", return_value={
                        "running": [], "retrying": []}) as runtime_state_at:
                self.assertEqual(project_control.status(profile), 0)
            runtime_state_at.assert_called_once_with("http://127.0.0.1:4040")
            with mock.patch.object(project_control, "_identity_alive", side_effect=[True, False]), \
                    mock.patch.object(project_control.os, "kill") as kill:
                self.assertEqual(project_control.stop(profile, force=True), 0)
            kill.assert_called_once_with(123, project_control.signal.SIGTERM)

    def test_invalid_registry_still_allows_recovery_status_and_stop_now(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry = root / "projects"
            write_registry_profile(registry, "alpha", "example/shared", dashboard_port=4040)
            write_registry_profile(registry, "beta", "example/shared", dashboard_port=4041)
            state_path = root / "alpha-state" / "symphony.pid"
            state_path.parent.mkdir()
            self.write_recovery_state(state_path)
            with mock.patch.object(project_control, "ROOT", root), \
                    mock.patch.object(project_control, "recovery_state_path", return_value=state_path), \
                    mock.patch.object(project_control, "_identity_alive", return_value=True), \
                    mock.patch.object(project_control, "runtime_state_at", return_value={"running": [], "retrying": []}), \
                    mock.patch.object(project_control, "read_secret") as read_secret, \
                    mock.patch.object(project_control, "github") as github, \
                    mock.patch.object(sys, "argv", ["project", "--project", "alpha", "status"]):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(project_control.main(), 0)
            self.assertIn("RECOVERY alpha", output.getvalue())
            read_secret.assert_not_called()
            github.assert_not_called()
            with mock.patch.object(project_control, "ROOT", root), \
                    mock.patch.object(project_control, "recovery_state_path", return_value=state_path), \
                    mock.patch.object(project_control, "_identity_alive", side_effect=[True, False]), \
                    mock.patch.object(project_control.os, "kill") as kill, \
                    mock.patch.object(project_control, "read_secret") as read_secret, \
                    mock.patch.object(project_control, "github") as github, \
                    mock.patch.object(sys, "argv", ["project", "--project", "alpha", "stop-now"]):
                self.assertEqual(project_control.main(), 0)
            kill.assert_called_once_with(123, project_control.signal.SIGTERM)
            read_secret.assert_not_called()
            github.assert_not_called()

    def test_removed_profile_still_allows_exact_recovery_stop_now(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry = root / "projects"
            write_registry_profile(registry, "beta", dashboard_port=4041)
            state_path = root / "alpha-state" / "symphony.pid"
            state_path.parent.mkdir()
            self.write_recovery_state(state_path)
            with mock.patch.object(project_control, "ROOT", root), \
                    mock.patch.object(project_control, "recovery_state_path", return_value=state_path), \
                    mock.patch.object(project_control, "_identity_alive", side_effect=[True, False]), \
                    mock.patch.object(project_control.os, "kill") as kill:
                with mock.patch.object(sys, "argv", ["project", "--project", "alpha", "stop-now"]):
                    self.assertEqual(project_control.main(), 0)
            kill.assert_called_once_with(123, project_control.signal.SIGTERM)

    def test_recovery_uses_persisted_dashboard_after_current_profile_port_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "symphony.pid"
            self.write_recovery_state(state_path, dashboard_port=4040)
            with mock.patch.object(project_control, "recovery_state_path", return_value=state_path), \
                    mock.patch.object(project_control, "_identity_alive", return_value=True), \
                    mock.patch.object(project_control, "runtime_state_at", return_value=None):
                state = project_control.read_recovery_state("alpha")
                self.assertEqual(project_control.recovery_status("alpha", state), 0)
            self.assertEqual(state["dashboard_url"], "http://127.0.0.1:4040")

    def test_recovery_rejects_reused_pid_and_malformed_state_without_killing(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "symphony.pid"
            self.write_recovery_state(state_path)
            with mock.patch.object(project_control, "recovery_state_path", return_value=state_path), \
                    mock.patch.object(project_control, "_identity_alive", return_value=False), \
                    mock.patch.object(project_control, "pid_alive", return_value=True), \
                    mock.patch.object(project_control.os, "kill") as kill:
                state = project_control.read_recovery_state("alpha")
                self.assertEqual(project_control.recovery_stop_now("alpha", state), 1)
            kill.assert_not_called()
            state_path.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(project_control, "recovery_state_path", return_value=state_path), \
                    mock.patch.object(project_control.os, "kill") as kill:
                with self.assertRaises(pw.PreparationError):
                    project_control.read_recovery_state("alpha")
            kill.assert_not_called()

    def test_invalid_registry_has_no_recovery_fallback_for_authority_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry = root / "projects"
            write_registry_profile(registry, "alpha", "example/shared", dashboard_port=4040)
            write_registry_profile(registry, "beta", "example/shared", dashboard_port=4041)
            for action in ("start", "test"):
                with mock.patch.object(project_control, "ROOT", root), \
                        mock.patch.object(sys, "argv", ["project", "--project", "alpha", action]):
                    self.assertEqual(project_control.main(), 78)
            with mock.patch.object(deploy, "ROOT", root), \
                    mock.patch.object(sys, "argv", ["deploy", "--project", "alpha", "--dry-run"]):
                self.assertEqual(deploy.main(), 78)
            with mock.patch.object(provision_secret, "ROOT", root), \
                    mock.patch.object(provision_secret.getpass, "getpass") as getpass:
                self.assertEqual(provision_secret.main(["--project", "alpha"]), 78)
            getpass.assert_not_called()

    def test_invalid_registry_without_process_state_fails_closed_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry = root / "projects"
            write_registry_profile(registry, "alpha", "example/shared", dashboard_port=4040)
            write_registry_profile(registry, "beta", "example/shared", dashboard_port=4041)
            missing_state = root / "missing" / "symphony.pid"
            with mock.patch.object(project_control, "ROOT", root), \
                    mock.patch.object(project_control, "recovery_state_path", return_value=missing_state), \
                    mock.patch.object(project_control, "_identity_alive") as identity_alive, \
                    mock.patch.object(project_control.os, "kill") as kill:
                for action in ("status", "stop-now"):
                    with mock.patch.object(sys, "argv", ["project", "--project", "alpha", action]):
                        self.assertEqual(project_control.main(), 78)
            identity_alive.assert_not_called()
            kill.assert_not_called()

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

    def test_recovery_archive_excludes_secret_named_paths_recursively(self):
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
            "--project", "cleanroom", "--dry-run"],
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
            (root / "deployment" / "projects" / profile.slug).mkdir(parents=True)
            (root / "deployment" / "projects" / profile.slug / "WORKFLOW.md").write_text("workflow\n")
            with mock.patch.object(project_control, "install_root", return_value=root / "deployment"), \
                    mock.patch.object(project_control, "verify_deployment", return_value={"root": root / "deployment", "deployment_identity": "test", "profile_sha256": "test"}), \
                    mock.patch.object(project_control, "resolve_symphony_binary", return_value="symphony"), \
                    mock.patch.object(project_control, "read_secret", return_value="secret"), \
                    mock.patch.object(project_control, "active_issues", return_value=[]), \
                    mock.patch.object(project_control, "establish_awake_guard"), \
                    mock.patch.object(project_control, "release_awake_guard") as release, \
                    mock.patch.object(project_control.subprocess, "Popen", side_effect=OSError("missing")):
                self.assertEqual(project_control.start(profile), 1)
            release.assert_called_once_with(profile)

    def test_start_verifies_deployment_before_secret_tracker_or_process(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = make_profile(pathlib.Path(directory))
            verification_error = pw.PreparationError("deployment", "tampered")
            with mock.patch.object(project_control, "verify_deployment", side_effect=verification_error), \
                    mock.patch.object(project_control, "read_secret") as read_secret, \
                    mock.patch.object(project_control, "active_issues") as active_issues, \
                    mock.patch.object(project_control.subprocess, "Popen") as popen:
                self.assertEqual(project_control.start(profile), 1)
            read_secret.assert_not_called()
            active_issues.assert_not_called()
            popen.assert_not_called()

    def test_start_identity_capture_failure_kills_unmanaged_child_and_retains_guard_if_needed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            profile = self.profile_with(make_profile(root), prevent_host_sleep=True)
            (root / "deployment" / "projects" / profile.slug).mkdir(parents=True)
            (root / "deployment" / "projects" / profile.slug / "WORKFLOW.md").write_text("workflow\n")
            child = mock.Mock(pid=123, poll=mock.Mock(return_value=None))
            child.wait.side_effect = [subprocess.TimeoutExpired("symphony", 5),
                                      subprocess.TimeoutExpired("symphony", 5)]
            with mock.patch.object(project_control, "install_root", return_value=root / "deployment"), \
                    mock.patch.object(project_control, "verify_deployment", return_value={"root": root / "deployment", "deployment_identity": "test", "profile_sha256": "test"}), \
                    mock.patch.object(project_control, "resolve_symphony_binary", return_value="symphony"), \
                    mock.patch.object(project_control, "read_secret", return_value="secret"), \
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
            (root / "deployment" / "projects" / profile.slug).mkdir(parents=True)
            (root / "deployment" / "projects" / profile.slug / "WORKFLOW.md").write_text("workflow\n")
            child = mock.Mock(pid=123, poll=mock.Mock(return_value=None))
            identity = process_state()["identity"]
            with mock.patch.object(project_control, "install_root", return_value=root / "deployment"), \
                    mock.patch.object(project_control, "verify_deployment", return_value={"root": root / "deployment", "deployment_identity": "test", "profile_sha256": "test"}), \
                    mock.patch.object(project_control, "resolve_symphony_binary", return_value="symphony"), \
                    mock.patch.object(project_control, "read_secret", return_value="secret"), \
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
            (root / "deployment" / "projects" / profile.slug).mkdir(parents=True)
            (root / "deployment" / "projects" / profile.slug / "WORKFLOW.md").write_text("workflow\n")
            child = mock.Mock(pid=123, poll=mock.Mock(return_value=None))
            identity = process_state()["identity"]
            with mock.patch.object(project_control, "install_root", return_value=root / "deployment"), \
                    mock.patch.object(project_control, "verify_deployment", return_value={"root": root / "deployment", "deployment_identity": "test", "profile_sha256": "test"}), \
                    mock.patch.object(project_control, "resolve_symphony_binary", return_value="symphony"), \
                    mock.patch.object(project_control, "read_secret", return_value="secret"), \
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

    def test_deployment_manifest_has_no_source_operator_claim(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/deploy.py"),
            "--project", "cleanroom", "--dry-run"],
            text=True, capture_output=True, check=True)
        self.assertIn('"source_commit"', result.stdout)
        self.assertNotIn("operator_cli", result.stdout)

    def test_source_deploy_rejects_arbitrary_install_root_and_uses_derived_destination(self):
        rejected = subprocess.run(
            [sys.executable, str(ROOT / "scripts/deploy.py"), "--project", "cleanroom",
             "--install-root", "C:/arbitrary", "--dry-run"],
            text=True, capture_output=True,
        )
        self.assertNotEqual(rejected.returncode, 0)
        selected = project_registry.resolve_project("cleanroom", ROOT / "projects")
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/deploy.py"), "--project", "cleanroom", "--dry-run"],
            text=True, capture_output=True, check=True,
        )
        self.assertEqual(json.loads(result.stdout)["install_root"],
                         str(deploy.selected_deployment(selected)))

    def test_registry_accepts_empty_one_and_arbitrary_n_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self.assertEqual(project_registry.validate_registry(root), ())
            write_registry_profile(root, "alpha")
            self.assertEqual([p.slug for p in project_registry.validate_registry(root)], ["alpha"])
            write_registry_profile(root, "beta")
            write_registry_profile(root, "gamma")
            self.assertEqual(
                [p.slug for p in project_registry.validate_registry(root)],
                ["alpha", "beta", "gamma"],
            )
            write_registry_profile(root, "delta")
            self.assertEqual(len(project_registry.validate_registry(root)), 4)
            (root / "beta" / "profile.toml").unlink()
            (root / "beta").rmdir()
            self.assertEqual(
                [p.slug for p in project_registry.validate_registry(root)],
                ["alpha", "delta", "gamma"],
            )

    def test_dashboard_port_allocation_is_explicit_and_not_a_slug_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            write_registry_profile(root, "project-12", dashboard_port=4040)
            write_registry_profile(root, "project-157", dashboard_port=4041)
            profiles = project_registry.validate_registry(root)
            self.assertEqual({p.slug: p.dashboard_port for p in profiles},
                             {"project-12": 4040, "project-157": 4041})
            self.assertEqual(project_registry.suggest_dashboard_port(root), 1024)
            write_registry_profile(root, "duplicate", dashboard_port=4040)
            with self.assertRaisesRegex(pw.PreparationError, "duplicate dashboard port"):
                project_registry.validate_registry(root)

    def test_dashboard_port_above_historical_range_is_valid_and_occupancy_is_not_renumbered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            write_registry_profile(root, "alpha", dashboard_port=5000)
            profile = project_registry.resolve_project("alpha", root)
            self.assertEqual(profile.dashboard_port, 5000)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
                occupied.bind(("127.0.0.1", 0))
                port = occupied.getsockname()[1]
                with self.assertRaisesRegex(pw.PreparationError, "occupied or unavailable"):
                    project_control.ensure_dashboard_port_available(port)

    def test_dashboard_allocator_fails_only_when_supported_domain_is_exhausted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for index, slug in enumerate(("alpha", "beta", "gamma")):
                write_registry_profile(root, slug, dashboard_port=6000 + index)
            with mock.patch.object(project_registry, "DASHBOARD_PORT_MIN", 6000), \
                    mock.patch.object(project_registry, "DASHBOARD_PORT_MAX", 6002):
                with self.assertRaisesRegex(pw.PreparationError, "range is exhausted"):
                    project_registry.suggest_dashboard_port(root)

    def test_dashboard_port_assignments_survive_registry_add_and_remove(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for index, slug in enumerate(("alpha", "beta", "gamma")):
                write_registry_profile(root, slug, dashboard_port=4040 + index)
            before = {p.slug: p.dashboard_port for p in project_registry.validate_registry(root)}
            write_registry_profile(root, "delta", dashboard_port=4043)
            after_add = {p.slug: p.dashboard_port for p in project_registry.validate_registry(root)}
            self.assertEqual({slug: after_add[slug] for slug in before}, before)
            (root / "beta" / "profile.toml").unlink()
            (root / "beta").rmdir()
            after_remove = {p.slug: p.dashboard_port for p in project_registry.validate_registry(root)}
            self.assertEqual({slug: after_remove[slug] for slug in ("alpha", "gamma", "delta")},
                             {"alpha": 4040, "gamma": 4042, "delta": 4043})

    def test_registry_rejects_directory_slug_and_repository_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = write_registry_profile(root, "alpha")
            path.write_text(path.read_text().replace('slug = "alpha"', 'slug = "beta"'), encoding="utf-8")
            with self.assertRaisesRegex(pw.PreparationError, "directory/slug mismatch"):
                project_registry.validate_registry(root)
            path.write_text(path.read_text().replace('slug = "beta"', 'slug = "alpha"'), encoding="utf-8")
            write_registry_profile(root, "beta", "example/alpha")
            with self.assertRaisesRegex(pw.PreparationError, "duplicate repository identity"):
                project_registry.validate_registry(root)

    def test_registry_rejects_service_port_and_namespace_collisions(self):
        alpha = make_profile(pathlib.Path("/tmp/alpha"))
        beta = self.profile_with(alpha, slug="beta", dashboard_port=alpha.dashboard_port)
        with self.assertRaisesRegex(pw.PreparationError, "duplicate dashboard port"):
            project_registry.validate_profiles((alpha, beta))
        beta = self.profile_with(beta, dashboard_port=4041,
                                 service_identity=alpha.service_identity)
        with self.assertRaisesRegex(pw.PreparationError, "duplicate service identity"):
            project_registry.validate_profiles((alpha, beta))
        beta = self.profile_with(beta, service_identity="symphony-pilot-beta")
        with mock.patch.object(project_registry, "project_namespaces", side_effect=[
                {"state": pathlib.Path("/tmp/project")},
                {"state": pathlib.Path("/tmp/project/subproject")},
        ]):
            with self.assertRaisesRegex(pw.PreparationError, "namespace overlap"):
                project_registry.validate_profiles((alpha, beta))

    def test_derived_namespaces_are_slug_scoped_and_profile_has_no_root_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            alpha_path = write_registry_profile(root, "alpha")
            alpha = pw.load_profile(alpha_path)
            beta_path = write_registry_profile(root, "beta")
            beta = pw.load_profile(beta_path)
            self.assertNotEqual(pw.deployment_path(alpha), pw.deployment_path(beta))
            self.assertNotEqual(alpha.workspace_root, beta.workspace_root)
            self.assertNotEqual(alpha.state_root, beta.state_root)
            self.assertNotEqual(alpha.service_identity, beta.service_identity)
            raw = alpha_path.read_text(encoding="utf-8")
            with self.assertRaisesRegex(pw.PreparationError, "unsupported profile fields"):
                bad = alpha_path.with_name("bad.toml")
                bad.write_text(raw + 'deployment_root = "/home/legacy"\n', encoding="utf-8")
                pw.load_profile(bad)

    def test_native_windows_never_guesses_wsl_home_from_windows_username(self):
        with mock.patch.object(pw.os, "name", "nt"), \
                mock.patch.dict(os.environ, {"USERNAME": "madis", "USER": "duck-lint",
                                              "WSL_USER": "duck-lint"}, clear=True):
            with self.assertRaisesRegex(pw.PreparationError, "WSL/Linux"):
                pw.resolve_host_root()
            symbolic = pw.host_namespace_root()
            self.assertEqual(str(symbolic), "<wsl-home>")
            self.assertNotIn("madis", str(symbolic))

    def test_native_windows_cannot_read_or_mutate_physical_project_namespace(self):
        profile = make_profile(pathlib.Path("C:/fixture"))
        symbolic = pw.Profile(**{**profile.__dict__,
                                 "state_root": pathlib.PurePosixPath("<wsl-home>/state")})
        with mock.patch.object(pw.os, "name", "nt"):
            with self.assertRaisesRegex(pw.PreparationError, "WSL/Linux"):
                project_control.state_paths(symbolic)

    def test_deployment_isolation_for_alpha_beta_gamma_and_new_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            profiles = {slug: write_registry_profile(root, slug)
                        for slug in ("alpha", "beta", "gamma")}
            source_commit = "a" * 40
            real_run = deploy.subprocess.run
            def fake_run(command, *args, **kwargs):
                if command[:3] == ["git", "rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(command, 0, source_commit + "\n", "")
                if command[:3] == ["git", "status", "--porcelain=v1"]:
                    return subprocess.CompletedProcess(command, 0, "", "")
                return real_run(command, *args, **kwargs)
            targets = {slug: root / "deployments" / slug for slug in profiles}
            with mock.patch.object(deploy.subprocess, "run", side_effect=fake_run):
                for slug, path in profiles.items():
                    deploy.deploy(path, targets[slug], False)
            generated_modules = sorted(path.stem for path in targets["alpha"].rglob("*.py"))
            module_env = os.environ.copy()
            module_env["PYTHONPATH"] = os.pathsep.join(
                [str(targets["alpha"] / "runtime"), str(targets["alpha"] / "scripts")]
            )
            subprocess.run(
                [sys.executable, "-c", "import " + ", ".join(generated_modules)],
                cwd=targets["alpha"], env=module_env, check=True,
                capture_output=True, text=True,
            )
            self.assertFalse((targets["alpha"] / "scripts" / "project.py").exists())
            manifest = json.loads((targets["alpha"] / "DEPLOYMENT.json").read_text(encoding="utf-8"))
            self.assertNotIn("operator_cli", manifest)
            self.assertEqual(len({str(path) for path in targets.values()}), 3)
            before = {slug: {str(p.relative_to(targets[slug])): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in targets[slug].rglob("*") if p.is_file()}
                      for slug in targets}
            (targets["beta"] / "bin").mkdir()
            (targets["beta"] / "bin" / "symphony-old").write_text("must not be inherited")
            with mock.patch.object(deploy.subprocess, "run", side_effect=fake_run):
                deploy.deploy(profiles["beta"], targets["beta"], False)
            after = {slug: {str(p.relative_to(targets[slug])): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in targets[slug].rglob("*") if p.is_file()}
                     for slug in ("alpha", "gamma")}
            self.assertEqual(before["alpha"], after["alpha"])
            self.assertEqual(before["gamma"], after["gamma"])
            self.assertFalse((targets["beta"] / "bin").exists())
            write_registry_profile(root, "delta")
            with mock.patch.object(deploy.subprocess, "run", side_effect=fake_run):
                deploy.deploy(root / "delta" / "profile.toml", root / "deployments" / "delta", False)
            self.assertTrue((root / "deployments" / "delta" / "DEPLOYMENT.json").exists())

    def test_tracker_rendering_is_repository_scoped_for_arbitrary_projects(self):
        from render_workflow import render
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            policy = root / "policy.md"
            policy.write_text("policy\n", encoding="utf-8")
            one = make_profile(root)
            two = self.profile_with(one, slug="other", repository="another/project")
            first = render(one, root / "one", policy)
            second = render(two, root / "two", policy)
            self.assertIn("repo: example/project", first)
            self.assertNotIn("another/project", first)
            self.assertIn("repo: another/project", second)
            self.assertEqual(first.count("symphony:auto"), 1)
            self.assertEqual(second.count("symphony:auto"), 1)

    def test_operator_resolution_has_no_implicit_cleanroom(self):
        missing = subprocess.run([sys.executable, str(ROOT / "scripts/project.py"), "status"],
                                 text=True, capture_output=True)
        self.assertNotEqual(missing.returncode, 0)
        self.assertNotIn("cleanroom", (missing.stdout + missing.stderr).lower())
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            standalone = root / "standalone.toml"
            standalone.write_text("slug = \"standalone\"\n", encoding="utf-8")
            for script, arguments in (
                    (ROOT / "scripts/project.py", ["--profile", str(standalone), "status"]),
                    (ROOT / "scripts/deploy.py", ["--profile", str(standalone), "--dry-run"]),
                    (ROOT / "scripts/validate_profile.py", ["--profile", str(standalone)]),
                    (ROOT / "scripts/provision_secret.py", ["--profile", str(standalone)])):
                rejected = subprocess.run([sys.executable, str(script), *arguments],
                                          text=True, capture_output=True)
                self.assertNotEqual(rejected.returncode, 0)
            registered = subprocess.run(
                [sys.executable, str(ROOT / "scripts/validate_profile.py"), "--project", "cleanroom"],
                text=True, capture_output=True, check=True,
            )
            self.assertIn("valid profile: cleanroom", registered.stdout)
            write_registry_profile(root, "alpha")
            write_registry_profile(root, "beta")
            alpha = project_registry.resolve_project("alpha", root)
            beta = project_registry.resolve_project("beta", root)
            self.assertNotEqual(project_control.install_root(alpha), project_control.install_root(beta))
            self.assertNotEqual(str(alpha.state_root), str(beta.state_root))

    def test_selected_project_fails_when_another_registry_member_collides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry = root / "projects"
            write_registry_profile(registry, "alpha", "example/shared")
            write_registry_profile(registry, "beta", "example/shared")
            with mock.patch.object(project_control, "ROOT", root), \
                    mock.patch.object(sys, "argv", ["project", "--project", "alpha", "status"]):
                self.assertEqual(project_control.main(), 78)
            with mock.patch.object(deploy, "ROOT", root), \
                    mock.patch.object(sys, "argv", ["deploy", "--project", "alpha", "--dry-run"]):
                self.assertEqual(deploy.main(), 78)

    def test_selected_operator_lifecycle_does_not_touch_unselected_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            alpha = make_profile(root / "alpha")
            beta = self.profile_with(make_profile(root / "beta"), slug="other",
                                     service_identity="symphony-pilot-other", dashboard_port=4041)
            beta_marker = beta.state_root / "unrelated.json"
            beta_marker.parent.mkdir(parents=True)
            beta_marker.write_text("beta-state\n", encoding="utf-8")
            with mock.patch.object(project_control, "_managed_identity", return_value=None), \
                    mock.patch.object(project_control, "release_awake_guard"):
                self.assertEqual(project_control.status(alpha), 0)
            self.assertEqual(beta_marker.read_text(encoding="utf-8"), "beta-state\n")
            with mock.patch.object(project_control, "_safe_pid", return_value=None), \
                    mock.patch.object(project_control, "release_awake_guard"):
                self.assertEqual(project_control.finish(alpha), 0)
            self.assertEqual(beta_marker.read_text(encoding="utf-8"), "beta-state\n")
            self.assertEqual(project_control.install_root(alpha).name, "demo")
            self.assertEqual(project_control.install_root(beta).name, "other")
            self.assertEqual(project_control.install_root(alpha).parent,
                             project_control.install_root(beta).parent)

    def test_official_binary_resolution_does_not_use_deployment_contents(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(project_control.shutil, "which", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "official Symphony"):
                project_control.resolve_symphony_binary()
        self.assertNotIn("glob(\"symphony-*\")", (ROOT / "scripts/project.py").read_text())

if __name__ == "__main__":
    unittest.main(verbosity=2)
