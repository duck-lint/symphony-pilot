#!/usr/bin/env python3
"""Best-effort host reconciliation after an architect attempt."""

from __future__ import annotations

import argparse
import pathlib
import re

from host_integration import clear_notification, notify, _safe_summary
from prepare_workspace import comments, compact_workpad, github, load_profile, read_secret, workpad


def current_blocker_body(workpad_body: str) -> str:
    """Exclude preserved history from the active blocker decision."""
    return workpad_body.split("### Preserved history", 1)[0]


def is_infrastructure_blocker(workpad_body: str) -> bool:
    return re.search(r"^### Infrastructure(?: publication)? blocker\b",
                     current_blocker_body(workpad_body), re.I | re.M) is not None


def infrastructure_message(workpad_body: str) -> str:
    current = current_blocker_body(workpad_body)
    detail = re.search(r"^- detail:\s*(.+)", current, re.I | re.M)
    if not detail:
        return "Autonomous work is paused by an infrastructure/provider blocker."
    return "Infrastructure service requires attention: " + _safe_summary(detail.group(1))


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
        pad_body = (pad or {}).get("body") or ""
        current_body = current_blocker_body(pad_body)
        labels = github(profile, token, "GET", f"/issues/{issue}/labels?per_page=100")
        names = [item["name"] for item in labels]
        publication_failure = re.search(
            r"403.*Resource not accessible by personal access token|publication.*permission",
            current_body, re.I)
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
                current_body = current_blocker_body(body)
        issue_url = f"https://github.com/{profile.repository}/issues/{issue}"
        if profile.blocked_label in names:
            infrastructure = is_infrastructure_blocker(current_body)
            if infrastructure:
                clear_notification(profile, "human", issue)
                detail = infrastructure_message(current_body)
                notify(profile, "infrastructure", issue, detail, issue_url,
                       f"infrastructure:GH-{issue}:{current_body}")
            else:
                clear_notification(profile, "infrastructure", issue)
                notify(profile, "human", issue,
                       f"Issue #{issue} is safely paused and requires human attention.",
                       issue_url, f"human:GH-{issue}:{current_body}")
        else:
            clear_notification(profile, "human", issue)
            clear_notification(profile, "infrastructure", issue)
        issue_json = github(profile, token, "GET", f"/issues/{issue}")
        if isinstance(issue_json, dict) and issue_json.get("state") == "closed":
            notify(profile, "completed", issue,
                   f"Issue #{issue} completed.", issue_url,
                   f"completed:GH-{issue}:{issue_json.get('updated_at', '')}")
        elif isinstance(issue_json, dict):
            clear_notification(profile, "completed", issue)
    except Exception as exc:  # after_run is best effort; never print a secret-bearing traceback
        print(f"symphony-pilot after_run warning: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
