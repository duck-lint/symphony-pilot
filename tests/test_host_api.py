from __future__ import annotations

import http.client
import json
import pathlib
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

import control_db
import host_api
from project_registry import resolve_project


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
        self.start_server("127.0.0.1")

    def start_server(self, host):
        app = host_api.HostControlApplication(self.registry, self.database_path)
        self.server = host_api.create_server(host, 0, app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection(host, self.server.server_address[1], timeout=5)

    def stop_server(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def tearDown(self):
        self.stop_server()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        self.connection.request(method, path, body=body, headers=headers or {})
        response = self.connection.getresponse()
        payload = response.read()
        return response, payload

    def raw_request_status(self, header_lines):
        request = (
            "GET / HTTP/1.1\r\n"
            + "".join(f"{name}: {value}\r\n" for name, value in header_lines)
            + "Connection: close\r\n\r\n"
        ).encode("ascii")
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5) as connection:
            connection.sendall(request)
            response = connection.recv(4096)
        return int(response.split(b"\r\n", 1)[0].split()[1])

    def test_literal_loopback_only_binding(self):
        self.assertEqual(host_api.validate_loopback_bind("127.0.0.1"), "127.0.0.1")
        self.assertEqual(host_api.validate_loopback_bind("::1"), "::1")
        for host in ("0.0.0.0", "192.168.1.2", "localhost"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                host_api.validate_loopback_bind(host)

    def test_canonical_host_is_required_for_every_surface(self):
        valid = self.server.trusted_host
        for path in ("/", "/app.js", "/style.css", "/api/v1/projects"):
            response, _ = self.request("GET", path, headers={"Host": valid})
            self.assertEqual(response.status, 200, path)
        for invalid in (
            f"attacker.example:{self.server.server_port}",
            f"localhost:{self.server.server_port}", "malformed host",
            "127.0.0.1:1", "127.0.0.1", "127.0.0.1.evil:8765",
        ):
            for path in ("/", "/app.js", "/api/v1/projects"):
                with self.subTest(host=invalid, path=path):
                    response, body = self.request("GET", path, headers={"Host": invalid})
                    self.assertEqual(response.status, 421)
                    self.assertNotIn(b"symphony-csrf", body)
                    self.assertNotIn(b'"projects"', body)
        response, _ = self.request("POST", "/api/v1/action", "{}", {"Host": "attacker.example:1"})
        self.assertEqual(response.status, 421)

    def test_host_field_cardinality_is_validated_from_raw_http(self):
        valid = self.server.trusted_host
        cases = (
            ("one canonical Host", (("Host", valid),), 200),
            ("no Host", (), 421),
            ("duplicate canonical Host", (("Host", valid), ("Host", valid)), 421),
            ("canonical then hostile", (("Host", valid), ("Host", f"attacker.example:{self.server.server_port}")), 421),
            ("hostile then canonical", (("Host", f"attacker.example:{self.server.server_port}"), ("Host", valid)), 421),
            ("case-varied duplicate canonical Host", (("Host", valid), ("hOsT", valid)), 421),
            ("localhost", (("Host", f"localhost:{self.server.server_port}"),), 421),
            ("wrong port", (("Host", "127.0.0.1:1"),), 421),
            ("comma-combined Host", (("Host", f"{valid}, {valid}"),), 421),
        )
        for name, headers, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(self.raw_request_status(headers), expected)

    def test_ipv6_canonical_host_where_available(self):
        self.stop_server()
        try:
            self.start_server("::1")
        except OSError as exc:
            self.skipTest(f"IPv6 loopback unavailable: {exc}")
        response, _ = self.request("GET", "/", headers={"Host": self.server.trusted_host})
        self.assertEqual(response.status, 200)
        response, _ = self.request("GET", "/", headers={"Host": f"::1:{self.server.server_port}"})
        self.assertEqual(response.status, 421)

    def test_reads_resolve_authority_redact_and_do_not_mutate(self):
        with control_db.open_database(self.database_path) as database:
            before_task = database.read_task(self.TASK_ID)
            before_events = database.list_events(self.TASK_ID)
        response, body = self.request("GET", "/api/v1/projects")
        self.assertEqual(response.status, 200)
        projects = json.loads(body)["projects"]
        self.assertEqual(projects[0]["repository"], "duck-lint/CLEANROOM")
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        self.assertNotIn("secret_reference", body.decode())
        response, body = self.request("GET", f"/api/v1/projects/cleanroom/tasks/{self.TASK_ID}")
        self.assertEqual(response.status, 200)
        self.assertNotIn("github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ", body.decode())
        self.assertNotIn("very-secret-value", body.decode())
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_task(self.TASK_ID), before_task)
            self.assertEqual(database.list_events(self.TASK_ID), before_events)

    def test_get_does_not_create_or_migrate_database(self):
        missing = pathlib.Path(self.temp.name) / "missing.sqlite3"
        application = host_api.HostControlApplication(self.registry, missing)
        with self.assertRaises(control_db.ControlPlaneError):
            application.projects()
        self.assertFalse(missing.exists())

        old = pathlib.Path(self.temp.name) / "old.sqlite3"
        connection = sqlite3.connect(old)
        connection.execute("PRAGMA user_version = 0")
        connection.execute("CREATE TABLE legacy(value TEXT)")
        connection.close()
        before = old.read_bytes()
        application = host_api.HostControlApplication(self.registry, old)
        with self.assertRaises(control_db.SchemaError):
            application.projects()
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(control_db.inspect_schema_version(old), 0)

    def test_schema_mismatch_fails_without_get_repair(self):
        connection = sqlite3.connect(self.database_path)
        connection.execute("DROP INDEX tasks_project_state_idx")
        connection.close()
        before = self.database_path.read_bytes()
        response, _ = self.request("GET", "/api/v1/projects")
        self.assertEqual(response.status, 400)
        self.assertEqual(self.database_path.read_bytes(), before)
        with self.assertRaises(control_db.SchemaError):
            control_db.open_database_readonly(self.database_path)

    def test_unknown_selectors_and_all_mutations_are_rejected(self):
        cases = (
            "/api/v1/projects/unknown/tasks", "/api/v1/projects/..%2Fcleanroom/tasks",
            "/api/v1/projects/duck-lint%2Fsymphony-canary/tasks",
            "/api/v1/projects/cleanroom/tasks/not-a-uuid",
            "/api/v1/projects/cleanroom/tasks/22222222-2222-2222-2222-222222222222",
        )
        for path in cases:
            response, _ = self.request("GET", path)
            self.assertIn(response.status, (400, 404))
        for path in (f"/api/v1/projects/cleanroom/tasks/{self.TASK_ID}/queue", "/api/v1/action"):
            response, _ = self.request("POST", path, "{}", {"Content-Type": "application/json"})
            self.assertEqual(response.status, 501)
        with control_db.open_database(self.database_path) as database:
            self.assertEqual(database.read_task(self.TASK_ID)["state"], "PREPARED")

    def test_runtime_lock_is_a_record_not_live_verification(self):
        profile = resolve_project("cleanroom", self.registry)
        identity = {"executable": "/does/not/exist", "version": "recorded", "sha256": "a" * 64}
        lock = {"schema": "symphony-pilot-runtime-lock/v1", "symphony": identity,
                "codex": identity, "containment": identity}
        application = host_api.HostControlApplication(self.registry, self.database_path)
        with mock.patch.object(host_api, "_read_json_file", return_value=lock):
            summary = application._runtime_summary(profile)
        self.assertEqual(summary["state"], "recorded")
        self.assertTrue(summary["lock_valid"])
        self.assertEqual(summary["live_verification"], "not_performed")
        self.assertNotIn("accepted", summary.values())


if __name__ == "__main__":
    unittest.main()
