#!/usr/bin/env python3
"""Provision one host secret without putting its value in shell history."""
from __future__ import annotations
import argparse
import getpass
import os
import pathlib
import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from prepare_workspace import PreparationError, require_physical_namespace, secret_path
from project_registry import resolve_project

def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, help="registered project slug")
    args = parser.parse_args(argv)
    try:
        profile = resolve_project(args.project, ROOT / "projects")
        path = require_physical_namespace(secret_path(profile))
    except PreparationError as exc:
        print(f"symphony-pilot project resolution stopped: {exc}", file=sys.stderr)
        return 78
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    value = getpass.getpass(f"Enter tracker credential for {profile.slug} (hidden): ")
    if not value or "\n" in value or "\r" in value:
        raise SystemExit("credential must be a non-empty single line")
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(value + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    print(f"provisioned {path} with mode 0600")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
