from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

import after_run
import before_run
import control_db
from tests.storage_support import queue_task
import prepare_workspace as pw
from prepare_workspace import Profile


class UnpublishedContinuationTests(unittest.TestCase):
    TASK_ID = "22222222-2222-2222-2222-222222222222"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.seed = self.root / "seed"
        self.remote = self.root / "remote.git"
        self.workspace = self.root / "work" / "T-000001"
        self.state = self.root / "state"
        self.profile_path = self.root / "profile.toml"
        self.profile_path.write_text("placeholder", encoding="utf-8")
        self.remote_ref_observations: list[tuple[str, list[str]]] = []
        self._run(self.root, "git", "init", "-q", "-b", "master", str(self.seed))
        self._git(self.seed, "config", "user.email", "step6@example.invalid")
        self._git(self.seed, "config", "user.name", "Step 6")
        (self.seed / "README").write_text("base\n", encoding="utf-8")
        self._git(self.seed, "add", "README")
        self._git(self.seed, "commit", "-qm", "base")
        self.base_sha = self._git(self.seed, "rev-parse", "HEAD")
        self._run(self.root, "git", "init", "-q", "--bare", str(self.remote))
        self._git(self.seed, "remote", "add", "origin", str(self.remote))
        self._git(self.seed, "push", "-q", "-u", "origin", "master")
        self._run(self.root, "git", "clone", "-q", "--no-single-branch", str(self.remote), str(self.workspace))
        self.profile = Profile(
            slug="demo", repository="example/demo", git_remote=str(self.remote),
            workspace_root=self.root / "work", state_root=self.state, log_root=self.root / "logs",
            secret_reference="unused", trusted_dispatchers=("duck-lint",), dispatch_labels=("auto",),
            blocked_label="human", service_identity="symphony-pilot-demo", dashboard_port=4040,
            max_concurrent_agents=1, max_turns=1, poll_interval_ms=1000, max_retry_backoff_ms=1000,
            codex_model="model", codex_reasoning_effort="high", toolchain=None,
        )
        with control_db.open_database(self.root / "control.sqlite3") as database:
            task = database.create_task(
                project_slug="demo", title="Unpublished continuation", objective="Synthetic hook cadence",
                base_ref="master", base_sha=self.base_sha, task_id=self.TASK_ID,
                identifier="T-000001", created_at="2026-09-04T12:00:00+00:00",
            )
            queue_task(database, task)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, cwd: pathlib.Path, *args: str) -> str:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def _git(self, cwd: pathlib.Path, *args: str) -> str:
        return self._run(cwd, "git", *args)

    def _task(self) -> dict[str, object]:
        with control_db.open_database(self.root / "control.sqlite3") as database:
            return database.read_task(self.TASK_ID)

    def _projection(self) -> dict[str, object]:
        with control_db.open_database(self.root / "control.sqlite3") as database:
            return database.read_projection(self.TASK_ID)

    def _remote_refs(self) -> list[str]:
        return self._git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines()

    def _observe_remote_refs(self, label: str) -> list[str]:
        refs = self._remote_refs()
        self.remote_ref_observations.append((label, refs))
        return refs

    def _before(self) -> int:
        with mock.patch.object(before_run, "load_profile", return_value=self.profile):
            return before_run.main(["--profile", str(self.profile_path), "--workspace", str(self.workspace)])

    def _after(self) -> int:
        with mock.patch.object(after_run, "load_profile", return_value=self.profile):
            return after_run.main(["--profile", str(self.profile_path), "--workspace", str(self.workspace)])

    def test_exact_local_unpublished_continuation_succeeds(self):
        with control_db.open_database(self.root / "control.sqlite3") as database:
            database.update_heads(self.TASK_ID, current_head=self.base_sha)
        task = self._task()
        self._git(self.workspace, "switch", "-q", "-C", task["branch"], self.base_sha)
        self.assertEqual(pw.prepare(self.profile, self.workspace), None)
        self.assertEqual(self._git(self.workspace, "rev-parse", "HEAD"), self.base_sha)
        self.assertEqual(self._remote_refs(), ["refs/heads/master"])

    def test_divergent_local_head_is_preserved_and_blocked(self):
        with control_db.open_database(self.root / "control.sqlite3") as database:
            database.update_heads(self.TASK_ID, current_head=self.base_sha)
        task = self._task()
        self._git(self.workspace, "switch", "-q", "-C", task["branch"], self.base_sha)
        (self.workspace / "divergence").write_text("B\n", encoding="utf-8")
        self._git(self.workspace, "add", "divergence")
        self._git(self.workspace, "config", "user.email", "step6@example.invalid")
        self._git(self.workspace, "config", "user.name", "Step 6")
        self._git(self.workspace, "commit", "-qm", "unlicensed local evidence")
        divergent = self._git(self.workspace, "rev-parse", "HEAD")
        with self.assertRaises(pw.PreparationError):
            pw.prepare(self.profile, self.workspace)
        self.assertEqual(self._git(self.workspace, "rev-parse", "HEAD"), divergent)
        self.assertEqual(self._git(self.workspace, "branch", "--show-current"), task["branch"])
        self.assertEqual(self._task()["current_head"], self.base_sha)
        self.assertEqual(self._projection()["blockers"][0]["kind"], "infrastructure")

    def test_wrong_branch_fails_without_repair(self):
        with control_db.open_database(self.root / "control.sqlite3") as database:
            database.update_heads(self.TASK_ID, current_head=self.base_sha)
        task = self._task()
        self._git(self.workspace, "switch", "-q", "-C", "wrong-branch", self.base_sha)
        with self.assertRaises(pw.PreparationError):
            pw.prepare(self.profile, self.workspace)
        self.assertEqual(self._git(self.workspace, "branch", "--show-current"), "wrong-branch")
        self.assertEqual(self._task()["current_head"], self.base_sha)

    def test_lost_workspace_does_not_manufacture_current_head(self):
        task = self._task()
        self._git(self.workspace, "switch", "-q", "-C", task["branch"], self.base_sha)
        (self.workspace / "unpublished").write_text("B\n", encoding="utf-8")
        self._git(self.workspace, "add", "unpublished")
        self._git(self.workspace, "config", "user.email", "step6@example.invalid")
        self._git(self.workspace, "config", "user.name", "Step 6")
        self._git(self.workspace, "commit", "-qm", "unpublished continuation")
        current_head = self._git(self.workspace, "rev-parse", "HEAD")
        with control_db.open_database(self.root / "control.sqlite3") as database:
            database.update_heads(self.TASK_ID, current_head=current_head)
        # Simulate loss of the retained workspace: a fresh clone contains the
        # registered base branch and object history only, not the unpublished B.
        import shutil
        def remove_readonly(function, path, _excinfo):
            os.chmod(path, stat.S_IWRITE)
            function(path)
        shutil.rmtree(self.workspace, onerror=remove_readonly)
        self._run(self.root, "git", "clone", "-q", "--no-single-branch", str(self.remote), str(self.workspace))
        with self.assertRaises(pw.PreparationError):
            pw.prepare(self.profile, self.workspace)
        self.assertEqual(self._task()["current_head"], current_head)
        self.assertEqual(self._projection()["blockers"][0]["kind"], "infrastructure")

    def test_initial_base_fast_forward_starts_recorded_base(self):
        (self.seed / "README").write_text("base-forwarded\n", encoding="utf-8")
        self._git(self.seed, "add", "README")
        self._git(self.seed, "commit", "-qm", "base fast-forward")
        self._git(self.seed, "push", "-q", "origin", "master")
        task = self._task()
        self.assertIsNone(task["current_head"])
        self.assertEqual(pw.prepare(self.profile, self.workspace), None)
        self.assertEqual(self._git(self.workspace, "rev-parse", "HEAD"), self.base_sha)
        self.assertEqual(self._git(self.workspace, "branch", "--show-current"), task["branch"])


if __name__ == "__main__":
    unittest.main()
