"""Focused Pilot contracts for the frozen harness boundary."""
from __future__ import annotations

import pathlib
import subprocess
import tempfile
import unittest
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "runtime"))

from control_db import ControlPlaneDatabase, StateConflict
from lifecycle import (AllocationConflict, LifecycleError, _handoff_inputs, _licensed_run,
                       _next_role, _record_lifecycle_outcome, _writer_scopes,
                       _working_tree_paths, broker_writer_delta,
                       issue_next_dispatch, perform_mechanical_validation,
                       reconcile_orphaned_executions)
from prepare_workspace import load_profile
from unittest.mock import patch


SHA = "a" * 40
TIME = "2026-09-08T00:00:00+00:00"


class HarnessCutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = ControlPlaneDatabase.open(pathlib.Path(self.temp.name) / "control.sqlite3")
        self.task = self.database.create_task(
            project_slug="demo", title="Task", objective="Objective", base_ref="main", base_sha=SHA,
            current_head=SHA, created_at=TIME,
        )
        self.database.queue_task(self.task["id"], project_slug="demo")
        self.lifecycle = self.database.create_lifecycle(self.task["id"], started_at=TIME)
        self.round = self.database.start_working_round(self.lifecycle["id"], started_at=TIME)
        self.attempt = self.database.start_planning_attempt(self.round["id"], started_at=TIME)

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def dispatch(self, role, write_scopes=()):
        registered = (
            ("harness/project-spec",) if role == "PLANNER" else
            ("harness/implementations",) if role == "ARCHIVIST" else ()
        )
        return self.database.authorize_dispatch(
            self.task["id"], lifecycle_id=self.lifecycle["id"], working_round_id=self.round["id"],
            planning_attempt_id=self.attempt["id"], role=role, expected_starting_head=SHA,
            read_scopes=("project",), write_scopes=write_scopes,
            registered_artifact_scopes=registered,
            protected_artifact_scopes=("harness/project-spec", "harness/implementations"),
            implementation_roots=("src",),
            created_at=TIME,
        )

    def evidence(self, grant, role, **extra):
        value = {
            "runtime_execution_id": "runtime-execution-1", "task_id": self.task["id"],
            "dispatch_id": grant["dispatch"]["id"], "observed_role": role, "status": "finished",
            "started_at": TIME, "finished_at": "2026-09-08T00:01:00+00:00", "head_sha": SHA,
            "starting_head": SHA, "changed_paths": [], "dirty": False,
            "runtime_process_id": "pilot-test-process", "app_server_thread_id": "pilot-test-thread",
            "result": {"outcome": "accepted", "summary": "observed"},
        }
        value.update(extra)
        return value

    def test_schema_contains_current_identities_and_no_architect(self):
        names = {row["name"] for row in self.database.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"lifecycles", "working_rounds", "planning_attempts", "role_dispatches", "capability_grants", "execution_evidence", "writer_deltas", "human_dispositions"} <= names)
        self.assertNotIn("ARCHITECT", self.database.connection.execute("SELECT sql FROM sqlite_master WHERE sql LIKE '%ARCHITECT%'").fetchall())

    def test_queued_task_can_issue_initial_pm_without_workspace(self):
        profile = type("Profile", (), {
            "slug": "demo", "state_root": pathlib.Path(self.temp.name) / "state",
            "harness_artifacts": type("Artifacts", (), {
                "planner": ("harness/project-spec",),
                "archivist": ("harness/implementations",),
                "implementation_roots": ("src",),
            })(),
        })()
        fresh_task = self.database.create_task(
            project_slug="demo", title="Fresh task", objective="Objective", base_ref="main", base_sha=SHA,
            current_head=SHA, created_at=TIME,
        )
        self.database.queue_task(fresh_task["id"], project_slug="demo")
        database_path = self.database.path
        self.database.close()
        with patch("lifecycle.control_database_path", return_value=database_path):
            dispatch = issue_next_dispatch(profile, fresh_task["id"])
        self.database = ControlPlaneDatabase.open(database_path)
        self.assertEqual(dispatch["role"], "PROJECT-MANAGER")
        self.assertEqual(dispatch["task_id"], fresh_task["id"])
        self.assertIsNone(dispatch["working_round_id"])
        self.assertEqual(dispatch["handoff_inputs"], [])

    def test_failed_execution_without_result_packet_is_retained(self):
        grant = self.dispatch("REVIEWER")
        launch = self.evidence(grant, "REVIEWER", status="running", phase="started")
        launch.pop("finished_at")
        run = self.database.record_execution_started(grant["dispatch"]["id"], launch)
        terminal = self.evidence(grant, "REVIEWER", status="failed", phase="terminated")
        terminal.pop("result")
        retained = self.database.record_execution_termination(grant["dispatch"]["id"], terminal)
        self.assertEqual(retained["id"], run["id"])
        self.assertEqual(retained["status"], "failed")
        self.assertIsNone(self.database.read_projection(self.task["id"])["active_execution"])

    def test_pilot_dispatch_carries_bounded_accepted_pm_handoff(self):
        pm = self.dispatch("PROJECT-MANAGER")
        self._finish_execution(pm, "PROJECT-MANAGER", outcome="accepted", runtime_id="pm-handoff")
        handoffs = _handoff_inputs(self.database, self.task, self.lifecycle, None, "PLANNER")
        self.assertEqual(handoffs[0]["kind"], "PM_HANDOFF")
        self.assertEqual(handoffs[0]["source_role"], "PROJECT-MANAGER")
        self.assertEqual(handoffs[0]["input"]["outcome"], "accepted")

    def test_role_run_requires_retained_bound_execution_evidence(self):
        grant = self.dispatch("REVIEWER")
        with self.assertRaises(StateConflict):
            self.database.record_execution_termination(grant["dispatch"]["id"], {"task_id": self.task["id"]})
        launch = self.evidence(grant, "REVIEWER", status="running", phase="started")
        launch.pop("finished_at")
        run = self.database.record_execution_started(grant["dispatch"]["id"], launch)
        self.assertEqual(run["status"], "running")
        active = self.database.read_projection(self.task["id"])["active_execution"]
        self.assertEqual(active["id"], run["id"])
        self.assertEqual(active["role"], "REVIEWER")
        with self.assertRaises(StateConflict):
            self.database.record_execution_termination(
                grant["dispatch"]["id"],
                self.evidence(grant, "REVIEWER", runtime_execution_id="stale-runtime", phase="terminated"),
            )
        run = self.database.record_execution_termination(grant["dispatch"]["id"], self.evidence(grant, "REVIEWER", phase="terminated"))
        self.assertEqual(run["role"], "REVIEWER")
        self.assertIsNotNone(run["execution_evidence_id"])
        self.assertEqual(run["status"], "finished")
        projection = self.database.read_projection(self.task["id"])
        self.assertIsNone(projection["active_execution"])

    def test_runtime_cannot_change_role_or_add_writer_scope(self):
        grant = self.dispatch("REVIEWER")
        with self.assertRaises(StateConflict):
            self.database.authorize_dispatch(
                self.task["id"], lifecycle_id=self.lifecycle["id"], working_round_id=self.round["id"],
                planning_attempt_id=self.attempt["id"], role="REVIEWER", expected_starting_head=SHA,
                read_scopes=("project",), write_scopes=("src",),
                implementation_roots=("src",),
            )
        with self.assertRaises(StateConflict):
            self.database.record_execution_started(grant["dispatch"]["id"], self.evidence(grant, "PLANNER", status="running", phase="started"))

    def test_writer_grants_are_separate_and_git_is_denied(self):
        planner = self.dispatch("PLANNER", ("harness/project-spec",))
        self.assertEqual(planner["grant"]["write_scopes_json"], '["harness/project-spec"]')
        with self.assertRaises(StateConflict):
            self.dispatch("IMPLEMENTER", (".git",))
        with self.assertRaises(StateConflict):
            self.dispatch("ARCHIVIST", ("harness/project-spec", "harness/implementations"))
        with self.assertRaises(StateConflict):
            self.dispatch("IMPLEMENTER", ("harness/project-spec",))

    def test_human_disposition_is_required_before_another_lifecycle(self):
        self.database.connection.execute("UPDATE lifecycles SET state = 'NON_CONVERGED', terminal_outcome = 'NON_CONVERGED', ended_at = ? WHERE id = ?", (TIME, self.lifecycle["id"]))
        self.database.connection.execute("UPDATE tasks SET state = 'TERMINATED' WHERE id = ?", (self.task["id"],))
        with self.assertRaises(StateConflict):
            self.database.create_lifecycle(self.task["id"])
        self.database.record_human_disposition(self.lifecycle["id"], decision="START_ANOTHER_LIFECYCLE", detail="operator requested another lifecycle", created_at=TIME)
        next_lifecycle = self.database.create_lifecycle(self.task["id"], started_at=TIME)
        self.assertEqual(next_lifecycle["ordinal"], 2)

    def test_projection_does_not_infer_execution_from_dispatch(self):
        grant = self.dispatch("ADVERSARY")
        projection = self.database.read_projection(self.task["id"])
        self.assertEqual(projection["role_runs"], [])
        self.assertEqual(projection["dispatches"][0]["id"], grant["dispatch"]["id"])
        self.assertEqual(projection["execution_evidence"], [])
        self.assertIsNone(projection["active_execution"])

    def test_pm_convergence_requires_distinct_mechanical_validation(self):
        _record_lifecycle_outcome(
            self.database, self.task, self.lifecycle, self.round,
            "PROJECT-MANAGER", {"outcome": "converged"}, SHA,
        )
        lifecycle = self.database.connection.execute(
            "SELECT * FROM lifecycles WHERE id = ?", (self.lifecycle["id"],)
        ).fetchone()
        self.assertEqual(lifecycle["convergence_status"], "RECORDED")
        self.assertEqual(lifecycle["mechanical_validation_status"], "NOT_RUN")
        self.assertEqual(lifecycle["mechanical_acceptance"], "NOT_REACHED")
        with self.assertRaises(AllocationConflict):
            _next_role(self.database, self.task)
        accepted = self.database._record_mechanical_validation(
            self.lifecycle["id"], head_sha=SHA, passed=True,
            evidence={"validator": "pilot", "result": "pass"}, validated_at=TIME,
        )
        self.assertEqual(accepted["mechanical_validation_status"], "PASSED")
        self.assertEqual(accepted["mechanical_acceptance"], "ACCEPTED")
        role, _, _, _ = _next_role(self.database, self.task)
        self.assertEqual(role, "ARCHIVIST")

    def _finish_execution(self, grant, role, *, outcome, verdict=None, runtime_id=None):
        runtime_id = runtime_id or f"runtime-{role.lower()}"
        launch = self.evidence(
            grant, role, runtime_execution_id=runtime_id,
            status="running", phase="started",
        )
        launch.pop("finished_at")
        run = self.database.record_execution_started(grant["dispatch"]["id"], launch)
        result = {"outcome": outcome, "summary": outcome}
        if verdict is not None:
            result["verdict"] = verdict
        terminal = self.evidence(
            grant, role, runtime_execution_id=runtime_id,
            phase="terminated", result=result,
        )
        run = self.database.record_execution_termination(grant["dispatch"]["id"], terminal)
        if role in {"PLANNER", "IMPLEMENTER", "ARCHIVIST"}:
            self.database.record_writer_delta(
                run["id"], changed_paths=[], workspace_head=SHA,
                authorization_status="ACCEPTED", dirty=False,
            )
        return run

    def test_round_eight_planning_exhaustion_does_not_start_round_nine(self):
        for ordinal in range(1, 8):
            if ordinal > 1:
                self.round = self.database.start_working_round(self.lifecycle["id"], started_at=TIME)
            if ordinal == 8:
                break
            self.database.terminate_working_round_non_converged(self.round["id"], ended_at=TIME)
        self.round = self.database.start_working_round(self.lifecycle["id"], started_at=TIME)
        self.attempt = self.database.start_planning_attempt(self.round["id"], started_at=TIME)
        for index in range(3):
            planner = self.dispatch("PLANNER", ("harness/project-spec",))
            self._finish_execution(planner, "PLANNER", outcome="accepted", runtime_id=f"planner-{index}")
            reviewer = self.dispatch("REVIEWER")
            self._finish_execution(
                reviewer, "REVIEWER", outcome="correction_required",
                verdict="correction_required", runtime_id=f"reviewer-{index}",
            )
            _record_lifecycle_outcome(
                self.database, self.task, self.lifecycle, self.round,
                "REVIEWER", {"outcome": "correction_required"}, SHA,
            )
            if index < 2:
                self.attempt = self.database.start_planning_attempt(self.round["id"], started_at=TIME)
        role, _, working, _ = _next_role(self.database, self.task)
        self.assertEqual(role, "ARCHIVIST")
        self.assertEqual(working["ordinal"], 8)
        lifecycle = self.database.connection.execute(
            "SELECT state, terminal_outcome FROM lifecycles WHERE id = ?", (self.lifecycle["id"],)
        ).fetchone()
        self.assertEqual(lifecycle["state"], "NON_CONVERGED")
        self.assertEqual(lifecycle["terminal_outcome"], "NON_CONVERGED")
        with self.assertRaises(StateConflict):
            self.database.start_working_round(self.lifecycle["id"], started_at=TIME)

    def test_non_converged_round_returns_through_pm_before_next_planner(self):
        _record_lifecycle_outcome(
            self.database, self.task, self.lifecycle, self.round,
            "PROJECT-MANAGER", {"outcome": "no_convergence"}, SHA,
        )
        role, _, working, _ = _next_role(self.database, self.task)
        self.assertEqual(role, "PROJECT-MANAGER")
        self.assertEqual(working["id"], self.round["id"])
        handoff = self.dispatch("PROJECT-MANAGER")
        self._finish_execution(
            handoff, "PROJECT-MANAGER", outcome="next_working_round",
            runtime_id="pm-handoff",
        )
        _record_lifecycle_outcome(
            self.database, self.task, self.lifecycle, self.round,
            "PROJECT-MANAGER", {"outcome": "next_working_round"}, SHA,
        )
        role, _, working, _ = _next_role(self.database, self.task)
        self.assertEqual(role, "PLANNER")
        self.assertIsNone(working)

    def test_host_writer_broker_uses_and_verifies_symphony_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = pathlib.Path(directory)
            def git(*args):
                return subprocess.run(
                    ["git", *args], cwd=workspace, check=True,
                    capture_output=True, text=True,
                ).stdout.strip()
            git("init", "-q")
            (workspace / "README").write_text("base\n", encoding="utf-8")
            subprocess.run(
                ["git", "-c", "user.name=Operator", "-c", "user.email=operator@example.com", "add", "README"],
                cwd=workspace, check=True, capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "-c", "user.name=Operator", "-c", "user.email=operator@example.com", "commit", "-qm", "base"],
                cwd=workspace, check=True, capture_output=True, text=True,
            )
            base = git("rev-parse", "HEAD")
            (workspace / "README").write_text("writer delta\n", encoding="utf-8")
            result = broker_writer_delta(
                workspace, "PLANNER", ["README"], ["README"],
                expected_starting_head=base,
            )
            self.assertEqual(result["commit_sha"], git("rev-parse", "HEAD"))
            self.assertEqual(
                git("show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce"),
                "Symphony Agent\x00symphony@localhost\x00Symphony Agent\x00symphony@localhost",
            )

    def test_implementer_proposal_is_narrowed_by_registered_roots(self):
        profile = load_profile(
            pathlib.Path(__file__).parents[1] / "projects" / "symphony-canary" / "profile.toml"
        )
        self.assertEqual(
            _writer_scopes(profile, "IMPLEMENTER", {"proposed_implementation_paths": ["lib/canary.py"]}),
            ["lib/canary.py"],
        )
        with self.assertRaises(LifecycleError):
            _writer_scopes(profile, "IMPLEMENTER", {"proposed_implementation_paths": ["tests/test_canary.py"]})
        with self.assertRaises(LifecycleError):
            _writer_scopes(profile, "IMPLEMENTER", {"proposed_implementation_paths": ["lib", "harness"]})

    def test_orphaned_running_execution_is_terminalized_without_success_license(self):
        grant = self.dispatch("REVIEWER")
        launch = self.evidence(grant, "REVIEWER", status="running", phase="started")
        launch.pop("finished_at")
        run = self.database.record_execution_started(grant["dispatch"]["id"], launch)
        database_path = self.database.path
        self.database.close()
        with patch("lifecycle.control_database_path", return_value=database_path):
            repaired = reconcile_orphaned_executions(
                type("Profile", (), {"slug": "demo"})(), managed_runtime_stopped=True,
            )
        self.database = ControlPlaneDatabase.open(database_path)
        orphan = self.database.read_role_run(run["id"])
        self.assertEqual(repaired[0]["status"], "failed")
        self.assertEqual(orphan["id"], run["id"])
        self.assertEqual(orphan["status"], "failed")
        projection = self.database.read_projection(self.task["id"])
        self.assertIsNone(projection["active_execution"])
        evidence = self.database.connection.execute(
            "SELECT status FROM execution_evidence WHERE role_run_id = ?", (run["id"],)
        ).fetchone()
        self.assertEqual(evidence["status"], "failed")
        with self.assertRaises(AllocationConflict):
            _licensed_run(self.database, orphan)

    def test_authorized_dispatch_orphan_reconciliation_does_not_invent_run(self):
        grant = self.dispatch("REVIEWER")
        database_path = self.database.path
        self.database.close()
        with patch("lifecycle.control_database_path", return_value=database_path):
            repaired = reconcile_orphaned_executions(
                type("Profile", (), {"slug": "demo"})(), managed_runtime_stopped=True,
            )
        self.database = ControlPlaneDatabase.open(database_path)
        self.assertEqual(repaired[0]["status"], "rejected")
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM role_runs").fetchone()[0], 0
        )

    def test_nul_porcelain_parses_both_rename_copy_paths_for_authorization(self):
        result = type("GitResult", (), {"returncode": 0, "stdout": "R  old.txt\x00new.txt\x00C  source.txt\x00copy.txt\x00", "stderr": ""})()
        with patch("lifecycle.run_git", return_value=result) as run_git:
            self.assertEqual(
                _working_tree_paths(pathlib.Path("unused")),
                ["copy.txt", "new.txt", "old.txt", "source.txt"],
            )
        self.assertIn("-z", run_git.call_args.args)
        with patch("lifecycle._working_tree_paths", return_value=["old.txt", "new.txt"]):
            with self.assertRaises(LifecycleError):
                broker_writer_delta(
                    pathlib.Path("unused"), "PLANNER", ["old.txt", "new.txt"], ["new.txt"],
                    expected_starting_head=SHA,
                )

    def test_mechanical_validation_computes_failure_and_records_evidence(self):
        _record_lifecycle_outcome(
            self.database, self.task, self.lifecycle, self.round,
            "PROJECT-MANAGER", {"outcome": "converged"}, SHA,
        )
        facts = type("Facts", (), {"task_uuid": self.task["id"]})()
        with patch("lifecycle.local_task_facts", return_value=(facts, {})), \
             patch("lifecycle.control_database_path", return_value=self.database.path), \
             patch("lifecycle._git", return_value=SHA), \
             patch("lifecycle._working_tree_paths", return_value=[]), \
             patch("lifecycle.verify_git_truth", return_value=SHA):
            result = perform_mechanical_validation(object(), pathlib.Path("unused"), lifecycle_id=self.lifecycle["id"])
        self.assertFalse(result["passed"])
        self.assertFalse(result["evidence"]["checks"]["required_lifecycle_executions_retained"])
        lifecycle = self.database.connection.execute(
            "SELECT mechanical_validation_status, mechanical_acceptance FROM lifecycles WHERE id = ?",
            (self.lifecycle["id"],),
        ).fetchone()
        self.assertEqual(lifecycle["mechanical_validation_status"], "FAILED")
        self.assertEqual(lifecycle["mechanical_acceptance"], "NOT_REACHED")

    def test_mechanical_validation_computes_pass_before_final_acceptance(self):
        pm = self.dispatch("PROJECT-MANAGER")
        self._finish_execution(pm, "PROJECT-MANAGER", outcome="accepted", runtime_id="pm-pass")
        planner = self.dispatch("PLANNER", ("harness/project-spec",))
        self._finish_execution(planner, "PLANNER", outcome="accepted", runtime_id="planner-pass")
        reviewer = self.dispatch("REVIEWER")
        self._finish_execution(reviewer, "REVIEWER", outcome="accepted", verdict="accepted", runtime_id="reviewer-pass")
        implementer = self.dispatch("IMPLEMENTER", ("src/module.py",))
        self._finish_execution(implementer, "IMPLEMENTER", outcome="accepted", runtime_id="implementer-pass")
        adversary = self.dispatch("ADVERSARY")
        self._finish_execution(adversary, "ADVERSARY", outcome="adversary_pass", runtime_id="adversary-pass")
        _record_lifecycle_outcome(
            self.database, self.task, self.lifecycle, self.round,
            "PROJECT-MANAGER", {"outcome": "converged"}, SHA,
        )
        facts = type("Facts", (), {"task_uuid": self.task["id"]})()
        profile = type("Profile", (), {
            "slug": "demo", "state_root": pathlib.Path(self.temp.name) / "state",
            "harness_artifacts": type("Artifacts", (), {
                "planner": ("harness/project-spec",),
                "archivist": ("harness/implementations",),
                "implementation_roots": ("src",),
            })(),
        })()
        with patch("lifecycle.local_task_facts", return_value=(facts, {})), \
             patch("lifecycle.control_database_path", return_value=self.database.path), \
             patch("lifecycle._git", return_value=SHA), \
             patch("lifecycle._working_tree_paths", return_value=[]), \
             patch("lifecycle.verify_git_truth", return_value=SHA):
            result = perform_mechanical_validation(profile, pathlib.Path("unused"), lifecycle_id=self.lifecycle["id"])
        self.assertTrue(result["passed"])
        self.assertEqual(result["next_dispatch"]["role"], "ARCHIVIST")
        self.assertTrue(all(result["evidence"]["checks"].values()))
        lifecycle = self.database.connection.execute(
            "SELECT mechanical_validation_status, mechanical_acceptance FROM lifecycles WHERE id = ?",
            (self.lifecycle["id"],),
        ).fetchone()
        self.assertEqual(lifecycle["mechanical_validation_status"], "PASSED")
        self.assertEqual(lifecycle["mechanical_acceptance"], "ACCEPTED")

    def test_registered_project_harness_artifact_scopes_are_loaded(self):
        profile = load_profile(
            pathlib.Path(__file__).parents[1] / "projects" / "symphony-canary" / "profile.toml"
        )
        self.assertEqual(profile.harness_artifacts.planner, ("harness/project-spec",))
        self.assertEqual(profile.harness_artifacts.archivist, ("harness/implementations",))
        self.assertEqual(profile.harness_artifacts.implementation_roots, ("lib",))
        canary = pathlib.Path(__file__).resolve().parents[2] / "symphony-canary"
        self.assertTrue((canary / "harness").is_dir())
        self.assertTrue((canary / "lib").is_dir())
        for relative in (*profile.harness_artifacts.planner, *profile.harness_artifacts.archivist, *profile.harness_artifacts.implementation_roots):
            self.assertTrue((canary / relative).exists(), relative)

    def test_planner_packet_cannot_skip_reviewer(self):
        _record_lifecycle_outcome(
            self.database, self.task, self.lifecycle, self.round, "PLANNER",
            {"outcome": "accepted"}, SHA,
        )
        state = self.database.connection.execute(
            "SELECT state FROM working_rounds WHERE id = ?", (self.round["id"],)
        ).fetchone()[0]
        self.assertEqual(state, "PLANNING")


if __name__ == "__main__":
    unittest.main()
