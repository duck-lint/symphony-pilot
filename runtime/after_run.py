#!/usr/bin/env python3
"""Best-effort host reconciliation after an architect attempt."""

from __future__ import annotations

import argparse
import pathlib
import re

from host_integration import notify
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
        labels = github(profile, token, "GET", f"/issues/{issue}/labels?per_page=100")
        names = [item["name"] for item in labels]
        publication_failure = re.search(
            r"403.*Resource not accessible by personal access token|publication.*permission",
            text, re.I)
        if publication_failure and profile.blocked_label not in names:
            retained = [name for name in names
                        if name not in set(profile.dispatch_labels) | {profile.blocked_label}]
            github(profile, token, "PUT", f"/issues/{issue}/labels",
                   {"labels": retained + [profile.blocked_label]})
            names.append(profile.blocked_label)
        if publication_failure and pad:
            body = compact_workpad(pad.get("body") or "")
            if "Infrastructure publication blocker" not in body:
                body += ("\n\n### Infrastructure publication blocker\n"
                         "- GitHub publication authority rejected the required operation; "
                         "automatic retry is stopped.\n")
                github(profile, token, "PATCH", f"/issues/comments/{pad['id']}", {"body": body})
        issue_url = f"https://github.com/{profile.repository}/issues/{issue}"
        pad_body = (pad or {}).get("body") or ""
        if profile.blocked_label in names:
            infrastructure = "infrastructure blocker" in (text + "\n" + pad_body).lower()
            if infrastructure:
                detail = "Autonomous work is paused by an infrastructure/provider blocker."
                detail_match = re.search(r"- detail:\s*(.+)", pad_body, re.I)
                if detail_match:
                    detail = detail_match.group(1)
                notify(profile, "infrastructure", issue, detail, issue_url,
                       f"infrastructure:GH-{issue}:{detail}")
            else:
                notify(profile, "human", issue,
                       f"Issue #{issue} is safely paused and requires human attention.",
                       issue_url, f"human:GH-{issue}")
        issue_json = github(profile, token, "GET", f"/issues/{issue}")
        if isinstance(issue_json, dict) and issue_json.get("state") == "closed":
            notify(profile, "completed", issue,
                   f"Issue #{issue} completed.", issue_url,
                   f"completed:GH-{issue}:{issue_json.get('updated_at', '')}")
    except Exception as exc:  # after_run is best effort; never print a secret-bearing traceback
        print(f"symphony-pilot after_run warning: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
