from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import serve_control_ui


class ServeControlUiTests(unittest.TestCase):
    def test_main_uses_current_server_contract_and_closes(self):
        class FakeServer:
            trusted_host = "[::1]:4321"

            def __init__(self):
                self.served = False
                self.closed = False

            def serve_forever(self):
                self.served = True
                raise KeyboardInterrupt

            def server_close(self):
                self.closed = True

        server = FakeServer()
        database_path = pathlib.Path("/host/control.sqlite3")
        application = object()
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["serve_control_ui.py", "--host", "::1", "--port", "4321"]),
            mock.patch.object(serve_control_ui, "default_database_path", return_value=database_path),
            mock.patch.object(serve_control_ui, "HostControlApplication", return_value=application) as app_type,
            mock.patch.object(serve_control_ui, "create_server", return_value=server) as create_server,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(serve_control_ui.main(), 0)

        app_type.assert_called_once_with(ROOT / "projects", database_path)
        create_server.assert_called_once_with("::1", 4321, application)
        self.assertEqual(output.getvalue(), "Pilot control UI: http://[::1]:4321\n")
        self.assertTrue(server.served)
        self.assertTrue(server.closed)


if __name__ == "__main__":
    unittest.main()
