#!/usr/bin/env python3
"""Preserve dirty execution state before a workspace is removed."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from prepare_workspace import archive_recovery, git, issue_facts, load_profile, read_secret

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=pathlib.Path)
    parser.add_argument("--workspace", required=True, type=pathlib.Path)
    args = parser.parse_args()
    profile = load_profile(args.profile)
    workspace = args.workspace.resolve()
    status = git(workspace, "status", "--porcelain=v1", "--untracked-files=all", check=False)
    if not status:
        return 0
    token = read_secret(profile)
    facts = issue_facts(profile, workspace, token)
    archive = archive_recovery(profile, workspace, facts, status)
    print(json.dumps({"issue": facts.issue, "archive": str(archive),
                      "created_utc": dt.datetime.now(dt.timezone.utc).isoformat()}, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
