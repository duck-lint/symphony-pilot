from __future__ import annotations

import concurrent.futures
import pathlib
import sqlite3
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
import control_db


class ControlDatabaseTests(unittest.TestCase):
    BASE_TIME = "2026-08-31T12:00:00+00:00"
    BASE_SHA = "a" * 40

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = pathlib.Path(self.temporary.name) / "control.sqlite3"
        self.database = control_db.open_database(self.database_path)

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def task(self, *, task_id: str | None = None, identifier: str | None = None, state: str = "PREPARED") -> dict:
        return self.database.create_task(
            project_slug="demo",
            title="A local task",
            objective="Prove the host persistence contract",
            base_ref="main",
            base_sha=self.BASE_SHA,
            branch="codex/gh-10-aaaaaaaaaaaa",
            task_id=task_id,
            identifier=identifier,
            state=state,
            created_at=self.BASE_TIME,
        )

    def test_empty_initialization_version_wal_foreign_keys_and_idempotence(self):
        self.database.close()
        self.database_path.unlink()
        self.assertEqual(control_db.inspect_schema_version(self.database_path), 0)

        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.schema_version, 1)
            self.assertEqual(database.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(database.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(database.connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            tables = {
                row[0] for row in database.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertNotIn("projects", tables)
            self.assertIn("tasks", tables)

        with control_db.open_database(self.database_path) as reopened:
            self.assertEqual(reopened.schema_version, 1)
            self.assertEqual(reopened.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1)

    def test_newer_and_partial_schemas_fail_closed(self):
        self.database.close()
        newer_path = pathlib.Path(self.temporary.name) / "newer.sqlite3"
        connection = sqlite3.connect(newer_path)
        try:
            connection.execute("PRAGMA user_version = 2")
        finally:
            connection.close()
        with self.assertRaises(control_db.UnsupportedSchemaVersion):
            control_db.open_database(newer_path)
        self.assertEqual(control_db.inspect_schema_version(newer_path), 2)

        partial_path = pathlib.Path(self.temporary.name) / "partial.sqlite3"
        connection = sqlite3.connect(partial_path)
        try:
            connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
        finally:
            connection.close()
        with self.assertRaises(control_db.SchemaError):
            control_db.open_database(partial_path)

    def test_task_identifier_allocation_and_database_uniqueness(self):
        first = self.task(task_id="11111111-1111-1111-1111-111111111111")
        second = self.task(task_id="22222222-2222-2222-2222-222222222222")
        self.assertEqual(first["identifier"], "T-000001")
        self.assertEqual(second["identifier"], "T-000002")
        self.assertEqual(self.database.list_tasks(project_slug="demo", states=["PREPARED"])[0]["id"], first["id"])

        with self.assertRaises(sqlite3.IntegrityError):
            self.task(task_id="33333333-3333-3333-3333-333333333333", identifier="T-000001")
        with self.assertRaises(sqlite3.IntegrityError):
            self.task(task_id="11111111-1111-1111-1111-111111111111", identifier="T-000003")
        with self.assertRaises(ValueError):
            self.task(task_id="not-a-uuid")
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute(
                "INSERT INTO tasks(id, identifier, project_slug, title, objective, state, base_ref, base_sha, branch, created_at, updated_at) "
                "VALUES ('33333333-3333-3333-3333-333333333333', 'T-000003', 'demo', 'x', 'y', 'NOT_A_STATE', 'main', ?, 'b', ?, ?)",
                (self.BASE_SHA, self.BASE_TIME, self.BASE_TIME),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute(
                "INSERT INTO tasks(id, identifier, project_slug, title, objective, state, base_ref, base_sha, branch, created_at, updated_at) "
                "VALUES ('not-a-uuid-3333-3333-3333-333333333333', 'T-000003', 'demo', 'x', 'y', 'PREPARED', 'main', ?, 'b', ?, ?)",
                (self.BASE_SHA, self.BASE_TIME, self.BASE_TIME),
            )

    def test_concurrent_task_creation_allocates_collision_free_identifiers(self):
        self.database.close()

        def create(index: int) -> str:
            with control_db.open_database(self.database_path) as database:
                task = database.create_task(
                    project_slug="demo", title=f"Task {index}", objective="Concurrent allocation",
                    base_ref="main", base_sha=self.BASE_SHA, branch=f"codex/task-{index}",
                    task_id=f"{index:08x}-1111-1111-1111-111111111111",
                )
                return str(task["identifier"])

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            identifiers = list(executor.map(create, range(1, 5)))
        self.assertEqual(sorted(identifiers), ["T-000001", "T-000002", "T-000003", "T-000004"])

    def test_workpad_is_one_current_versioned_row(self):
        task = self.task()
        first = self.database.upsert_workpad(task["id"], "first", updated_at=self.BASE_TIME)
        self.assertEqual(first["version"], 1)
        second = self.database.upsert_workpad(
            task["id"], "second", expected_version=1, updated_at="2026-08-31T12:01:00+00:00"
        )
        self.assertEqual(second["version"], 2)
        self.assertEqual(self.database.read_workpad(task["id"])["body"], "second")
        with self.assertRaises(control_db.StateConflict):
            self.database.upsert_workpad(task["id"], "stale", expected_version=1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute(
                "INSERT INTO workpads(task_id, body, version, updated_at) VALUES (?, 'duplicate', 3, ?)",
                (task["id"], self.BASE_TIME),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute(
                "INSERT INTO workpads(task_id, body, version, updated_at) VALUES (?, 'orphan', 1, ?)",
                ("99999999-9999-9999-9999-999999999999", self.BASE_TIME),
            )

    def test_role_round_uniqueness_and_finding_provenance(self):
        task = self.task()
        run = self.database.create_role_run(task["id"], "REVIEWER", 1, started_at=self.BASE_TIME)
        self.assertIn("role_started", [event["event_type"] for event in self.database.list_events(task["id"])])
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.create_role_run(task["id"], "REVIEWER", 1)
        finished = self.database.finish_role_run(
            run["id"], result_summary="accepted", finished_at="2026-08-31T12:01:00+00:00"
        )
        self.assertEqual(finished["status"], "finished")
        finding = self.database.record_finding(
            task_id=task["id"], role_run_id=run["id"], kind="review", severity="high", body="Fix required"
        )
        self.assertEqual(finding["task_id"], task["id"])
        licensed = self.database.record_finding(
            task_id=task["id"], role_run_id=run["id"], kind="correction", severity="medium",
            body="Correction is licensed", status="licensed", licensed_correction_round=1,
        )
        self.assertEqual(licensed["licensed_correction_round"], 1)
        other = self.task(task_id="33333333-3333-3333-3333-333333333333")
        other_run = self.database.create_role_run(other["id"], "ADVERSARY", 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.record_finding(
                task_id=task["id"], role_run_id=other_run["id"],
                kind="cross-task", severity="critical", body="Must reject"
            )

    def test_blocker_and_optional_publication_lifecycle(self):
        task = self.task()
        self.assertIsNone(self.database.read_publication(task["id"]))
        blocker = self.database.record_blocker(
            task_id=task["id"], kind="infrastructure", body="WSL capability unavailable", created_at=self.BASE_TIME
        )
        self.assertEqual(blocker["status"], "open")
        self.assertIn(
            "infrastructure_blocked",
            [event["event_type"] for event in self.database.list_events(task["id"])],
        )
        resolved = self.database.resolve_blocker(
            blocker["id"], resolved_at="2026-08-31T12:01:00+00:00"
        )
        self.assertEqual(resolved["status"], "resolved")
        with self.assertRaises(control_db.StateConflict):
            self.database.resolve_blocker(blocker["id"])

        publication = self.database.record_publication(
            task_id=task["id"], publication_status="published", head_sha=self.BASE_SHA,
            remote_branch="codex/task", github_pr_number=7,
            published_at="2026-08-31T12:02:00+00:00",
        )
        self.assertEqual(publication["github_pr_number"], 7)
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute(
                "INSERT INTO publications(task_id, publication_status) VALUES (?, 'started')",
                ("99999999-9999-9999-9999-999999999999",),
            )

    def test_events_and_compare_and_set_transition_are_atomic(self):
        task = self.task()
        transitioned = self.database.transition_task(
            task["id"], expected_state="PREPARED", new_state="QUEUED", event_type="queued",
            payload={"source": "host"}, occurred_at=self.BASE_TIME,
        )
        self.assertEqual(transitioned["state"], "QUEUED")
        self.assertIn("queued", [event["event_type"] for event in self.database.list_events(task["id"])])
        with self.assertRaises(control_db.StateConflict):
            self.database.transition_task(
                task["id"], expected_state="PREPARED", new_state="PLANNED", event_type="architect_started"
            )
        self.assertEqual(self.database.read_task(task["id"])["state"], "QUEUED")

        self.database.connection.execute(
            """
            CREATE TRIGGER reject_planning_event
            BEFORE INSERT ON task_events
            WHEN NEW.event_type = 'architect_started'
            BEGIN
                SELECT RAISE(ABORT, 'synthetic event failure');
            END
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.transition_task(
                task["id"], expected_state="QUEUED", new_state="PLANNED", event_type="architect_started"
            )
        self.assertEqual(self.database.read_task(task["id"])["state"], "QUEUED")
        self.assertNotIn("architect_started", [event["event_type"] for event in self.database.list_events(task["id"])])

        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute(
                "INSERT INTO task_events(id, task_id, event_type, payload_json, occurred_at) VALUES (?, ?, 'queued', 'not-json', ?)",
                ("44444444-4444-4444-4444-444444444444", task["id"], self.BASE_TIME),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute(
                "INSERT INTO blockers(id, task_id, kind, body, status, created_at, resolved_at) VALUES (?, ?, 'human', 'bad', 'open', ?, ?)",
                ("55555555-5555-5555-5555-555555555555", task["id"], self.BASE_TIME, self.BASE_TIME),
            )

    def test_projection_contains_current_state_and_history(self):
        task = self.task()
        self.database.upsert_workpad(task["id"], "workpad")
        run = self.database.create_role_run(task["id"], "PROJECT-MANAGER", 1)
        self.database.finish_role_run(run["id"])
        projection = self.database.read_projection(task["id"])
        self.assertEqual(projection["task"]["id"], task["id"])
        self.assertEqual(projection["workpad"]["version"], 1)
        self.assertEqual(len(projection["role_runs"]), 1)
        self.assertEqual(len(projection["events"]), 3)

    def test_backup_restore_round_trip_and_explicit_replacement(self):
        first = self.task()
        self.database.upsert_workpad(first["id"], "durable workpad")
        backup_path = pathlib.Path(self.temporary.name) / "backup.sqlite3"
        self.database.backup_to(backup_path)
        self.assertTrue(backup_path.is_file())
        self.assertEqual(control_db.inspect_schema_version(backup_path), 1)

        self.task(task_id="33333333-3333-3333-3333-333333333333")
        self.database.close()
        with self.assertRaises(FileExistsError):
            control_db.ControlPlaneDatabase.restore_from(backup_path, self.database_path)
        control_db.ControlPlaneDatabase.restore_from(backup_path, self.database_path, replace=True)
        with control_db.open_database(self.database_path) as restored:
            self.assertEqual([task["identifier"] for task in restored.list_tasks()], ["T-000001"])
            self.assertEqual(restored.read_workpad(first["id"])["body"], "durable workpad")

    def test_invalid_backup_never_replaces_existing_authority(self):
        first = self.task()
        backup_path = pathlib.Path(self.temporary.name) / "bad-backup.sqlite3"
        target_path = pathlib.Path(self.temporary.name) / "restore-target.sqlite3"
        self.database.backup_to(backup_path)
        connection = sqlite3.connect(backup_path)
        try:
            connection.execute("PRAGMA user_version = 2")
        finally:
            connection.close()
        with control_db.open_database(target_path) as target:
            target.create_task(
                project_slug="demo", title="Keep this", objective="The failed restore must not replace me",
                base_ref="main", base_sha=self.BASE_SHA, branch="codex/keep",
            )
        with self.assertRaises(control_db.UnsupportedSchemaVersion):
            control_db.ControlPlaneDatabase.restore_from(backup_path, target_path, replace=True)
        with control_db.open_database(target_path) as target:
            self.assertEqual([task["title"] for task in target.list_tasks()], ["Keep this"])

    def test_rendered_containment_workflow_does_not_expose_control_database(self):
        from render_workflow import render
        from prepare_workspace import Profile

        profile = Profile(
            slug="demo", repository="example/project", git_remote="git@example:project.git",
            workspace_root=pathlib.Path("/home/operator/symphony-workspaces/demo"),
            state_root=pathlib.Path("/home/operator/.local/state/symphony-pilot/demo"),
            log_root=pathlib.Path("/home/operator/.local/state/symphony-pilot/demo/logs"),
            secret_reference="github.token", trusted_dispatchers=("duck-lint",),
            dispatch_labels=("symphony:auto",), blocked_label="symphony:human",
            service_identity="symphony-pilot-demo", dashboard_port=4040,
            max_concurrent_agents=1, max_turns=8, poll_interval_ms=1000,
            max_retry_backoff_ms=1000, codex_model="gpt-5.6-luna",
            codex_reasoning_effort="high", toolchain=None,
        )
        with tempfile.TemporaryDirectory() as directory:
            policy = pathlib.Path(directory) / "policy.md"
            policy.write_text("policy\n", encoding="utf-8")
            rendered = render(profile, pathlib.Path(directory), policy)
        self.assertNotIn("control.sqlite3", rendered)
        self.assertNotIn(".local/state/symphony-pilot/control", rendered)

    def test_task_deletion_is_restricted_by_audit_history(self):
        task = self.task()
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))


if __name__ == "__main__":
    unittest.main(verbosity=2)
