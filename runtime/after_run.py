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
import datetime as dt

from host_integration import _safe_summary
from outbox import OutboxError, read_request, task_outbox_path
from prepare_workspace import load_profile, require_physical_namespace
from publication import PublicationError, validate_publication_request
from task_admission import read_task, task_state_path


def record_blocker(profile, issue: int, kind: str, detail: str) -> None:
    """Persist host-side failure detail without echoing task-controlled payload."""
    path = require_physical_namespace(profile.state_root) / "blockers" / f"GH-{issue}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": "symphony-pilot-blocker/v1",
        "issue": issue,
        "kind": kind,
        "detail": _safe_summary(detail),
        "status": "active",
        "recorded_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=pathlib.Path)
    parser.add_argument("--workspace", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        profile = load_profile(args.profile)
        workspace = args.workspace.resolve()
        match = re.fullmatch(r"GH-(\d+)", workspace.name)
        if not match:
            return 0
        issue = int(match.group(1))
        task_path = task_state_path(require_physical_namespace(profile.state_root), issue)
        task = read_task(task_path)
        outbox = task_outbox_path(task_path)
        if not outbox.is_file():
            raise OutboxError("task produced no publication request")
        request = read_request(outbox, task)
        print(json.dumps({"task_id": task["task_id"], "request": request}, sort_keys=True))
        validate_publication_request(outbox, workspace, task)
        # There is deliberately no task-side publication path. Until the
        # host publication service is separately enabled, accepted intent is
        # still a blocker rather than a false completion signal.
        record_blocker(profile, issue, "publication_unavailable",
                       "strict publication request validated but host publication is not enabled")
        print("symphony-pilot after_run blocker: host publication is not enabled")
        return 78
    except OutboxError as exc:
        if "issue" in locals() and "profile" in locals():
            record_blocker(profile, issue, "task_outbox", str(exc))
        print(f"symphony-pilot after_run blocker: invalid task outbox: {_safe_summary(str(exc))}")
        return 78
    except PublicationError as exc:
        if "issue" in locals() and "profile" in locals():
            record_blocker(profile, issue, "publication", str(exc))
        print(f"symphony-pilot after_run blocker: publication rejected: {_safe_summary(str(exc))}")
        return 78
    except Exception as exc:  # never print a secret-bearing traceback
        print(f"symphony-pilot after_run warning: {type(exc).__name__}")
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
