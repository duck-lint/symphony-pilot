#!/usr/bin/env python3
"""Reconcile one hostile Architect lifecycle result into Pilot SQLite.

The after_run exit status is not an activation barrier; SQLite blocker side
effects are the scheduler barrier.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from lifecycle import fail_attempt_for_workspace, reconcile
from prepare_workspace import load_profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=pathlib.Path)
    parser.add_argument("--workspace", required=True, type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        profile = load_profile(args.profile)
        reconcile(profile, args.workspace)
        return 0
    except Exception as exc:
        # Runtime treats after_run as best-effort. Persisting the blocker is
        # therefore the safety boundary, not this process's exit status.
        try:
            profile = load_profile(args.profile)
            fail_attempt_for_workspace(profile, args.workspace, f"lifecycle reconciliation failed: {type(exc).__name__}")
        except Exception:
            pass
        print(f"symphony-pilot after_run stopped: {type(exc).__name__}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
