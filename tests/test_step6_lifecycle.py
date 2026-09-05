from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "runtime"))

import control_db
from tests.storage_support import queue_task
import lifecycle as lifecycle_module
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
        queue_task(self.database, task)
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

    def _finding(self, role: str, classification: str, *, blocker_kind=None) -> dict[str, object]:
        return {
            "role": role, "kind": "bounded finding", "severity": "high",
            "body": "bounded finding evidence", "classification": classification,
            "blocker_kind": blocker_kind,
        }

    def _reach_implemented(self) -> str:
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
        self._write_result(attempt, self._result(
            attempt, "implementation_complete",
            roles=[self._role("IMPLEMENTER", "COMPLETE", head)],
        ))
        reconcile(self.profile, self.workspace)
        return head

    def _reach_adversarial_review(self) -> str:
        head = self._reach_implemented()
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "review_approved",
            roles=[self._role("REVIEWER", "APPROVE", head)],
        ))
        reconcile(self.profile, self.workspace)
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "adversary_pass",
            roles=[self._role("ADVERSARY", "PASS", head)],
        ))
        reconcile(self.profile, self.workspace)
        return head

    def _license_validation_correction(self) -> tuple[str, dict[str, object], int]:
        head = self._reach_adversarial_review()
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "correction_required", findings=[self._finding("ARCHITECT", "licensed correction")],
        ))
        reconcile(self.profile, self.workspace)
        with control_db.open_database(self.database_path) as database:
            finding = database.read_projection(self.TASK_ID)["findings"][0]
        return head, finding, int(finding["licensed_correction_round"])

    def _resolve_blocker(self, blocker_id: str) -> None:
        with control_db.open_database(self.database_path) as database:
            database.resolve_blocker(blocker_id, resolved_at="2026-09-04T12:30:00+00:00")

    def _record_blocked_correction(self, *, partial_head: bool = False) -> tuple[dict[str, object], str, str]:
        attempt = self._attempt()
        if partial_head:
            (self.workspace / "partial-correction").write_text("partial\n", encoding="utf-8")
            self._git("add", "partial-correction")
            self._git("commit", "-qm", "partial correction")
            actual_head = self._git("rev-parse", "HEAD")
        else:
            actual_head = self._git("rev-parse", "HEAD")
        blocked_finding = self._finding(
            "IMPLEMENTER", "unresolved project decision", blocker_kind="project"
        )
        self._write_result(attempt, self._result(
            attempt, "blocked",
            roles=[self._role("IMPLEMENTER", "BLOCKED", actual_head if partial_head else None) | {
                "findings": [blocked_finding],
            }],
        ))
        reconcile(self.profile, self.workspace)
        with control_db.open_database(self.database_path) as database:
            projection = database.read_projection(self.TASK_ID)
            role_run = next(row for row in reversed(projection["role_runs"]) if row["role"] == "IMPLEMENTER")
            blocker_id = str(projection["blockers"][-1]["id"])
            current_head = str(database.read_task(self.TASK_ID)["current_head"])
        return role_run, blocker_id, current_head

    def _complete_correction(self, expected_head: str, finding_id: str) -> str:
        attempt = self._attempt()
        (self.workspace / "completed-correction").write_text("complete\n", encoding="utf-8")
        self._git("add", "completed-correction")
        self._git("commit", "-qm", "complete correction")
        corrected_head = self._git("rev-parse", "HEAD")
        self.assertNotEqual(corrected_head, expected_head)
        self._write_result(attempt, self._result(
            attempt, "correction_complete",
            roles=[self._role("IMPLEMENTER", "COMPLETE", corrected_head)],
            resolved=[finding_id],
        ))
        reconcile(self.profile, self.workspace)
        return corrected_head

    def test_blocked_correction_rolls_over_and_completes_after_blocker_resolution(self):
        old_head, finding, licensed_round = self._license_validation_correction()
        role_run, blocker_id, current_head = self._record_blocked_correction()
        self.assertEqual(current_head, old_head)
        self.assertEqual(role_run["round"], licensed_round)
        self.assertEqual(finding["status"], "licensed")
        with control_db.open_database(self.database_path) as database:
            stored = database.read_finding(finding["id"])
            self.assertEqual(stored["licensed_correction_round"], licensed_round + 1)
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "ADVERSARIAL_REVIEW")
        self._resolve_blocker(blocker_id)

        corrected_head = self._complete_correction(old_head, str(finding["id"]))
        with control_db.open_database(self.database_path) as database:
            stored = database.read_finding(finding["id"])
            self.assertEqual(stored["status"], "resolved")
            self.assertIsNone(stored["licensed_correction_round"])
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "IMPLEMENTED")
            self.assertEqual(database.read_task(self.TASK_ID)["current_head"], corrected_head)
            correction_runs = [
                row for row in database.read_projection(self.TASK_ID)["role_runs"]
                if row["role"] == "IMPLEMENTER"
            ]
            self.assertEqual(correction_runs[-1]["round"], licensed_round + 1)

    def test_blocked_partial_correction_rolls_over_and_invalidates_acceptance(self):
        old_head, finding, licensed_round = self._license_validation_correction()
        role_run, blocker_id, partial_head = self._record_blocked_correction(partial_head=True)
        self.assertNotEqual(partial_head, old_head)
        self.assertEqual(role_run["round"], licensed_round)
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_task(self.TASK_ID)["current_head"], partial_head)
            self.assertEqual(database.read_finding(finding["id"])["licensed_correction_round"], licensed_round + 1)
            self.assertFalse(lifecycle_module._current_acceptance(
                database, self.TASK_ID, "review_accepted", old_head
            ))
            self.assertFalse(lifecycle_module._current_acceptance(
                database, self.TASK_ID, "adversary_accepted", old_head
            ))
        self._resolve_blocker(blocker_id)

        corrected_head = self._complete_correction(partial_head, str(finding["id"]))
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_finding(finding["id"])["status"], "resolved")
            self.assertEqual(database.read_task(self.TASK_ID)["current_head"], corrected_head)

    def test_multiple_blocked_corrections_roll_license_once_per_implementer_round(self):
        old_head, finding, licensed_round = self._license_validation_correction()
        first_run, first_blocker, current_head = self._record_blocked_correction()
        self.assertEqual(current_head, old_head)
        self.assertEqual(first_run["round"], licensed_round)
        self._resolve_blocker(first_blocker)

        second_run, second_blocker, current_head = self._record_blocked_correction()
        self.assertEqual(current_head, old_head)
        self.assertEqual(second_run["round"], licensed_round + 1)
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_finding(finding["id"])["licensed_correction_round"], licensed_round + 2)
        self._resolve_blocker(second_blocker)

        self._complete_correction(old_head, str(finding["id"]))
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_finding(finding["id"])["status"], "resolved")
            self.assertIsNone(database.read_finding(finding["id"])["licensed_correction_round"])

    def test_architect_only_block_does_not_consume_correction_round(self):
        old_head, finding, licensed_round = self._license_validation_correction()
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "blocked", findings=[self._finding(
                "ARCHITECT", "infrastructure condition", blocker_kind="infrastructure"
            )],
        ))
        reconcile(self.profile, self.workspace)
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "ADVERSARIAL_REVIEW")
            self.assertEqual(database.read_task(self.TASK_ID)["current_head"], old_head)
            self.assertEqual(database.read_finding(finding["id"])["licensed_correction_round"], licensed_round)
            self.assertFalse(any(
                row["role"] == "IMPLEMENTER" and row["round"] == licensed_round + 1
                for row in database.read_projection(self.TASK_ID)["role_runs"]
            ))

    def test_stale_correction_round_fails_closed_and_preserves_evidence(self):
        old_head, finding, licensed_round = self._license_validation_correction()
        with control_db.open_database(self.database_path) as database:
            database.connection.execute(
                "UPDATE findings SET licensed_correction_round = ? WHERE id = ?",
                (licensed_round + 99, finding["id"]),
            )
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "blocked",
            roles=[self._role("IMPLEMENTER", "BLOCKED") | {
                "findings": [self._finding(
                    "IMPLEMENTER", "infrastructure condition", blocker_kind="infrastructure"
                )],
            }],
        ))
        import after_run
        with mock.patch.object(after_run, "load_profile", return_value=self.profile):
            self.assertEqual(
                after_run.main(["--profile", str(self.profile_path), "--workspace", str(self.workspace)]),
                78,
            )
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_task(self.TASK_ID)["current_head"], old_head)
            self.assertEqual(
                database.read_finding(finding["id"])["licensed_correction_round"], licensed_round + 99
            )
            self.assertEqual(
                database.read_projection(self.TASK_ID)["blockers"][-1]["kind"], "infrastructure"
            )
            implementer_runs = [
                row for row in database.read_projection(self.TASK_ID)["role_runs"]
                if row["role"] == "IMPLEMENTER"
            ]
            self.assertEqual(len(implementer_runs), 1)

    def test_architect_only_correction_block_cannot_adopt_changed_head(self):
        old_head, finding, licensed_round = self._license_validation_correction()
        attempt = self._attempt()
        architect_run_id = str(attempt["run"]["id"])
        (self.workspace / "unauthorized-correction").write_text("unexpected\n", encoding="utf-8")
        self._git("add", "unauthorized-correction")
        self._git("commit", "-qm", "unauthorized architect-only change")
        unexpected_head = self._git("rev-parse", "HEAD")
        self._write_result(attempt, self._result(
            attempt, "blocked", findings=[self._finding(
                "ARCHITECT", "infrastructure condition", blocker_kind="infrastructure"
            )],
        ))
        import after_run
        with mock.patch.object(after_run, "load_profile", return_value=self.profile):
            self.assertEqual(
                after_run.main(["--profile", str(self.profile_path), "--workspace", str(self.workspace)]),
                78,
            )
        with control_db.open_database(self.database_path) as database:
            projection = database.read_projection(self.TASK_ID)
            self.assertEqual(database.read_task(self.TASK_ID)["current_head"], old_head)
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "ADVERSARIAL_REVIEW")
            self.assertEqual(database.read_role_run(architect_run_id)["status"], "failed")
            self.assertEqual(database.read_finding(finding["id"])["licensed_correction_round"], licensed_round)
            self.assertEqual(projection["blockers"][-1]["kind"], "infrastructure")
            implementer_runs = [row for row in projection["role_runs"] if row["role"] == "IMPLEMENTER"]
            self.assertEqual(len(implementer_runs), 1)
        self.assertEqual(self._git("rev-parse", "HEAD"), unexpected_head)

    def test_architect_only_planned_block_cannot_adopt_changed_head(self):
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "planning_complete",
            roles=[self._role("PROJECT-MANAGER", "APPROVE"), self._role("PLANNER", "COMPLETE")],
        ))
        reconcile(self.profile, self.workspace)
        attempt = self._attempt()
        architect_run_id = str(attempt["run"]["id"])
        (self.workspace / "unauthorized-planned").write_text("unexpected\n", encoding="utf-8")
        self._git("add", "unauthorized-planned")
        self._git("commit", "-qm", "unauthorized planned change")
        unexpected_head = self._git("rev-parse", "HEAD")
        self._write_result(attempt, self._result(
            attempt, "blocked", findings=[self._finding(
                "ARCHITECT", "infrastructure condition", blocker_kind="infrastructure"
            )],
        ))
        import after_run
        with mock.patch.object(after_run, "load_profile", return_value=self.profile):
            self.assertEqual(
                after_run.main(["--profile", str(self.profile_path), "--workspace", str(self.workspace)]),
                78,
            )
        with control_db.open_database(self.database_path) as database:
            projection = database.read_projection(self.TASK_ID)
            self.assertIsNone(database.read_task(self.TASK_ID)["current_head"])
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "PLANNED")
            self.assertEqual(database.read_role_run(architect_run_id)["status"], "failed")
            self.assertEqual(projection["blockers"][-1]["kind"], "infrastructure")
            self.assertFalse(any(row["role"] == "IMPLEMENTER" for row in projection["role_runs"]))
        self.assertEqual(self._git("rev-parse", "HEAD"), unexpected_head)

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
            "blocker_kind": None,
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
            "blocker_kind": None,
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

    def test_mechanical_validation_correction_invalidates_prior_acceptance(self):
        old_head = self._reach_adversarial_review()
        finding = self._finding("ARCHITECT", "licensed correction")
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "correction_required", findings=[finding],
        ))
        reconcile(self.profile, self.workspace)

        with control_db.open_database(self.database_path) as database:
            stored = database.read_projection(self.TASK_ID)["findings"][0]
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "ADVERSARIAL_REVIEW")

        attempt = self._attempt()
        (self.workspace / "mechanical-correction").write_text("fixed validation\n", encoding="utf-8")
        self._git("add", "mechanical-correction")
        self._git("commit", "-qm", "mechanical correction")
        new_head = self._git("rev-parse", "HEAD")
        self._write_result(attempt, self._result(
            attempt, "correction_complete",
            roles=[self._role("IMPLEMENTER", "COMPLETE", new_head)],
            resolved=[stored["id"]],
        ))
        reconcile(self.profile, self.workspace)

        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "IMPLEMENTED")
            self.assertEqual(database.read_finding(stored["id"])["status"], "resolved")
            self.assertNotEqual(old_head, new_head)
            self.assertFalse(lifecycle_module._current_acceptance(
                database, self.TASK_ID, "review_accepted", old_head
            ))
            self.assertFalse(lifecycle_module._current_acceptance(
                database, self.TASK_ID, "adversary_accepted", old_head
            ))

    def test_blocked_results_require_and_persist_blocking_evidence(self):
        project_finding = self._finding(
            "PROJECT-MANAGER", "unresolved project decision", blocker_kind="project"
        )
        project_finding["kind"] = "human escalation prose that is not authority"
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "blocked",
            roles=[{"role": "PROJECT-MANAGER", "verdict": "BLOCKED", "summary": "decision needed",
                    "head_sha": None, "findings": [project_finding]}],
        ))
        reconcile(self.profile, self.workspace)
        with control_db.open_database(self.database_path) as database:
            projection = database.read_projection(self.TASK_ID)
            self.assertEqual(projection["task"]["state"], "QUEUED")
            self.assertEqual([row["kind"] for row in projection["blockers"]], ["project"])
            self.assertIn("human escalation prose", projection["findings"][0]["kind"])
            self.assertEqual(projection["role_runs"][-1]["status"], "finished")

    def test_reviewer_blocked_and_infrastructure_blocked_without_specialized_role(self):
        self._reach_implemented()
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "blocked",
            roles=[{"role": "REVIEWER", "verdict": "BLOCKED", "summary": "infra unavailable",
                    "head_sha": None, "findings": [self._finding(
                        "REVIEWER", "infrastructure condition", blocker_kind="infrastructure"
                    )]}],
        ))
        reconcile(self.profile, self.workspace)
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "IMPLEMENTED")
            self.assertEqual(database.read_projection(self.TASK_ID)["blockers"][0]["kind"], "infrastructure")

    def test_architect_only_infrastructure_block_is_persisted(self):
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "blocked", findings=[self._finding(
                "ARCHITECT", "infrastructure condition", blocker_kind="infrastructure"
            )],
        ))
        reconcile(self.profile, self.workspace)
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "QUEUED")
            self.assertEqual(database.read_projection(self.TASK_ID)["blockers"][0]["kind"], "infrastructure")

    def test_blocked_without_blocking_evidence_fails_attempt_and_adds_infrastructure_blocker(self):
        attempt = self._attempt()
        self._write_result(attempt, self._result(attempt, "blocked"))
        import after_run

        with mock.patch.object(after_run, "load_profile", return_value=self.profile):
            self.assertEqual(
                after_run.main(["--profile", str(self.profile_path), "--workspace", str(self.workspace)]),
                78,
            )
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_role_run(attempt["run"]["id"])["status"], "failed")
            self.assertEqual(database.read_projection(self.TASK_ID)["blockers"][0]["kind"], "infrastructure")

    def test_staging_failure_is_compensated_after_architect_allocation(self):
        with mock.patch.object(lifecycle_module, "lifecycle_root", side_effect=OSError("fixture")):
            with self.assertRaises(LifecycleError):
                self._attempt()
        with control_db.open_database(self.database_path) as database:
            runs = database.read_projection(self.TASK_ID)["role_runs"]
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["role"], "ARCHITECT")
            self.assertEqual(runs[0]["status"], "failed")
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "QUEUED")
            self.assertEqual(database.read_projection(self.TASK_ID)["blockers"][0]["kind"], "infrastructure")

    def test_pre_row_allocation_failure_records_direct_infrastructure_blocker(self):
        with mock.patch.object(
                lifecycle_module, "_packet", side_effect=LifecycleError("packet staging fixture")):
            with self.assertRaises(LifecycleError):
                self._attempt()
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_projection(self.TASK_ID)["role_runs"], [])
            self.assertEqual(database.read_projection(self.TASK_ID)["blockers"][0]["kind"], "infrastructure")
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "QUEUED")

    def test_two_concurrent_preparations_create_one_started_architect_without_blocking_winner(self):
        from concurrent.futures import ThreadPoolExecutor

        marker = self.workspace / ".git" / "symphony-preparation.json"
        marker.write_text(json.dumps({"schema": "symphony-pilot-preparation/v3"}), encoding="utf-8")
        with control_db.open_database(self.database_path) as database:
            branch = database.read_task(self.TASK_ID)["branch"]
        self._git("switch", "-q", "-C", branch)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda unused: self._prepare_only(), (None, None)
            ))
        successes = [value for value in results if isinstance(value, dict)]
        failures = [value for value in results if isinstance(value, LifecycleError)]
        self.assertEqual(len(successes), 1, results)
        self.assertEqual(len(failures), 1)
        with control_db.open_database(self.database_path) as database:
            started = database.connection.execute(
                "SELECT count(id) FROM role_runs WHERE task_id = ? AND role = 'ARCHITECT' AND status = 'started'",
                (self.TASK_ID,),
            ).fetchone()[0]
            self.assertEqual(started, 1)
            self.assertEqual(database.read_projection(self.TASK_ID)["blockers"], [])

    def _prepare_only(self):
        try:
            return prepare_attempt(self.profile, self.workspace)
        except LifecycleError as exc:
            return exc

    def test_host_json_directory_is_rejected_without_reading_it(self):
        directory = self.root / "json-directory"
        directory.mkdir()
        with self.assertRaises(LifecycleError):
            lifecycle_module._read_host_json(directory, "directory")

    def test_host_json_fifo_is_rejected_without_blocking(self):
        fifo = self.root / "json-fifo"
        try:
            os.mkfifo(fifo)
        except (AttributeError, NotImplementedError, OSError):
            self.skipTest("FIFO creation is unavailable on this host")
        with self.assertRaises(LifecycleError):
            lifecycle_module._read_host_json(fifo, "fifo")

    def test_host_json_descriptor_survives_path_replacement(self):
        if os.name == "nt":
            self.skipTest("open-file replacement probe requires native POSIX semantics")
        original = self.root / "original.json"
        replacement = self.root / "replacement.json"
        original.write_text(json.dumps({"safe": True}), encoding="utf-8")
        replacement.write_text(json.dumps({"safe": False}), encoding="utf-8")
        original_fstat = lifecycle_module.os.fstat

        def replace_after_open(descriptor):
            os.replace(original, self.root / "opened-original.json")
            original.symlink_to(replacement)
            return original_fstat(descriptor)

        try:
            with mock.patch.object(lifecycle_module.os, "fstat", side_effect=replace_after_open):
                self.assertEqual(
                    lifecycle_module._read_host_json(original, "replacement race"), {"safe": True}
                )
        except OSError:
            self.skipTest("symlink replacement is unavailable on this host")

    def test_persistent_identity_and_git_truth_failures_create_blockers(self):
        def assert_failed(case, result):
            case._write_result(attempt, result)
            import after_run
            with mock.patch.object(after_run, "load_profile", return_value=case.profile):
                self.assertEqual(
                    after_run.main([
                        "--profile", str(case.profile_path), "--workspace", str(case.workspace)
                    ]),
                    78,
                )
            with control_db.open_database(case.database_path) as database:
                self.assertEqual(database.read_role_run(attempt["run"]["id"])["status"], "failed")
                self.assertEqual(database.read_task(case.TASK_ID)["state"], "QUEUED")
                self.assertEqual(database.read_projection(case.TASK_ID)["blockers"][0]["kind"], "infrastructure")

        mutations = {
            "wrong task UUID": lambda case, result: result.update(
                task_uuid="22222222-2222-2222-2222-222222222222"
            ),
            "wrong T-N": lambda case, result: result.update(identifier="T-000002"),
            "wrong Architect run": lambda case, result: result.update(
                architect_role_run_id="22222222-2222-2222-2222-222222222222"
            ),
            "stale state": lambda case, result: result.update(expected_state="PLANNED"),
            "stale workpad": lambda case, result: result.update(expected_workpad_version=2),
            "stale starting HEAD": lambda case, result: result.update(expected_starting_head="b" * 40),
            "dirty workspace": lambda case, result: (case.workspace / "dirty").write_text("dirty\n", encoding="utf-8"),
            "read-only HEAD change": lambda case, result: (
                (case.workspace / "unexpected").write_text("unexpected\n", encoding="utf-8"),
                case._git("add", "unexpected"),
                case._git("commit", "-qm", "unexpected read-only change"),
            ),
            "wrong branch": lambda case, result: case._git("switch", "-q", "-c", "wrong-branch"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                case = type(self)("runTest")
                case.setUp()
                try:
                    attempt = case._attempt()
                    result = case._result(
                        attempt, "planning_complete",
                        roles=[case._role("PROJECT-MANAGER", "APPROVE"), case._role("PLANNER", "COMPLETE")],
                    )
                    mutate(case, result)
                    assert_failed(case, result)
                finally:
                    case.tearDown()

    def test_replayed_result_is_rejected_without_duplicate_history(self):
        attempt = self._attempt()
        result = self._result(
            attempt, "planning_complete",
            roles=[self._role("PROJECT-MANAGER", "APPROVE"), self._role("PLANNER", "COMPLETE")],
        )
        self._write_result(attempt, result)
        reconcile(self.profile, self.workspace)
        with control_db.open_database(self.database_path) as database:
            before = database.read_projection(self.TASK_ID)
        with self.assertRaises(LifecycleError):
            reconcile(self.profile, self.workspace)
        with control_db.open_database(self.database_path) as database:
            after = database.read_projection(self.TASK_ID)
            self.assertEqual(len(after["role_runs"]), len(before["role_runs"]))
            self.assertEqual(len(after["events"]), len(before["events"]))

    def test_finding_resolution_is_task_owned_and_unlicensed_ids_are_rejected(self):
        self._reach_implemented()
        reviewer_finding = self._finding("REVIEWER", "licensed correction")
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "correction_required",
            roles=[{"role": "REVIEWER", "verdict": "FINDINGS", "summary": "correction",
                    "head_sha": self._git("rev-parse", "HEAD"), "findings": [reviewer_finding]}],
        ))
        reconcile(self.profile, self.workspace)
        with control_db.open_database(self.database_path) as database:
            current_finding = database.read_projection(self.TASK_ID)["findings"][0]
            foreign_task = database.create_task(
                task_id="22222222-2222-2222-2222-222222222222", identifier="T-000002",
                project_slug="demo", title="Foreign task", objective="Foreign objective",
                base_ref="master", base_sha=self.BASE_SHA, current_head=self.BASE_SHA,
                state="IMPLEMENTED", created_at="2026-09-04T12:00:01+00:00",
            )
            foreign_role = database.create_role_run(foreign_task["id"], "REVIEWER", 1, head_sha=self.BASE_SHA)
            foreign_finding = database.record_finding(
                task_id=foreign_task["id"], role_run_id=foreign_role["id"], kind="foreign",
                severity="high", body="foreign licensed finding", status="licensed",
                licensed_correction_round=1,
            )

        attempt = self._attempt()
        (self.workspace / "foreign-resolution").write_text("attempt\n", encoding="utf-8")
        self._git("add", "foreign-resolution")
        self._git("commit", "-qm", "foreign resolution attempt")
        new_head = self._git("rev-parse", "HEAD")
        self._write_result(attempt, self._result(
            attempt, "correction_complete",
            roles=[self._role("IMPLEMENTER", "COMPLETE", new_head)],
            resolved=[foreign_finding["id"]],
        ))
        import after_run
        with mock.patch.object(after_run, "load_profile", return_value=self.profile):
            self.assertEqual(
                after_run.main(["--profile", str(self.profile_path), "--workspace", str(self.workspace)]),
                78,
            )
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_finding(current_finding["id"])["status"], "licensed")
            self.assertEqual(database.read_finding(foreign_finding["id"])["status"], "licensed")
            self.assertEqual(database.read_projection(self.TASK_ID)["blockers"][0]["kind"], "infrastructure")

    def test_unlicensed_resolution_on_implementation_result_is_rejected(self):
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
        self._write_result(attempt, self._result(
            attempt, "implementation_complete",
            roles=[self._role("IMPLEMENTER", "COMPLETE", head)],
            resolved=["22222222-2222-2222-2222-222222222222"],
        ))
        # The wrong role is rejected before any resolution can be applied.
        import after_run
        with mock.patch.object(after_run, "load_profile", return_value=self.profile):
            self.assertEqual(
                after_run.main(["--profile", str(self.profile_path), "--workspace", str(self.workspace)]),
                78,
            )
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "PLANNED")
            self.assertEqual(database.read_projection(self.TASK_ID)["blockers"][0]["kind"], "infrastructure")

    def test_acceptance_and_review_negatives_are_current_head_bound(self):
        self._reach_implemented()

        # A reviewer cannot approve while its licensed correction remains open.
        finding = self._finding("REVIEWER", "licensed correction")
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "correction_required",
            roles=[{"role": "REVIEWER", "verdict": "FINDINGS", "summary": "correction",
                    "head_sha": self._git("rev-parse", "HEAD"), "findings": [finding]}],
        ))
        reconcile(self.profile, self.workspace)
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "review_approved",
            roles=[self._role("REVIEWER", "APPROVE", self._git("rev-parse", "HEAD"))],
        ))
        import after_run
        with mock.patch.object(after_run, "load_profile", return_value=self.profile):
            self.assertEqual(
                after_run.main(["--profile", str(self.profile_path), "--workspace", str(self.workspace)]),
                78,
            )

        # Establish a clean acceptance chain, then prove each downstream phase
        # refuses a result when the prerequisite acceptance is no longer current.
        self.tearDown()
        self.setUp()
        self._reach_implemented()
        head = self._git("rev-parse", "HEAD")
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "review_approved", roles=[self._role("REVIEWER", "APPROVE", head)]
        ))
        reconcile(self.profile, self.workspace)
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "adversary_pass", roles=[self._role("ADVERSARY", "PASS", head)]
        ))
        with mock.patch.object(lifecycle_module, "_current_acceptance", return_value=False), \
             mock.patch.object(after_run, "load_profile", return_value=self.profile):
            self.assertEqual(
                after_run.main(["--profile", str(self.profile_path), "--workspace", str(self.workspace)]),
                78,
            )

        self.tearDown()
        self.setUp()
        self._reach_adversarial_review()
        head = self._git("rev-parse", "HEAD")
        attempt = self._attempt()
        self._write_result(attempt, self._result(attempt, "validation_pass"))
        with mock.patch.object(lifecycle_module, "_current_acceptance", return_value=False), \
             mock.patch.object(after_run, "load_profile", return_value=self.profile):
            self.assertEqual(
                after_run.main(["--profile", str(self.profile_path), "--workspace", str(self.workspace)]),
                78,
            )

        self.tearDown()
        self.setUp()
        head = self._reach_adversarial_review()
        attempt = self._attempt()
        self._write_result(attempt, self._result(attempt, "validation_pass"))
        reconcile(self.profile, self.workspace)
        attempt = self._attempt()
        self._write_result(attempt, self._result(
            attempt, "archive_complete", roles=[self._role("ARCHIVIST", "COMPLETE", head)]
        ))
        with mock.patch.object(lifecycle_module, "_current_acceptance", return_value=False), \
             mock.patch.object(after_run, "load_profile", return_value=self.profile):
            self.assertEqual(
                after_run.main(["--profile", str(self.profile_path), "--workspace", str(self.workspace)]),
                78,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
