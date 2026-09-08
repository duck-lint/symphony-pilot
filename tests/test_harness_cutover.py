"""Focused Pilot contracts for the frozen harness boundary."""
from __future__ import annotations

import pathlib
import subprocess
import tempfile
import unittest
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "runtime"))

from control_db import ControlPlaneDatabase, StateConflict
from lifecycle import AllocationConflict, _next_role, _record_lifecycle_outcome, broker_writer_delta
from prepare_workspace import load_profile


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
            ("project-harness/plan",) if role == "PLANNER" else
            ("project-harness/archive",) if role == "ARCHIVIST" else ()
        )
        return self.database.authorize_dispatch(
            self.task["id"], lifecycle_id=self.lifecycle["id"], working_round_id=self.round["id"],
            planning_attempt_id=self.attempt["id"], role=role, expected_starting_head=SHA,
            read_scopes=("project",), write_scopes=write_scopes,
            registered_artifact_scopes=registered,
            protected_artifact_scopes=("project-harness/plan", "project-harness/archive"),
            created_at=TIME,
        )

    def evidence(self, grant, role, **extra):
        value = {
            "runtime_execution_id": "runtime-execution-1", "task_id": self.task["id"],
            "dispatch_id": grant["dispatch"]["id"], "observed_role": role, "status": "finished",
            "started_at": TIME, "finished_at": "2026-09-08T00:01:00+00:00", "head_sha": SHA,
            "starting_head": SHA, "changed_paths": [], "dirty": False,
            "result": {"outcome": "accepted", "summary": "observed"},
        }
        value.update(extra)
        return value

    def test_schema_contains_current_identities_and_no_architect(self):
        names = {row["name"] for row in self.database.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"lifecycles", "working_rounds", "planning_attempts", "role_dispatches", "capability_grants", "execution_evidence", "writer_deltas", "human_dispositions"} <= names)
        self.assertNotIn("ARCHITECT", self.database.connection.execute("SELECT sql FROM sqlite_master WHERE sql LIKE '%ARCHITECT%'").fetchall())

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
            )
        with self.assertRaises(StateConflict):
            self.database.record_execution_started(grant["dispatch"]["id"], self.evidence(grant, "PLANNER", status="running", phase="started"))

    def test_writer_grants_are_separate_and_git_is_denied(self):
        planner = self.dispatch("PLANNER", ("project-harness/plan",))
        self.assertEqual(planner["grant"]["write_scopes_json"], '["project-harness/plan"]')
        with self.assertRaises(StateConflict):
            self.dispatch("IMPLEMENTER", (".git",))
        with self.assertRaises(StateConflict):
            self.dispatch("ARCHIVIST", ("project-harness/plan", "project-harness/archive"))
        with self.assertRaises(StateConflict):
            self.dispatch("IMPLEMENTER", ("project-harness/plan",))

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
        accepted = self.database.record_mechanical_validation(
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
            planner = self.dispatch("PLANNER", ("project-harness/plan",))
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
        self.database.terminate_working_round_non_converged(self.round["id"], ended_at=TIME)
        with self.assertRaises(StateConflict):
            self.database.start_working_round(self.lifecycle["id"], started_at=TIME)

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

    def test_registered_project_harness_artifact_scopes_are_loaded(self):
        profile = load_profile(
            pathlib.Path(__file__).parents[1] / "projects" / "symphony-canary" / "profile.toml"
        )
        self.assertEqual(profile.harness_artifacts.planner, ("project-harness/plan", "project-harness/decision-memory"))
        self.assertEqual(profile.harness_artifacts.archivist, ("project-harness/archive",))

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
