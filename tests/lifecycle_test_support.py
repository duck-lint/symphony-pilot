from __future__ import annotations

import datetime as dt
import json
import pathlib

from tests.test_named_role_authority import NamedRoleAuthorityTests


class LifecycleFixture(NamedRoleAuthorityTests):
    """Execution-bound fixture shared by preserved boundary regressions."""

    def _attempt(self):
        return self.attempt()

    def _git(self, *args: str) -> str:
        return self.git(*args)

    def _role(self, role: str, verdict: str, head: str | None = None) -> dict[str, object]:
        return {"role": role, "verdict": verdict, "summary": verdict.lower(), "head_sha": head, "findings": []}

    def _finding(self, role: str, classification: str, *, blocker_kind=None) -> dict[str, object]:
        return {
            "role": role, "kind": "bounded finding", "severity": "high",
            "body": "bounded finding evidence", "classification": classification,
            "blocker_kind": blocker_kind,
        }

    def _result(self, attempt: dict[str, object], outcome: str, *, roles=None, findings=None, resolved=None) -> dict[str, object]:
        packet = attempt["packet"]
        role = str(packet["role"])
        selected = next((item for item in (roles or []) if item.get("role") == role), None)
        role_packet = None if role == "ARCHITECT" else (selected or self._role(role, "COMPLETE"))
        return {
            "schema": "symphony-pilot-lifecycle-result/v1",
            "task_uuid": packet["task_uuid"], "identifier": packet["identifier"],
            "role_run_id": packet["role_run_id"], "role": role,
            "expected_state": packet["current_state"],
            "expected_workpad_version": packet["workpad"]["version"],
            "expected_starting_head": packet["selected_head"],
            "workpad_body": packet["workpad"]["body"], "summary": outcome,
            "outcome": outcome, "packet": role_packet,
            "findings": findings or [], "requested_resolved_finding_ids": resolved or [],
            "authorized_write_paths": ["src"] if role == "ARCHITECT" and outcome == "planning_complete" else [],
        }

    def _write_result(self, attempt: dict[str, object], result: dict[str, object], *, status: str = "finished") -> None:
        namespace = pathlib.Path(attempt["namespace"])
        (namespace / "outbox" / "result.json").write_text(json.dumps(result), encoding="utf-8")
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        (namespace / "host" / "execution.json").write_text(json.dumps({
            "schema": "symphony-runtime-execution/v1",
            "role": attempt["packet"]["role"], "role_run_id": attempt["packet"]["role_run_id"],
            "status": status, "started_at": now, "finished_at": now,
        }), encoding="utf-8")

    def _reach_planned(self) -> None:
        self.finish(self.attempt(), outcome="role_requested", role="ARCHITECT")
        self.finish(self.attempt(), outcome="role_complete", role="PROJECT-MANAGER", verdict="APPROVE")
        self.finish(self.attempt(), outcome="role_requested", role="ARCHITECT")
        self.finish(self.attempt(), outcome="role_complete", role="PLANNER", verdict="COMPLETE")
        self.finish(self.attempt(), outcome="planning_complete", role="ARCHITECT", authorized_write_paths=["src"])

    def _reach_adversarial_review(self) -> str:
        self._reach_planned()
        (self.workspace / "src" / "implementation.txt").write_text("implemented\n", encoding="utf-8")
        self._git("add", "src/implementation.txt")
        self._git("commit", "-qm", "implementation")
        head = self._git("rev-parse", "HEAD")
        self.finish(self.attempt(), outcome="role_complete", role="IMPLEMENTER", verdict="COMPLETE", head_sha=head)
        self.finish(self.attempt(), outcome="implementation_complete", role="ARCHITECT")
        self.finish(self.attempt(), outcome="role_complete", role="REVIEWER", verdict="APPROVE")
        self.finish(self.attempt(), outcome="review_approved", role="ARCHITECT")
        self.finish(self.attempt(), outcome="role_complete", role="ADVERSARY", verdict="PASS")
        return head

    @staticmethod
    def _current_acceptance(database, task_id: str, event_type: str, head: str) -> bool:
        for event in database.list_events(task_id):
            if event["event_type"] != event_type:
                continue
            try:
                payload = json.loads(event["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("head_sha") == head:
                return True
        return False


Step6LifecycleTests = LifecycleFixture
