#!/usr/bin/env python3
"""Best-effort host reconciliation after an architect attempt."""

from __future__ import annotations

import argparse
import pathlib
import re

from prepare_workspace import comments, compact_workpad, github, load_profile, read_secret, workpad


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=pathlib.Path)
    parser.add_argument("--workspace", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        profile = load_profile(args.profile)
        token = read_secret(profile)
        match = re.fullmatch(r"GH-(\d+)", args.workspace.name)
        if not match:
            return 0
        issue = int(match.group(1))
        items = comments(profile, token, issue)
        pad = workpad(items)
        text = "\n".join(item.get("body") or "" for item in items)
        if not re.search(r"403.*Resource not accessible by personal access token|publication.*permission", text, re.I):
            return 0
        labels = github(profile, token, "GET", f"/issues/{issue}/labels?per_page=100")
        names = [item["name"] for item in labels if item.get("name") not in set(profile.dispatch_labels) | {profile.blocked_label}]
        github(profile, token, "PUT", f"/issues/{issue}/labels", {"labels": names + [profile.blocked_label]})
        if pad:
            body = compact_workpad(pad.get("body") or "")
            if "Infrastructure publication blocker" not in body:
                body += "\n\n### Infrastructure publication blocker\n- GitHub publication authority rejected the required operation; automatic retry is stopped.\n"
                github(profile, token, "PATCH", f"/issues/comments/{pad['id']}", {"body": body})
    except Exception as exc:  # after_run is best effort; never print a secret-bearing traceback
        print(f"symphony-pilot after_run warning: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
