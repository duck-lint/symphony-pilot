#!/usr/bin/env python3
"""Trusted preparation plus Step-6 Architect-attempt allocation."""
from __future__ import annotations

import argparse
import pathlib
import sys

from lifecycle import LifecycleError, prepare_attempt
from prepare_workspace import PreparationError, load_profile, prepare


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=pathlib.Path)
    parser.add_argument("--workspace", required=True, type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        profile = load_profile(args.profile)
        prepare(profile, args.workspace)
        prepare_attempt(profile, args.workspace)
        return 0
    except (PreparationError, LifecycleError, OSError, ValueError) as exc:
        print(f"symphony-pilot before_run stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
