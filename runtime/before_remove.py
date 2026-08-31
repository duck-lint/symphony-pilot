#!/usr/bin/env python3
"""Straight-cutover workspace removal hook.

Execution workspaces are disposable. The hook does not archive, reconcile, or
import task-local state; only a host-admitted recovery artifact may survive,
and that path is deliberately outside this hook.
"""
from __future__ import annotations

import argparse
import pathlib

from prepare_workspace import load_profile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=pathlib.Path)
    parser.add_argument("--workspace", required=True, type=pathlib.Path)
    args = parser.parse_args()
    load_profile(args.profile)
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        return 0
    # No model-generated workspace state is promoted or copied during removal.
    print(f"symphony-pilot: disposable task workspace removed from lifecycle: {workspace.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
