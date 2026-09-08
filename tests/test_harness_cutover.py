"""Focused Pilot contracts for the frozen harness boundary."""
from __future__ import annotations

import pathlib
import tempfile
import unittest
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "runtime"))

from control_db import ControlPlaneDatabase, StateConflict
from lifecycle import _record_lifecycle_outcome


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
        return self.database.authorize_dispatch(
            self.task["id"], lifecycle_id=self.lifecycle["id"], working_round_id=self.round["id"],
            planning_attempt_id=self.attempt["id"], role=role, expected_starting_head=SHA,
            read_scopes=("project",), write_scopes=write_scopes, created_at=TIME,
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
            self.database.record_execution_evidence(grant["dispatch"]["id"], {"task_id": self.task["id"]})
        run = self.database.record_execution_evidence(grant["dispatch"]["id"], self.evidence(grant, "REVIEWER"))
        self.assertEqual(run["role"], "REVIEWER")
        self.assertIsNotNone(run["execution_evidence_id"])

    def test_runtime_cannot_change_role_or_add_writer_scope(self):
        grant = self.dispatch("REVIEWER")
        with self.assertRaises(StateConflict):
            self.database.authorize_dispatch(
                self.task["id"], lifecycle_id=self.lifecycle["id"], working_round_id=self.round["id"],
                planning_attempt_id=self.attempt["id"], role="REVIEWER", expected_starting_head=SHA,
                read_scopes=("project",), write_scopes=("src",),
            )
        with self.assertRaises(StateConflict):
            self.database.record_execution_evidence(grant["dispatch"]["id"], self.evidence(grant, "PLANNER"))

    def test_writer_grants_are_separate_and_git_is_denied(self):
        planner = self.dispatch("PLANNER", (".symphony/plan",))
        self.assertEqual(planner["grant"]["write_scopes_json"], '[".symphony/plan"]')
        with self.assertRaises(StateConflict):
            self.dispatch("IMPLEMENTER", (".git",))
        with self.assertRaises(StateConflict):
            self.dispatch("ARCHIVIST", (".symphony/plan", ".symphony/archive"))
        with self.assertRaises(StateConflict):
            self.dispatch("IMPLEMENTER", (".symphony/plan",))

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
