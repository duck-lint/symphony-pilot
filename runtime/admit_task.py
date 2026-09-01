#!/usr/bin/env python3
"""Create the host task record at dispatch time.

The only Git facts used here come from the GitHub API response.  The issue
body and comments are never inspected for refs, SHAs, branches, or identity.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from prepare_workspace import github, load_profile, read_secret, require_physical_namespace  # noqa: E402
from dispatch_provenance import DispatchProvenanceError, fetch_all_events, prove_dispatch  # noqa: E402
from runtime_lock import validate_lock  # noqa: E402
from task_admission import ServerAdmission, create_task, read_task, task_state_path, write_task  # noqa: E402


def issue_number(workspace: pathlib.Path) -> int:
    match = re.fullmatch(r"GH-(\d+)", workspace.name)
    if not match:
        raise ValueError("workspace identity must be GH-N")
    return int(match.group(1))


def admit(profile_path: pathlib.Path, workspace: pathlib.Path, runtime_lock_path: pathlib.Path) -> pathlib.Path:
    profile = load_profile(profile_path)
    issue = issue_number(workspace.resolve())
    state_path = task_state_path(require_physical_namespace(profile.state_root), issue)
    if state_path.exists():
        record = read_task(state_path)
        if record["repository"] != profile.repository or record["project_slug"] != profile.slug:
            raise ValueError("existing task record belongs to another project")
        return state_path

    lock_path = require_physical_namespace(runtime_lock_path.resolve())
    if lock_path != require_physical_namespace(profile.state_root) / "runtime-lock.json":
        raise ValueError("runtime lock must come from the project host state namespace")
    lock = validate_lock(json.loads(lock_path.read_text(encoding="utf-8")))
    token = read_secret(profile)
    repository = github(profile, token, "GET", "")
    if not isinstance(repository, dict) or not isinstance(repository.get("default_branch"), str):
        raise ValueError("GitHub did not return a trusted default branch")
    default_ref = repository["default_branch"]
    ref = github(profile, token, "GET", f"/git/ref/heads/{default_ref}")
    base_sha = ((ref.get("object") or {}).get("sha") if isinstance(ref, dict) else None)
    if not isinstance(base_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        raise ValueError("GitHub did not return a trusted default-branch HEAD")
    issue_data = github(profile, token, "GET", f"/issues/{issue}")
    if not isinstance(issue_data, dict):
        raise ValueError("GitHub issue metadata is unavailable")
    events = fetch_all_events(lambda page: github(
        profile, token, "GET", f"/issues/{issue}/events?per_page=100&page={page}"))
    try:
        provenance = prove_dispatch(issue_data, events, profile.dispatch_labels, profile.trusted_dispatchers)
    except DispatchProvenanceError as exc:
        raise ValueError(f"dispatch provenance blocked: {exc}") from exc
    body = "<!-- symphony-workpad:v1 -->\n## Symphony Workpad\n\n"
    comment = github(profile, token, "POST", f"/issues/{issue}/comments", {"body": body})
    comment_id = comment.get("id") if isinstance(comment, dict) else None
    if not isinstance(comment_id, int) or comment_id < 1:
        raise ValueError("GitHub did not return the authoritative workpad comment id")
    record = create_task(ServerAdmission(
        repository=profile.repository,
        project_slug=profile.slug,
        issue_number=issue,
        dispatch_provenance=provenance,
        default_ref=default_ref,
        base_sha=base_sha.lower(),
        workpad_comment_id=comment_id,
        runtime_identity={
            "symphony": lock["symphony"],
            "codex": lock["codex"],
            "containment": lock["containment"],
        },
    ))
    write_task(state_path, record)
    return state_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=pathlib.Path)
    parser.add_argument("--workspace", required=True, type=pathlib.Path)
    parser.add_argument("--runtime-lock", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        print(admit(args.profile, args.workspace, args.runtime_lock))
        return 0
    except Exception as exc:
        print(f"symphony-pilot admission stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
