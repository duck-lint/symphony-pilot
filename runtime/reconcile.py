#!/usr/bin/env python3
"""Pilot-owned Runtime receipt reconciliation boundary."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from lifecycle import LifecycleError, reconcile
from prepare_workspace import load_profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=pathlib.Path)
    parser.add_argument("--workspace", required=True, type=pathlib.Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--phase", required=True, choices=("started", "terminated"))
    args = parser.parse_args(argv)
    try:
        profile = load_profile(args.profile)
        projection = reconcile(profile, args.workspace.resolve(), task_id=args.task_id)
        task = projection.get("task", {})
        active = projection.get("active_execution")
        if args.phase == "started":
            if not isinstance(active, dict) or active.get("dispatch_id") != args.dispatch_id:
                raise LifecycleError("Pilot did not retain the Runtime launch evidence")
        elif isinstance(active, dict) and active.get("dispatch_id") == args.dispatch_id:
            raise LifecycleError("Pilot did not terminalize the Runtime execution")
        print(json.dumps({
            "acknowledged": True,
            "task_id": task.get("id"),
            "dispatch_id": args.dispatch_id,
            "phase": args.phase,
        }, sort_keys=True))
        return 0
    except (LifecycleError, OSError, ValueError, RuntimeError) as exc:
        print(f"symphony-pilot reconciliation stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
