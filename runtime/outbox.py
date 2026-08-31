#!/usr/bin/env python3
"""Strict untrusted final-turn result format."""
from __future__ import annotations

import json
import pathlib
import re


OUTBOX_SCHEMA = "symphony-pilot-task-result/v1"
DISPOSITIONS = {"continue", "human_blocked", "infrastructure_blocked", "ready_for_human_merge"}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_WORKPAD_BYTES = 64 * 1024
MAX_SUMMARY_BYTES = 12 * 1024
SECRET_MARKER_RE = re.compile(r"(?:gh[pousr]_|github_pat_|sk-[A-Za-z0-9]|BEGIN [A-Z ]*PRIVATE KEY|Bearer\s+)", re.I)


class OutboxError(ValueError):
    pass


def task_outbox_path(task_record_path: pathlib.Path) -> pathlib.Path:
    """Return the fixed host-owned staging path bound as /symphony-outbox."""
    return task_record_path.parent / "outbox" / "result.json"


def task_bundle_path(task_record_path: pathlib.Path) -> pathlib.Path:
    return task_record_path.parent / "outbox" / "publication.bundle"


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        raise OutboxError(f"outbox {field} is invalid or exceeds its bound")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise OutboxError(f"outbox {field} contains control characters")
    if SECRET_MARKER_RE.search(value):
        raise OutboxError(f"outbox {field} contains a credential marker")
    return value


def validate_request(value: object, task: dict[str, object]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"schema", "task_id", "head", "workpad_body", "disposition", "summary"}:
        raise OutboxError("outbox fields are invalid")
    if value["schema"] != OUTBOX_SCHEMA or value["task_id"] != task["task_id"]:
        raise OutboxError("outbox identity is invalid")
    if value["disposition"] not in DISPOSITIONS:
        raise OutboxError("outbox disposition is not licensed")
    if value["head"] is not None and (not isinstance(value["head"], str) or not SHA_RE.fullmatch(value["head"])):
        raise OutboxError("outbox head is invalid")
    if value["disposition"] == "ready_for_human_merge" and value["head"] is None:
        raise OutboxError("publish requires an exact head")
    _text(value["workpad_body"], "workpad_body", MAX_WORKPAD_BYTES)
    _text(value["summary"], "summary", MAX_SUMMARY_BYTES)
    return value


def read_request(path: pathlib.Path, task: dict[str, object]) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise OutboxError("outbox cannot be read") from exc
    return validate_request(value, task)
