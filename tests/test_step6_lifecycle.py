from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "runtime"))

import control_db
from lifecycle import (RESULT_SCHEMA, LifecycleError, prepare_attempt, read_result,
                       reconcile)
from prepare_workspace import Profile


class Step6LifecycleTests(unittest.TestCase):
    BASE_SHA = "a" * 40
    TASK_ID = "11111111-1111-1111-1111-111111111111"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.workspace = self.root / "work" / "T-000001"
        self.workspace.mkdir(parents=True)
        self.profile_path = self.root / "profile.toml"
        self.profile_path.write_text("placeholder", encoding="utf-8")
        self.profile = Profile(
            slug="demo", repository="example/demo", git_remote=str(self.workspace),
            workspace_root=self.root / "work", state_root=self.root / "state", log_root=self.root / "logs",
            secret_reference="unused", trusted_dispatchers=("duck-lint",), dispatch_labels=("auto",),
            blocked_label="human", service_identity="symphony-pilot-demo", dashboard_port=4040,
            max_concurrent_agents=1, max_turns=8, poll_interval_ms=1000, max_retry_backoff_ms=1000,
            codex_model="model", codex_reasoning_effort="high", toolchain=None,
        )
        self.database_path = self.root / "control.sqlite3"
        self.database = control_db.open_database(self.database_path)
        self._git("init", "-q", "-b", "master")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Step 6 test")
        (self.workspace / "README").write_text("base\n", encoding="utf-8")
        self._git("add", "README")
        self._git("commit", "-qm", "base")
        base_sha = self._git("rev-parse", "HEAD")
        self._git("remote", "add", "origin", str(self.workspace))
        task = self.database.create_task(
            project_slug="demo", title="Lifecycle task", objective="Synthetic lifecycle",
            base_ref="master", base_sha=base_sha, task_id=self.TASK_ID,
            identifier="T-000001", created_at="2026-09-04T12:00:00+00:00",
        )
        self.database.queue_task(task["id"], project_slug="demo")
        self.database.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=self.workspace, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def _attempt(self) -> dict[str, object]:
        self.database = control_db.open_database(self.database_path)
        task = self.database.read_task(self.TASK_ID)
        branch = task["branch"]
        self._git("switch", "-q", "-C", branch)
        self.database.close()
        marker = self.workspace / ".git" / "symphony-preparation.json"
        if not marker.exists():
            marker.write_text(json.dumps({"schema": "symphony-pilot-preparation/v3"}), encoding="utf-8")
        return prepare_attempt(self.profile, self.workspace)

    def _result(self, attempt: dict[str, object], outcome: str, *, roles=None, findings=None,
                resolved=None) -> dict[str, object]:
        packet = attempt["packet"]
        task = packet
        return {
            "schema": RESULT_SCHEMA, "task_uuid": task["task_uuid"], "identifier": task["identifier"],
            "architect_role_run_id": task["architect_role_run_id"], "expected_state": task["current_state"],
            "expected_workpad_version": task["workpad"]["version"],
            "expected_starting_head": task["selected_head"],
            "workpad_body": task["workpad"]["body"] + f"\n- Outcome: {outcome}\n",
            "summary": outcome, "outcome": outcome, "role_results": roles or [],
            "findings": findings or [], "requested_resolved_finding_ids": resolved or [],
        }

    def _write_result(self, attempt: dict[str, object], result: dict[str, object]) -> None:
        path = pathlib.Path(attempt["namespace"]) / "outbox" / "result.json"
        path.write_text(json.dumps(result), encoding="utf-8")

    def _role(self, role: str, verdict: str, head: str | None = None) -> dict[str, object]:
        return {"role": role, "verdict": verdict, "summary": verdict.lower(), "head_sha": head, "findings": []}

    def test_complete_lifecycle_stops_at_archivist_and_ready_is_guarded(self):
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "planning_complete",
            roles=[self._role("PROJECT-MANAGER", "APPROVE"), self._role("PLANNER", "COMPLETE")],
        ))
        reconcile(self.profile, self.workspace)

        attempt = self._attempt()
        (self.workspace / "implementation").write_text("implemented\n", encoding="utf-8")
        self._git("add", "implementation")
        self._git("commit", "-qm", "implementation")
        head = self._git("rev-parse", "HEAD")
        result = self._result(attempt, "implementation_complete", roles=[self._role("IMPLEMENTER", "COMPLETE", head)])
        self._write_result(attempt, result)
        reconcile(self.profile, self.workspace)

        for outcome, role, verdict in (
            ("review_approved", "REVIEWER", "APPROVE"),
            ("adversary_pass", "ADVERSARY", "PASS"),
            ("validation_pass", None, None),
            ("archive_complete", "ARCHIVIST", "COMPLETE"),
        ):
            attempt = self._attempt()
            roles = [] if role is None else [self._role(role, verdict, head)]
            self._write_result(attempt, self._result(attempt, outcome, roles=roles))
            reconcile(self.profile, self.workspace)

        with control_db.open_database(self.database_path) as database:
            task = database.read_task(self.TASK_ID)
            self.assertEqual(task["state"], "ARCHIVIST")
            self.assertEqual(task["current_head"], head)
            self.assertEqual(database.read_workpad(self.TASK_ID)["version"], 7)
            self.assertCountEqual(
                [row["role"] for row in database.read_projection(self.TASK_ID)["role_runs"]],
                ["ARCHITECT", "PROJECT-MANAGER", "PLANNER", "ARCHITECT", "IMPLEMENTER",
                 "ARCHITECT", "REVIEWER", "ARCHITECT", "ADVERSARY", "ARCHITECT", "ARCHITECT", "ARCHIVIST"],
            )
            with self.assertRaises(control_db.StateConflict):
                database.transition_task(
                    self.TASK_ID, expected_state="ARCHIVIST", new_state="READY_FOR_HUMAN_MERGE",
                    event_type="ready_for_human_merge",
                )

    def test_strict_result_boundary_rejects_untrusted_shapes(self):
        path = self.root / "result.json"
        valid = {"schema": RESULT_SCHEMA}
        path.write_text(json.dumps(valid), encoding="utf-8")
        with self.assertRaises(LifecycleError):
            read_result(path)
        path.write_text("{\"schema\":", encoding="utf-8")
        with self.assertRaises(LifecycleError):
            read_result(path)
        path.write_text("\ufeff{}", encoding="utf-8")
        with self.assertRaises(LifecycleError):
            read_result(path)

        path.write_bytes(b"{" + b"\"padding\":\"" + b"x" * (128 * 1024) + b"\"}")
        with self.assertRaises(LifecycleError):
            read_result(path)

        path.write_text(json.dumps({"schema": RESULT_SCHEMA, "unexpected": True}), encoding="utf-8")
        with self.assertRaises(LifecycleError):
            read_result(path)

        path.write_text(json.dumps({"schema": "Bearer fake-token"}), encoding="utf-8")
        with self.assertRaises(LifecycleError):
            read_result(path)
        path.unlink()
        try:
            path.symlink_to(self.root / "missing")
        except OSError:
            self.skipTest("symlink creation is unavailable on this Windows host")
        with self.assertRaises(LifecycleError):
            read_result(path)

    def test_missing_result_finishes_attempt_and_creates_infrastructure_blocker(self):
        attempt = self._attempt()
        import after_run

        with mock.patch.object(after_run, "load_profile", return_value=self.profile):
            self.assertEqual(
                after_run.main(["--profile", str(self.profile_path), "--workspace", str(self.workspace)]),
                78,
            )

        with control_db.open_database(self.database_path) as database:
            run = database.read_role_run(attempt["run"]["id"])
            self.assertEqual(run["status"], "failed")
            blockers = database.read_projection(self.TASK_ID)["blockers"]
            self.assertEqual([row["kind"] for row in blockers], ["infrastructure"])
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "QUEUED")

    def test_licensed_corrections_from_review_and_validation_return_to_implementation(self):
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "planning_complete",
            roles=[self._role("PROJECT-MANAGER", "APPROVE"), self._role("PLANNER", "COMPLETE")],
        ))
        reconcile(self.profile, self.workspace)

        attempt = self._attempt()
        (self.workspace / "implementation").write_text("implemented\n", encoding="utf-8")
        self._git("add", "implementation")
        self._git("commit", "-qm", "implementation")
        implementation_head = self._git("rev-parse", "HEAD")
        self._write_result(attempt, self._result(
            attempt, "implementation_complete",
            roles=[self._role("IMPLEMENTER", "COMPLETE", implementation_head)],
        ))
        reconcile(self.profile, self.workspace)

        reviewer_finding = {
            "role": "REVIEWER", "kind": "review defect", "severity": "high",
            "body": "the implementation needs a correction", "classification": "licensed correction",
        }
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "correction_required",
            roles=[{"role": "REVIEWER", "verdict": "FINDINGS", "summary": "correction",
                    "head_sha": implementation_head, "findings": [reviewer_finding]}],
        ))
        reconcile(self.profile, self.workspace)
        with control_db.open_database(self.database_path) as database:
            first_finding = database.read_projection(self.TASK_ID)["findings"][0]
        self.assertEqual(first_finding["status"], "licensed")

        attempt = self._attempt()
        (self.workspace / "review-correction").write_text("fixed\n", encoding="utf-8")
        self._git("add", "review-correction")
        self._git("commit", "-qm", "review correction")
        corrected_head = self._git("rev-parse", "HEAD")
        self._write_result(attempt, self._result(
            attempt, "correction_complete",
            roles=[self._role("IMPLEMENTER", "COMPLETE", corrected_head)],
            resolved=[first_finding["id"]],
        ))
        reconcile(self.profile, self.workspace)
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "IMPLEMENTED")
            self.assertEqual(database.read_finding(first_finding["id"])["status"], "resolved")

        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "review_approved",
            roles=[self._role("REVIEWER", "APPROVE", corrected_head)],
        ))
        reconcile(self.profile, self.workspace)

        adversary_finding = {
            "role": "ADVERSARY", "kind": "adversarial defect", "severity": "medium",
            "body": "the review missed a correction", "classification": "licensed correction",
        }
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "correction_required",
            roles=[{"role": "ADVERSARY", "verdict": "FINDINGS", "summary": "correction",
                    "head_sha": corrected_head, "findings": [adversary_finding]}],
        ))
        reconcile(self.profile, self.workspace)
        with control_db.open_database(self.database_path) as database:
            second_finding = database.read_projection(self.TASK_ID)["findings"][1]
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "REVIEW")

        attempt = self._attempt()
        (self.workspace / "adversary-correction").write_text("fixed again\n", encoding="utf-8")
        self._git("add", "adversary-correction")
        self._git("commit", "-qm", "adversary correction")
        final_head = self._git("rev-parse", "HEAD")
        self._write_result(attempt, self._result(
            attempt, "correction_complete",
            roles=[self._role("IMPLEMENTER", "COMPLETE", final_head)],
            resolved=[second_finding["id"]],
        ))
        reconcile(self.profile, self.workspace)
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "IMPLEMENTED")
            self.assertEqual(database.read_finding(second_finding["id"])["status"], "resolved")


if __name__ == "__main__":
    unittest.main(verbosity=2)
