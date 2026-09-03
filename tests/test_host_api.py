from __future__ import annotations

import http.client
import json
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

import control_db
import host_api


class HostApiTests(unittest.TestCase):
    TASK_ID = "11111111-1111-1111-1111-111111111111"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp.name)
        self.registry = root / "projects"
        (self.registry / "cleanroom").mkdir(parents=True)
        shutil.copy2(ROOT / "projects/cleanroom/profile.toml", self.registry / "cleanroom/profile.toml")
        self.database_path = root / "control.sqlite3"
        with control_db.open_database(self.database_path) as database:
            database.create_task(
                project_slug="cleanroom", title="Local task",
                objective="Do not leak github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                base_ref="main", base_sha="a" * 40, branch="codex/test",
                task_id=self.TASK_ID,
            )
            database.upsert_workpad(self.TASK_ID, "Bearer very-secret-value")
        app = host_api.HostControlApplication(self.registry, self.database_path)
        self.server = host_api.create_server("127.0.0.1", 0, app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        self.connection.request(method, path, body=body, headers=headers or {})
        response = self.connection.getresponse()
        payload = response.read()
        return response, payload

    def browser_session(self):
        response, body = self.request("GET", "/")
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        marker = b'<meta name="symphony-csrf" content="'
        csrf = body.split(marker, 1)[1].split(b'"', 1)[0].decode()
        return cookie, csrf

    def mutation_headers(self):
        cookie, csrf = self.browser_session()
        return {"Origin": self.server.trusted_origin, "Cookie": cookie,
                "X-Symphony-CSRF": csrf, "Content-Type": "application/json"}

    def test_literal_loopback_only_binding(self):
        self.assertEqual(host_api.validate_loopback_bind("127.0.0.1"), "127.0.0.1")
        self.assertEqual(host_api.validate_loopback_bind("::1"), "::1")
        for host in ("0.0.0.0", "192.168.1.2", "localhost"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                host_api.validate_loopback_bind(host)

    def test_reads_resolve_registered_authority_and_redact_credentials(self):
        response, body = self.request("GET", "/api/v1/projects")
        self.assertEqual(response.status, 200)
        projects = json.loads(body)["projects"]
        self.assertEqual(projects[0]["repository"], "duck-lint/CLEANROOM")
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        serialized = body.decode()
        self.assertNotIn("secret_reference", serialized)
        self.assertNotIn("SYMPHONY_PILOT_GITHUB_TOKEN", serialized)

        response, body = self.request("GET", f"/api/v1/projects/cleanroom/tasks/{self.TASK_ID}")
        self.assertEqual(response.status, 200)
        self.assertNotIn("github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ", body.decode())
        self.assertNotIn("very-secret-value", body.decode())
        self.assertIn("[REDACTED CREDENTIAL]", body.decode())

    def test_unknown_project_paths_repositories_and_task_ids_are_not_authority(self):
        cases = (
            "/api/v1/projects/unknown/tasks",
            "/api/v1/projects/..%2Fcleanroom/tasks",
            "/api/v1/projects/duck-lint%2Fsymphony-canary/tasks",
            "/api/v1/projects/cleanroom/tasks/not-a-uuid",
            "/api/v1/projects/cleanroom/tasks/22222222-2222-2222-2222-222222222222",
        )
        for path in cases:
            with self.subTest(path=path):
                response, _ = self.request("GET", path)
                self.assertIn(response.status, (400, 404))

    def test_only_closed_queue_command_exists_and_rejects_authority_fields(self):
        headers = self.mutation_headers()
        forbidden = {
            "sql": "DROP TABLE tasks", "project_path": "/tmp/project",
            "repository": "attacker/repo", "base_sha": "b" * 40,
            "published_head": "c" * 40, "remote_ref": "refs/heads/evil",
            "command": "sh",
        }
        response, _ = self.request(
            "POST", f"/api/v1/projects/cleanroom/tasks/{self.TASK_ID}/queue",
            json.dumps(forbidden), headers,
        )
        self.assertEqual(response.status, 400)
        response, _ = self.request("POST", "/api/v1/action", "{}", headers)
        self.assertEqual(response.status, 404)
        with control_db.open_database(self.database_path) as database:
            task = database.read_task(self.TASK_ID)
            self.assertEqual(task["state"], "PREPARED")
            self.assertEqual(task["base_sha"], "a" * 40)
            self.assertIsNone(task["published_head"])

    def test_origin_csrf_and_non_simple_content_type_are_required(self):
        path = f"/api/v1/projects/cleanroom/tasks/{self.TASK_ID}/queue"
        cookie, csrf = self.browser_session()
        invalid = (
            {},
            {"Origin": "https://hostile.example", "Cookie": cookie,
             "X-Symphony-CSRF": csrf, "Content-Type": "application/json"},
            {"Origin": self.server.trusted_origin, "Cookie": cookie,
             "Content-Type": "application/json"},
            {"Origin": self.server.trusted_origin, "Cookie": cookie,
             "X-Symphony-CSRF": "wrong", "Content-Type": "application/json"},
            {"Origin": self.server.trusted_origin, "Cookie": cookie,
             "X-Symphony-CSRF": csrf, "Content-Type": "text/plain"},
        )
        for headers in invalid:
            with self.subTest(headers=headers):
                response, _ = self.request("POST", path, "{}", headers)
                self.assertIn(response.status, (403, 415))
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "PREPARED")

    def test_get_never_mutates_and_queue_is_atomic_compare_and_set(self):
        with control_db.open_database(self.database_path) as database:
            before = database.list_events(self.TASK_ID)
        response, _ = self.request("GET", f"/api/v1/projects/cleanroom/tasks/{self.TASK_ID}")
        self.assertEqual(response.status, 200)
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.list_events(self.TASK_ID), before)

        headers = self.mutation_headers()
        path = f"/api/v1/projects/cleanroom/tasks/{self.TASK_ID}/queue"
        response, _ = self.request("POST", path, "{}", headers)
        self.assertEqual(response.status, 200)
        response, _ = self.request("POST", path, "{}", headers)
        self.assertEqual(response.status, 409)
        with control_db.open_database(self.database_path) as database:
            task = database.read_task(self.TASK_ID)
            events = database.list_events(self.TASK_ID)
        self.assertEqual(task["state"], "QUEUED")
        self.assertEqual([event["event_type"] for event in events].count("queued"), 1)
        self.assertEqual(json.loads(events[-1]["payload_json"]), {"source": "host_api"})


if __name__ == "__main__":
    unittest.main()
