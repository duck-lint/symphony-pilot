#!/usr/bin/env python3
"""Host reconciliation after a task turn.

Agent prose and workpad markers are payload only. The host reads one admitted
task record and, when present, one strict task outbox request.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re

from host_integration import _safe_summary
from outbox import OutboxError
from prepare_workspace import load_profile
from broker import BrokerError, process_result, record_blocker


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=pathlib.Path)
    parser.add_argument("--workspace", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        profile = load_profile(args.profile)
    except Exception as exc:
        print(f"symphony-pilot after_run blocker: {type(exc).__name__}")
        return 78
    workspace = args.workspace.resolve()
    match = re.fullmatch(r"GH-(\d+)", workspace.name)
    if not match:
        return 78
    issue = int(match.group(1))
    try:
        result = process_result(profile, workspace)
        print(json.dumps({"issue": issue, "status": "processed"}, sort_keys=True))
        return result
    except OutboxError as exc:
        record_blocker(profile, issue, "task_outbox", str(exc))
        print(f"symphony-pilot after_run blocker: invalid task outbox: {_safe_summary(str(exc))}")
        return 78
    except Exception as exc:  # never print a secret-bearing traceback
        record_blocker(profile, issue, "host_broker", str(exc))
        print(f"symphony-pilot after_run blocker: {type(exc).__name__}")
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
