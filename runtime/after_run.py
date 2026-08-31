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

from host_integration import clear_notification, notify, _safe_summary
from outbox import OutboxError, read_request, task_outbox_path
from prepare_workspace import github, load_profile, read_secret
from task_admission import read_task, task_state_path


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
        task = read_task(task_state_path(pathlib.Path(profile.state_root), issue))
        task_path = task_state_path(pathlib.Path(profile.state_root), issue)
        outbox = task_outbox_path(task_path)
        if outbox.is_file():
            request = read_request(outbox, task)
            print(json.dumps({"task_id": task["task_id"], "request": request}, sort_keys=True))
        token = read_secret(profile)
        issue_json = github(profile, token, "GET", f"/issues/{issue}")
        issue_url = f"https://github.com/{profile.repository}/issues/{issue}"
        if isinstance(issue_json, dict) and issue_json.get("state") == "closed":
            notify(profile, "completed", issue, f"Issue #{issue} completed.", issue_url,
                   f"completed:GH-{issue}:{issue_json.get('updated_at', '')}")
        elif isinstance(issue_json, dict):
            clear_notification(profile, "completed", issue)
    except OutboxError as exc:
        print(f"symphony-pilot after_run blocker: invalid task outbox: {_safe_summary(str(exc))}")
    except Exception as exc:  # never print a secret-bearing traceback
        print(f"symphony-pilot after_run warning: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
