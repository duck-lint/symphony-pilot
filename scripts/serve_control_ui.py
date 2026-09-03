#!/usr/bin/env python3
"""Serve the Pilot-owned operator UI on a literal loopback address."""
from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from control_db import default_database_path
from host_api import HostControlApplication, create_server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    application = HostControlApplication(ROOT / "projects", default_database_path())
    server = create_server(args.host, args.port, application)
    print(f"Pilot control UI: http://{server.trusted_host}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
