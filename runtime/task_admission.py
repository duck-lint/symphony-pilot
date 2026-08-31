#!/usr/bin/env python3
"""Host-owned task admission records.

Issue prose and task-local Git metadata are semantic/untrusted input.  This
module accepts only server-derived values supplied by the trusted dispatcher
and stores a strict record outside the execution workspace.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
import tempfile
import uuid


TASK_SCHEMA = "symphony-pilot-task/v1"
TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
BRANCH_PREFIX = "codex/gh-"
TASK_FIELDS = {
    "schema", "repository", "project_slug", "issue_number", "task_id",
    "dispatch_provenance", "default_ref", "base_sha", "issue_branch",
    "workpad_comment_id", "published_head", "draft_pr", "runtime_identity",
    "admitted_utc",
}


class TaskAdmissionError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class ServerAdmission:
    repository: str
    project_slug: str
    issue_number: int
    dispatch_provenance: list[dict[str, object]]
    default_ref: str
    base_sha: str
    workpad_comment_id: int
    runtime_identity: dict[str, object]


def derive_issue_branch(issue_number: int, task_id: str) -> str:
    if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number < 1:
        raise TaskAdmissionError("issue number is invalid")
    if not TASK_ID_RE.fullmatch(task_id):
        raise TaskAdmissionError("task id is invalid")
    return f"{BRANCH_PREFIX}{issue_number}-{task_id[:12]}"


def _validate_sha(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise TaskAdmissionError(f"{field} must be a lowercase commit SHA")
    return value


def _validate_ref(value: object, field: str) -> str:
    if not isinstance(value, str) or not REF_RE.fullmatch(value) or value.startswith("/"):
        raise TaskAdmissionError(f"{field} is invalid")
    if value in {"master", "main", "trunk", "release"} and field == "issue_branch":
        raise TaskAdmissionError("issue branch cannot equal a protected default branch")
    return value


def validate_task_record(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != TASK_FIELDS:
        raise TaskAdmissionError("task record fields are invalid")
    if value["schema"] != TASK_SCHEMA:
        raise TaskAdmissionError("task record schema is not accepted")
    if not isinstance(value["repository"], str) or not re.fullmatch(r"[^/\s]+/[^/\s]+", value["repository"]):
        raise TaskAdmissionError("task repository is invalid")
    if not isinstance(value["project_slug"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value["project_slug"]):
        raise TaskAdmissionError("task project slug is invalid")
    issue = value["issue_number"]
    if not isinstance(issue, int) or isinstance(issue, bool) or issue < 1:
        raise TaskAdmissionError("task issue number is invalid")
    task_id = value["task_id"]
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise TaskAdmissionError("task id is invalid")
    provenance = value["dispatch_provenance"]
    if not isinstance(provenance, list) or not provenance:
        raise TaskAdmissionError("dispatch provenance is missing")
    labels = set()
    for item in provenance:
        if not isinstance(item, dict) or set(item) != {"label", "actor", "event_id", "created_at"}:
            raise TaskAdmissionError("dispatch provenance entry is invalid")
        if (not isinstance(item["label"], str) or not item["label"] or item["label"] in labels or
                not isinstance(item["actor"], str) or not item["actor"] or
                not isinstance(item["event_id"], int) or isinstance(item["event_id"], bool) or item["event_id"] < 1 or
                not isinstance(item["created_at"], str) or not item["created_at"]):
            raise TaskAdmissionError("dispatch provenance entry is invalid")
        labels.add(item["label"])
    default_ref = _validate_ref(value["default_ref"], "default ref")
    base_sha = _validate_sha(value["base_sha"], "base SHA")
    branch = _validate_ref(value["issue_branch"], "issue_branch")
    if branch != derive_issue_branch(issue, task_id):
        raise TaskAdmissionError("issue branch is not host-derived")
    comment_id = value["workpad_comment_id"]
    if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id < 1:
        raise TaskAdmissionError("workpad comment id is invalid")
    published = value["published_head"]
    if published is not None:
        _validate_sha(published, "published head")
    draft = value["draft_pr"]
    if draft is not None and (
        not isinstance(draft, dict) or set(draft) != {"number", "base_ref", "head_ref"}
        or not isinstance(draft["number"], int) or draft["number"] < 1
        or draft["base_ref"] != default_ref or draft["head_ref"] != branch
    ):
        raise TaskAdmissionError("draft PR identity is invalid")
    runtime = value["runtime_identity"]
    if not isinstance(runtime, dict) or set(runtime) != {"symphony", "codex", "containment"}:
        raise TaskAdmissionError("runtime identity is incomplete")
    for name in runtime.values():
        if not isinstance(name, dict) or set(name) != {"executable", "version", "sha256"}:
            raise TaskAdmissionError("runtime identity entry is invalid")
        if not all(isinstance(name[k], str) and name[k] for k in ("executable", "version")):
            raise TaskAdmissionError("runtime identity entry is invalid")
        if not isinstance(name["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", name["sha256"]):
            raise TaskAdmissionError("runtime identity digest is invalid")
    if not isinstance(value["admitted_utc"], str) or not value["admitted_utc"]:
        raise TaskAdmissionError("admission timestamp is invalid")
    return value


def task_state_path(state_root: pathlib.Path, issue_number: int) -> pathlib.Path:
    if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number < 1:
        raise TaskAdmissionError("issue number is invalid")
    return state_root / "tasks" / f"GH-{issue_number}" / "task.json"


def read_task(path: pathlib.Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise TaskAdmissionError("host task record cannot be read") from exc
    return validate_task_record(value)


def write_task(path: pathlib.Path, value: dict[str, object]) -> None:
    validate_task_record(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".task-", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_task(admission: ServerAdmission, *, task_id: str | None = None) -> dict[str, object]:
    task_id = task_id or uuid.uuid4().hex
    branch = derive_issue_branch(admission.issue_number, task_id)
    record: dict[str, object] = {
        "schema": TASK_SCHEMA,
        "repository": admission.repository,
        "project_slug": admission.project_slug,
        "issue_number": admission.issue_number,
        "task_id": task_id,
        "dispatch_provenance": admission.dispatch_provenance,
        "default_ref": admission.default_ref,
        "base_sha": admission.base_sha,
        "issue_branch": branch,
        "workpad_comment_id": admission.workpad_comment_id,
        "published_head": None,
        "draft_pr": None,
        "runtime_identity": admission.runtime_identity,
        "admitted_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    return validate_task_record(record)
