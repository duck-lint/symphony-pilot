#!/usr/bin/env python3
"""Validate a project profile without reading its secret."""
from __future__ import annotations
import argparse
import pathlib
import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from prepare_workspace import load_profile, secret_path

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", type=pathlib.Path)
    args = parser.parse_args()
    profile = load_profile(args.profile)
    print(f"valid profile: {profile.slug}")
    print(f"repository: {profile.repository}")
    print(f"workspace_root: {profile.workspace_root}")
    print(f"deployment_root: {profile.deployment_root or '(default)'}")
    print(f"secret path: {secret_path(profile)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
