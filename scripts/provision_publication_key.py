#!/usr/bin/env python3
"""Provision the deterministic project publication deploy-key file."""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from prepare_workspace import publication_key_path  # noqa: E402
from project_registry import resolve_project  # noqa: E402


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    try:
        profile = resolve_project(args.project, ROOT / "projects")
        path = publication_key_path(profile)
    except Exception as exc:
        print(f"publication-key provisioning stopped: {type(exc).__name__}", file=sys.stderr)
        return 78
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    print(f"Paste publication deploy key for {profile.slug}, then signal EOF (input is not echoed):")
    value = sys.stdin.read().strip()
    if not value or not value.startswith("-----BEGIN ") or "PRIVATE KEY-----" not in value:
        raise SystemExit("publication key must be a PEM private key")
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(value + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    print(f"provisioned {path} with mode 0600")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
