#!/usr/bin/env python3
"""Host broker for one admitted task result.

Only this module maps an untrusted final result to predetermined tracker,
publication, and task-state operations. The result cannot supply identities,
labels, endpoints, or credentials.
"""
from __future__ import annotations

import json
import pathlib
import re
import urllib.parse

from host_integration import _safe_summary
from outbox import read_request, task_bundle_path, task_outbox_path
from prepare_workspace import github, read_secret, require_physical_namespace
from publication import publish_bundle, validate_publication_request
from task_admission import read_task, task_state_path, write_task


class BrokerError(RuntimeError):
    pass


def _issue(task: dict[str, object]) -> int:
    value = task["issue_number"]
    if not isinstance(value, int):
        raise BrokerError("task issue identity is invalid")
    return value


def _safe_body(value: str, head: str | None = None) -> str:
    # Validation is performed by outbox.validate_request; this adds the host
    # fact that the final published identity cannot be supplied by the task.
    if head:
        return value.rstrip() + f"\n\nFinal task HEAD: `{head}`\n"
    return value


def persist_workpad(profile, token: str, task: dict[str, object], body: str, head: str | None = None) -> None:
    comment_id = task["workpad_comment_id"]
    github(profile, token, "PATCH", f"/issues/comments/{comment_id}",
           {"body": _safe_body(body, head)})


def remove_dispatch_labels(profile, token: str, issue: int) -> None:
    for label in profile.dispatch_labels:
        github(profile, token, "DELETE", f"/issues/{issue}/labels/{urllib.parse.quote(label, safe='')}")


def add_blocked_label(profile, token: str, issue: int) -> None:
    github(profile, token, "POST", f"/issues/{issue}/labels", {"labels": [profile.blocked_label]})


def record_blocker(profile, issue: int, kind: str, detail: str) -> None:
    path = require_physical_namespace(profile.state_root) / "blockers" / f"GH-{issue}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": "symphony-pilot-blocker/v1", "issue": issue,
                                "kind": kind, "detail": _safe_summary(detail), "status": "active"},
                               indent=2, sort_keys=True) + "\n", encoding="utf-8")


def draft_pr(profile, token: str, task: dict[str, object], published_head: str | None = None) -> dict[str, object]:
    issue = _issue(task)
    branch = task["issue_branch"]
    default_ref = task["default_ref"]
    candidates = []
    for page in range(1, 101):
        page_items = github(profile, token, "GET", f"/pulls?state=all&per_page=100&page={page}")
        if not isinstance(page_items, list):
            raise BrokerError("GitHub pull-request metadata is unavailable")
        candidates.extend(page_items)
        if len(page_items) < 100:
            break
    else:
        raise BrokerError("GitHub pull-request history exceeded pagination bound")
    matching = []
    for item in candidates:
        if not isinstance(item, dict):
            raise BrokerError("GitHub pull-request metadata is malformed")
        head_data = item.get("head") if isinstance(item.get("head"), dict) else {}
        base_data = item.get("base") if isinstance(item.get("base"), dict) else {}
        head_repo = head_data.get("repo") if isinstance(head_data.get("repo"), dict) else {}
        if (head_data.get("ref") == branch and base_data.get("ref") == default_ref and
                head_repo.get("full_name") == profile.repository):
            matching.append(item)
    if len(matching) > 1:
        raise BrokerError("more than one matching issue draft PR exists")
    if matching:
        item = matching[0]
        if item.get("draft") is not True:
            raise BrokerError("matching issue branch PR is not a draft")
        number = item.get("number")
        if not isinstance(number, int) or number < 1:
            raise BrokerError("matching draft PR identity is invalid")
        body = "Host-created draft for the admitted Symphony task."
        if published_head:
            body += f"\n\nPublished task HEAD: `{published_head}`\n"
        github(profile, token, "PATCH", f"/pulls/{number}", {"base": default_ref, "draft": True, "body": body})
        return {"number": number, "base_ref": default_ref, "head_ref": branch}
    issue_data = github(profile, token, "GET", f"/issues/{issue}")
    title = issue_data.get("title") if isinstance(issue_data, dict) else None
    if not isinstance(title, str) or not title:
        title = f"Issue #{issue}"
    created = github(profile, token, "POST", "/pulls", {
        "title": title,
        "head": branch,
        "base": default_ref,
        "body": "Host-created draft for the admitted Symphony task.",
        "draft": True,
    })
    if not isinstance(created, dict) or not isinstance(created.get("number"), int):
        raise BrokerError("GitHub did not return a draft PR identity")
    number = created["number"]
    if published_head:
        github(profile, token, "PATCH", f"/pulls/{number}", {
            "base": default_ref, "draft": True,
            "body": f"Host-created draft for the admitted Symphony task.\n\nPublished task HEAD: `{published_head}`\n",
        })
    return {"number": number, "base_ref": default_ref, "head_ref": branch}


def process_result(profile, workspace: pathlib.Path) -> int:
    workspace = workspace.resolve()
    match = re.fullmatch(r"GH-(\d+)", workspace.name)
    if not match:
        raise BrokerError("workspace identity must be GH-N")
    issue = int(match.group(1))
    task_path = task_state_path(require_physical_namespace(profile.state_root), issue)
    task = read_task(task_path)
    outbox = task_outbox_path(task_path)
    request = read_request(outbox, task)
    token = read_secret(profile)
    disposition = request["disposition"]
    persist_workpad(profile, token, task, request["workpad_body"])
    if disposition == "continue":
        return 0
    if disposition == "human_blocked":
        remove_dispatch_labels(profile, token, issue)
        add_blocked_label(profile, token, issue)
        return 0
    if disposition == "infrastructure_blocked":
        remove_dispatch_labels(profile, token, issue)
        record_blocker(profile, issue, "task_infrastructure", request["summary"])
        return 0
    if disposition != "ready_for_human_merge":
        raise BrokerError("unrecognized task disposition")
    try:
        validated = validate_publication_request(outbox, task)
        published = validated["head"] if task["published_head"] == validated["head"] else publish_bundle(
            profile, task, task_bundle_path(task_path), validated["head"])
        pr = draft_pr(profile, token, task, published_head=published)
    except Exception as exc:
        remove_dispatch_labels(profile, token, issue)
        add_blocked_label(profile, token, issue)
        record_blocker(profile, issue, "task_infrastructure", str(exc))
        return 0
    updated = dict(task, published_head=published, draft_pr=pr)
    write_task(task_path, updated)
    persist_workpad(profile, token, updated, request["workpad_body"], published)
    remove_dispatch_labels(profile, token, issue)
    return 0
