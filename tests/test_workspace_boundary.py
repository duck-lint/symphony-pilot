"""Actual Git execution and outside-workspace sentinel regression probes."""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from tests import lifecycle_test_support as fixtures
import control_db
import lifecycle
import prepare_workspace as pw
import workspace_boundary as boundary


class WorkspaceBoundaryTests(unittest.TestCase):
    def test_task_workspace_is_created_unprivileged_and_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = pathlib.Path(directory) / "symphony-workspaces"
            pool.mkdir()
            project_root = pool / "demo"
            workspace = boundary.create_empty_task_workspace(project_root, "T-000001")
            self.assertEqual(workspace, project_root / "T-000001")
            self.assertTrue(workspace.is_dir())
            self.assertEqual(list(workspace.iterdir()), [])
            if os.name != "nt":
                self.assertEqual(workspace.stat().st_mode & 0o077, 0)
            self.assertEqual(boundary.create_empty_task_workspace(project_root, "T-000001"), workspace)
            (workspace / "unexpected").write_text("evidence", encoding="utf-8")
            with self.assertRaises(boundary.WorkspaceBoundaryError):
                boundary.create_empty_task_workspace(project_root, "T-000001")

    def test_reclaim_task_workspace_removes_only_the_exact_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = pathlib.Path(directory) / "symphony-workspaces"
            task = pool / "demo" / "T-000001"
            (task / "nested").mkdir(parents=True)
            (task / "nested" / "file").write_text("task", encoding="utf-8")
            sentinel = pool / "demo" / "sentinel"
            sentinel.write_text("retain", encoding="utf-8")

            reclaimed = boundary.reclaim_task_workspace(pool, "demo", "T-000001")

            self.assertEqual(reclaimed, task)
            self.assertFalse(task.exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "retain")

    def test_reclaim_rejects_symlink_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = pathlib.Path(directory) / "symphony-workspaces"
            task = pool / "demo" / "T-000001"
            task.mkdir(parents=True)
            outside = pathlib.Path(directory) / "outside"
            outside.mkdir()
            try:
                (task / "escape").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable on this host")
            with self.assertRaisesRegex(boundary.WorkspaceBoundaryError, "symlink"):
                boundary.reclaim_task_workspace(pool, "demo", "T-000001")
            self.assertTrue(task.exists())
            self.assertTrue(outside.exists())

    def test_reclaim_rejects_mounted_descendant_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = pathlib.Path(directory) / "symphony-workspaces"
            mounted = pool / "demo" / "T-000001" / "mounted"
            mounted.mkdir(parents=True)
            (mounted / "keep").write_text("keep", encoding="utf-8")
            with mock.patch.object(boundary.os.path, "ismount",
                                   side_effect=lambda path: pathlib.Path(path).name == "mounted"):
                with self.assertRaisesRegex(boundary.WorkspaceBoundaryError, "mountpoint|filesystem boundary"):
                    boundary.reclaim_task_workspace(pool, "demo", "T-000001")
            self.assertTrue(mounted.exists())

    def test_reclaim_rejects_process_owned_workspace_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = pathlib.Path(directory) / "symphony-workspaces"
            task = pool / "demo" / "T-000001"
            task.mkdir(parents=True)
            (task / "keep").write_text("keep", encoding="utf-8")
            with mock.patch.object(boundary, "_workspace_is_process_owned", return_value=True):
                with self.assertRaisesRegex(boundary.WorkspaceBoundaryError, "live process"):
                    boundary.reclaim_task_workspace(pool, "demo", "T-000001")
            self.assertTrue(task.exists())

    def case(self):
        case = fixtures.Step6LifecycleTests("runTest")
        case.setUp()
        self.addCleanup(case.tearDown)
        return case

    def planned(self, case):
        case._reach_planned()

    def blocked(self, case, attempt, *, kind="project"):
        role_name = attempt["packet"]["role"]
        finding = case._finding(role_name, "unresolved project decision", blocker_kind=kind)
        role = case._role(role_name, "BLOCKED")
        role["findings"] = [finding]
        case._write_result(attempt, case._result(attempt, "blocked", roles=[role]))

    def projection(self, case):
        with control_db.open_database(case.database_path) as db:
            return db.read_projection(case.TASK_ID)

    def test_blocked_implementer_unchanged_head(self):
        for kind in ("human", "project"):
            with self.subTest(kind=kind):
                case = self.case()
                self.planned(case)
                attempt = case._attempt()
                self.blocked(case, attempt, kind=kind)
                lifecycle.reconcile(case.profile, case.workspace)
                projection = self.projection(case)
                self.assertEqual(projection["task"]["state"], "PLANNED")
                self.assertIsNone(projection["task"]["current_head"])
                self.assertEqual(projection["blockers"][0]["kind"], kind)

    def test_dirty_blocked_implementation_becomes_infrastructure_blocked(self):
        case = self.case()
        self.planned(case)
        attempt = case._attempt()
        (case.workspace / "dirty").write_text("dirty\n")
        self.blocked(case, attempt)
        import after_run
        with mock.patch.object(after_run, "load_profile", return_value=case.profile):
            self.assertEqual(after_run.main(["--profile", str(case.profile_path),
                                            "--workspace", str(case.workspace)]), 78)
        projection = self.projection(case)
        self.assertEqual(projection["blockers"][0]["kind"], "infrastructure")
        self.assertEqual(projection["task"]["state"], "PLANNED")

    def poison(self, case, kind, sentinel):
        command = "printf executed > " + shlex.quote(sentinel.as_posix())
        script = case.workspace / ".git" / "sentinel-command"
        script.write_text("#!/bin/sh\n" + command + "\n", encoding="utf-8")
        script.chmod(0o755)
        executable = shlex.quote(script.as_posix())
        if kind == "hook":
            hook = case.workspace / ".git/hooks/post-checkout"
            hook.write_text("#!/bin/sh\n" + command + "\n", encoding="utf-8")
            hook.chmod(0o755)
        elif kind in {"include", "includeIf"}:
            included = case.root / "included.config"
            included.write_text('[core]\n fsmonitor = "' + script.as_posix() + '"\n')
            key = "include.path" if kind == "include" else "includeIf.gitdir:**/.git.path"
            case._git("config", key, str(included))
        elif kind == "filter":
            (case.workspace / ".gitattributes").write_text("README filter=evil\n")
            case._git("add", ".gitattributes")
            case._git("commit", "-qm", "attributes")
            case._git("config", "filter.evil.clean", executable)
            case._git("config", "filter.evil.smudge", executable)
            case._git("config", "filter.evil.process", executable)
        elif kind == "rewrite":
            case._git("config", "url.ext::" + executable + ".insteadOf", case.profile.git_remote)
            case._git("config", "protocol.ext.allow", "always")
        else:
            case._git("config", kind, executable)

    def test_hostile_git_config_never_executes_during_preparation_or_reconciliation(self):
        for kind in ("hook", "core.fsmonitor", "core.hooksPath", "filter", "include", "includeIf",
                     "rewrite", "credential.helper", "core.sshCommand", "diff.external",
                     "merge.evil.driver"):
            for phase in ("prepare", "reconcile"):
                with self.subTest(kind=kind, phase=phase):
                    case = self.case()
                    attempt = case._attempt()
                    sentinel = case.root / "outside-sentinel"
                    self.poison(case, kind, sentinel)
                    if phase == "prepare":
                        if kind == "hook":
                            pw.prepare(case.profile, case.workspace)
                        else:
                            with self.assertRaises(pw.PreparationError):
                                pw.prepare(case.profile, case.workspace)
                            self.assertEqual(self.projection(case)["blockers"][0]["kind"], "infrastructure")
                    else:
                        case._write_result(attempt, case._result(attempt, "blocked", findings=[
                            case._finding("ARCHITECT", "infrastructure condition", blocker_kind="infrastructure")]))
                        import after_run
                        with mock.patch.object(after_run, "load_profile", return_value=case.profile):
                            result = after_run.main(["--profile", str(case.profile_path),
                                                     "--workspace", str(case.workspace)])
                        self.assertEqual(result, 0 if kind == "hook" else 78)
                        self.assertEqual(self.projection(case)["blockers"][0]["kind"], "infrastructure")
                    self.assertFalse(sentinel.exists(), (kind, phase))

    def test_safe_metadata_writes_replace_leaf_links_without_touching_outside(self):
        case = self.case()
        sentinel = case.root / "outside"
        sentinel.write_text("unchanged")
        git_dir = case.workspace / ".git"
        try:
            (git_dir / "symphony-preparation.json.tmp").symlink_to(sentinel)
            (git_dir / "symphony-toolchain.env").symlink_to(sentinel)
        except OSError:
            self.skipTest("symlink creation unavailable")
        facts, _ = pw.local_task_facts(case.profile, case.workspace)
        pw.marker(case.profile, case.workspace, facts, {})
        profile = dataclasses.replace(case.profile, toolchain="rust")
        with mock.patch.object(pw, "find_tool", return_value="/usr/bin/cargo"), \
             mock.patch.object(pw.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            pw.prepare_toolchain(profile, case.workspace, facts.identifier)
        self.assertEqual(sentinel.read_text(), "unchanged")
        self.assertFalse((git_dir / "symphony-toolchain.env").is_symlink())
        self.assertEqual(json.loads((git_dir / "symphony-preparation.json").read_text())["task_uuid"], case.TASK_ID)

    def test_unsafe_parent_and_unknown_task_fail_without_inventing_identity(self):
        case = self.case()
        unknown = case.workspace.with_name("T-000099")
        with self.assertRaises(pw.PreparationError):
            pw.prepare(case.profile, unknown)
        self.assertEqual(self.projection(case)["blockers"], [])
        with self.assertRaises((OSError, boundary.WorkspaceBoundaryError)):
            boundary.atomic_metadata_write(case.root / "missing/file", "content")
        missing_database = case.root / "missing-database.sqlite3"
        with mock.patch.object(pw, "control_database_path", return_value=missing_database):
            with self.assertRaises(pw.PreparationError):
                pw.prepare(case.profile, case.workspace)
        self.assertFalse(missing_database.exists())

    def test_hostile_git_parent_blocks_known_task_before_git_runs(self):
        case = self.case()
        original = case.workspace / ".git"
        outside = case.root / "outside-git"
        original.rename(outside)
        try:
            original.symlink_to(outside, target_is_directory=True)
        except OSError:
            outside.rename(original)
            self.skipTest("directory symlink creation unavailable")
        with mock.patch.object(boundary.subprocess, "run", side_effect=AssertionError("Git must not run")):
            with self.assertRaises(pw.PreparationError):
                pw.prepare(case.profile, case.workspace)
        self.assertEqual(self.projection(case)["blockers"][0]["kind"], "infrastructure")
        with self.assertRaises(boundary.WorkspaceBoundaryError):
            boundary.atomic_metadata_write(original / "symphony-preparation.json", "{}")
        self.assertFalse((outside / "symphony-preparation.json").exists())

    def test_actual_policy_mutations_require_redeployment_but_docs_do_not(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import deploy
        import project
        import deployment_contract
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            for relative in (*deployment_contract.CONTRACT_FILES,
                             *deployment_contract.DEPLOYED_OPERATOR_FILES,
                             "projects/symphony-canary/profile.toml"):
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            profile_path = source / "projects/symphony-canary/profile.toml"
            profile = pw.load_profile(profile_path)
            def git_result(args, **kwargs):
                return subprocess.CompletedProcess(args, 0, "a" * 40 if args[1] == "rev-parse" else "", "")
            with mock.patch.object(deploy, "ROOT", source), mock.patch.object(project, "ROOT", source), \
                 mock.patch.object(deploy.subprocess, "run", side_effect=git_result):
                for index, relative in enumerate(("workflow/architect_policy.md", "workflow/agents/reviewer.toml",
                                                   "runtime/launch_codex.sh")):
                    target = deploy.deploy(profile_path, root / f"deployment-{index}", False)
                    with mock.patch.object(project, "install_root", return_value=target):
                        project.verify_deployment(profile)
                        (source / "README.md").write_text("ordinary documentation mutation\n")
                        project.verify_deployment(profile)
                        changed = source / relative
                        changed.write_bytes(changed.read_bytes() + b"\n# constitutive mutation\n")
                        with self.assertRaisesRegex(pw.PreparationError, "contract differs"):
                            project.verify_deployment(profile)
                        deploy.deploy(profile_path, target, False)
                        project.verify_deployment(profile)


if __name__ == "__main__":
    unittest.main()
