#!/usr/bin/env python3
"""Strict untrusted task-to-host request format."""
from __future__ import annotations

import json
import pathlib
import re


OUTBOX_SCHEMA = "symphony-pilot-task-outbox/v1"
ALLOWED_ACTIONS = {"publish", "complete", "block"}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class OutboxError(ValueError):
    pass


def task_outbox_path(task_record_path: pathlib.Path) -> pathlib.Path:
    """Return the host-owned staging path bound as /symphony-outbox."""
    return task_record_path.parent / "outbox" / "result.json"


def validate_request(value: object, task: dict[str, object]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"schema", "task_id", "action", "head", "summary"}:
        raise OutboxError("outbox fields are invalid")
    if value["schema"] != OUTBOX_SCHEMA or value["task_id"] != task["task_id"]:
        raise OutboxError("outbox identity is invalid")
    if value["action"] not in ALLOWED_ACTIONS:
        raise OutboxError("outbox action is not licensed")
    if value["head"] is not None and (not isinstance(value["head"], str) or not SHA_RE.fullmatch(value["head"])):
        raise OutboxError("outbox head is invalid")
    if value["action"] == "publish" and value["head"] is None:
        raise OutboxError("publish requires an exact head")
    if not isinstance(value["summary"], str) or len(value["summary"]) > 12000:
        raise OutboxError("outbox summary is invalid")
    return value


def read_request(path: pathlib.Path, task: dict[str, object]) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise OutboxError("outbox cannot be read") from exc
    return validate_request(value, task)
