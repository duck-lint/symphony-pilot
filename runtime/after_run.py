#!/usr/bin/env python3
"""Explicit Step-6 lifecycle boundary for the managed workflow.

Step 5 makes SQLite the scheduler authority but does not yet migrate the
full role/workpad/publication lifecycle. A task that reaches this hook is
therefore blocked closed; it is never reconstructed as a GitHub issue.
"""
from __future__ import annotations

import argparse
import pathlib
import re

from prepare_workspace import load_profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=pathlib.Path)
    parser.add_argument("--workspace", required=True, type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        profile = load_profile(args.profile)
    except Exception as exc:
        print(f"symphony-pilot after_run blocker: {type(exc).__name__}")
        return 78
    workspace = args.workspace.resolve()
    if not re.fullmatch(r"T-(\d{6})", workspace.name):
        return 78
    print("symphony-pilot after_run blocked: lifecycle reconciliation is deferred to Step 6")
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
