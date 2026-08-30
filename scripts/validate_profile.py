#!/usr/bin/env python3
"""Validate one profile or the complete canonical registry without reading secrets."""
from __future__ import annotations
import argparse
import pathlib
import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from prepare_workspace import PreparationError, load_profile, secret_path
from project_registry import validate_registry

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", nargs="?", type=pathlib.Path,
                        help="optional profile; omit to validate the complete registry")
    args = parser.parse_args()
    try:
        if args.profile:
            profile = load_profile(args.profile)
            print(f"valid profile: {profile.slug} ({profile.repository})")
            print(f"workspace_root: {profile.workspace_root}")
            print(f"secret namespace: {secret_path(profile).parent}")
        else:
            profiles = validate_registry(ROOT / "projects")
            print(f"valid registry: {len(profiles)} project(s)")
            for profile in profiles:
                print(f"- {profile.slug} ({profile.repository})")
        print("No credential was read.")
        return 0
    except PreparationError as exc:
        print(f"validation stopped: {exc.kind}: {exc}", file=sys.stderr)
        return 78

if __name__ == "__main__":
    raise SystemExit(main())
