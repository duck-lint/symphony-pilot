from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "runtime"))

import control_db
from lifecycle import RESULT_SCHEMA, prepare_attempt, reconcile
from prepare_workspace import Profile
from tests.storage_support import queue_task


class NamedRoleAuthorityTests(unittest.TestCase):
    TASK_ID = "11111111-1111-1111-1111-111111111111"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.workspace = self.root / "work" / "T-000001"
        self.workspace.mkdir(parents=True)
        self.profile = Profile(
            slug="demo", repository="example/demo", git_remote=str(self.workspace),
            workspace_root=self.root / "work", state_root=self.root / "state", log_root=self.root / "logs",
            secret_reference="unused", trusted_dispatchers=("duck-lint",), dispatch_labels=("auto",),
            blocked_label="human", service_identity="symphony-pilot-demo", dashboard_port=4040,
            max_concurrent_agents=1, max_turns=8, poll_interval_ms=1000, max_retry_backoff_ms=1000,
            codex_model="model", codex_reasoning_effort="high", toolchain=None,
        )
        self.database_path = self.root / "control.sqlite3"
        self.git("init", "-q", "-b", "master")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Named role test")
        (self.workspace / "README").write_text("base\n", encoding="utf-8")
        self.git("add", "README")
        self.git("commit", "-qm", "base")
        base_sha = self.git("rev-parse", "HEAD")
        self.git("remote", "add", "origin", str(self.workspace))
        with control_db.open_database(self.database_path) as database:
            task = database.create_task(
                project_slug="demo", title="Role task", objective="Named role lifecycle",
                base_ref="master", base_sha=base_sha, task_id=self.TASK_ID,
                identifier="T-000001",
            )
            queue_task(database, task)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=self.workspace, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def attempt(self) -> dict[str, object]:
        with control_db.open_database(self.database_path) as database:
            task = database.read_task(self.TASK_ID)
        self.git("switch", "-q", "-C", task["branch"])
        marker = self.workspace / ".git" / "symphony-preparation.json"
        if not marker.exists():
            marker.write_text(json.dumps({"schema": "symphony-pilot-preparation/v3"}), encoding="utf-8")
        return prepare_attempt(self.profile, self.workspace)

    def finish(self, attempt: dict[str, object], *, outcome: str, role: str, verdict: str | None = None, head_sha: str | None = None, findings: list[dict[str, object]] | None = None) -> None:
        packet = attempt["packet"]
        writable_roots = packet["dispatch"]["writable_roots"]
        if role == "IMPLEMENTER":
            self.assertIn(str(self.workspace), writable_roots)
        else:
            self.assertNotIn(str(self.workspace), writable_roots)
        self.assertTrue(all(str(path).endswith("outbox") for path in writable_roots if role != "IMPLEMENTER"))
        role_packet = None if role == "ARCHITECT" else {
            "role": role, "verdict": verdict or "COMPLETE", "summary": role.lower(),
            "head_sha": head_sha, "findings": findings or [],
        }
        result = {
            "schema": RESULT_SCHEMA, "task_uuid": packet["task_uuid"], "identifier": packet["identifier"],
            "role_run_id": packet["role_run_id"], "role": role,
            "expected_state": packet["current_state"], "expected_workpad_version": packet["workpad"]["version"],
            "expected_starting_head": packet["selected_head"], "workpad_body": packet["workpad"]["body"],
            "summary": outcome, "outcome": outcome, "packet": role_packet,
            "findings": [], "requested_resolved_finding_ids": [],
        }
        namespace = pathlib.Path(attempt["namespace"])
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        (namespace / "outbox" / "execution.json").write_text(json.dumps({
            "schema": "symphony-runtime-execution/v1", "role": packet["role"],
            "role_run_id": packet["role_run_id"], "status": "finished",
            "started_at": now, "finished_at": now, "session_id": f"session-{packet['role_run_id']}",
            "thread_id": f"thread-{packet['role_run_id']}", "turn_id": f"turn-{packet['role_run_id']}",
        }), encoding="utf-8")
        (namespace / "outbox" / "result.json").write_text(json.dumps(result), encoding="utf-8")
        reconcile(self.profile, self.workspace)

    def test_full_fresh_role_round_and_archivist_closeout(self):
        self.finish(self.attempt(), outcome="role_requested", role="ARCHITECT")
        self.finish(self.attempt(), outcome="role_complete", role="PROJECT-MANAGER", verdict="APPROVE")
        self.finish(self.attempt(), outcome="role_requested", role="ARCHITECT")
        self.finish(self.attempt(), outcome="role_complete", role="PLANNER", verdict="COMPLETE")
        self.finish(self.attempt(), outcome="planning_complete", role="ARCHITECT")

        implementation = self.workspace / "implementation.txt"
        implementation.write_text("implemented\n", encoding="utf-8")
        self.git("add", "implementation.txt")
        self.git("commit", "-qm", "implement")
        head = self.git("rev-parse", "HEAD")
        self.finish(self.attempt(), outcome="role_complete", role="IMPLEMENTER", verdict="COMPLETE", head_sha=head)
        self.finish(self.attempt(), outcome="implementation_complete", role="ARCHITECT")
        self.finish(self.attempt(), outcome="role_complete", role="REVIEWER", verdict="APPROVE")
        self.finish(self.attempt(), outcome="review_approved", role="ARCHITECT")
        self.finish(self.attempt(), outcome="role_complete", role="ADVERSARY", verdict="PASS")
        self.finish(self.attempt(), outcome="adversary_pass", role="ARCHITECT")
        self.finish(self.attempt(), outcome="validation_pass", role="ARCHITECT")
        self.finish(self.attempt(), outcome="archive_complete", role="ARCHIVIST", verdict="COMPLETE")

        with control_db.open_database(self.database_path) as database:
            projection = database.read_projection(self.TASK_ID)
            self.assertEqual(projection["task"]["state"], "FINAL_MECHANICAL_ACCEPTANCE")
            self.assertEqual([row["role"] for row in projection["role_runs"]], [
                "ARCHITECT", "PROJECT-MANAGER", "ARCHITECT", "PLANNER", "ARCHITECT",
                "IMPLEMENTER", "ARCHITECT", "REVIEWER", "ARCHITECT", "ADVERSARY",
                "ARCHITECT", "ARCHITECT", "ARCHIVIST",
            ])
            self.assertTrue(all(row["status"] == "finished" for row in projection["role_runs"]))
            self.assertEqual(projection["role_runs"][5]["head_sha"], head)

    def test_architect_claim_cannot_create_specialized_execution(self):
        attempt = self.attempt()
        packet = attempt["packet"]
        namespace = pathlib.Path(attempt["namespace"])
        result = {
            "schema": RESULT_SCHEMA, "task_uuid": packet["task_uuid"], "identifier": packet["identifier"],
            "role_run_id": packet["role_run_id"], "role": "PROJECT-MANAGER",
            "expected_state": packet["current_state"], "expected_workpad_version": packet["workpad"]["version"],
            "expected_starting_head": packet["selected_head"], "workpad_body": packet["workpad"]["body"],
            "summary": "forged", "outcome": "role_complete",
            "packet": {"role": "PROJECT-MANAGER", "verdict": "APPROVE", "summary": "forged", "head_sha": None, "findings": []},
            "findings": [], "requested_resolved_finding_ids": [],
        }
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        (namespace / "outbox" / "execution.json").write_text(json.dumps({"role": "ARCHITECT", "role_run_id": packet["role_run_id"], "status": "finished", "started_at": now, "finished_at": now}), encoding="utf-8")
        (namespace / "outbox" / "result.json").write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaises(Exception):
            reconcile(self.profile, self.workspace)
        with control_db.open_database(self.database_path) as database:
            self.assertFalse(any(row["role"] == "PROJECT-MANAGER" for row in database.read_projection(self.TASK_ID)["role_runs"]))

    def test_review_nonconvergence_starts_a_new_full_round_at_project_manager(self):
        finding = {
            "role": "REVIEWER", "kind": "review finding", "severity": "high",
            "body": "the accepted seam is incomplete", "classification": "licensed correction",
            "blocker_kind": None,
        }
        self.finish(self.attempt(), outcome="role_requested", role="ARCHITECT")
        self.finish(self.attempt(), outcome="role_complete", role="PROJECT-MANAGER", verdict="APPROVE")
        self.finish(self.attempt(), outcome="role_requested", role="ARCHITECT")
        self.finish(self.attempt(), outcome="role_complete", role="PLANNER", verdict="COMPLETE")
        self.finish(self.attempt(), outcome="planning_complete", role="ARCHITECT")
        (self.workspace / "implementation.txt").write_text("implemented\n", encoding="utf-8")
        self.git("add", "implementation.txt")
        self.git("commit", "-qm", "implement")
        head = self.git("rev-parse", "HEAD")
        self.finish(self.attempt(), outcome="role_complete", role="IMPLEMENTER", verdict="COMPLETE", head_sha=head)
        self.finish(self.attempt(), outcome="implementation_complete", role="ARCHITECT")
        self.finish(self.attempt(), outcome="role_complete", role="REVIEWER", verdict="FINDINGS", findings=[finding])
        self.finish(self.attempt(), outcome="correction_required", role="ARCHITECT")

        next_attempt = self.attempt()
        self.assertEqual(next_attempt["packet"]["role"], "PROJECT-MANAGER")
        self.assertEqual(next_attempt["run"]["round"], 2)


if __name__ == "__main__":
    unittest.main()
