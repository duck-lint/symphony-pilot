#!/usr/bin/env python3
"""Trusted, bounded Git evidence for one Pilot task.

The receipt reader is shared by the loopback API and lifecycle reconciliation.
It accepts only a registered profile plus a SQLite task row; the browser and
agent never provide paths, refs, repositories, or commands.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import subprocess
from typing import Any

from workspace_boundary import WorkspaceBoundaryError, physical_directory, run_git, validate_repository


RECEIPT_VERSION = 1
MAX_DIFF_CHARS = 256 * 1024
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.I | re.S),
    re.compile(r"(?:github_pat|gh[pousr]|sk)-?[A-Za-z0-9_\-]{12,}", re.I),
    re.compile(r"\bBearer\s+[^\s]+", re.I),
)


class ReceiptError(RuntimeError):
    """Base class for evidence that cannot be trusted or captured."""


class WorkspaceMissing(ReceiptError):
    """The trusted task workspace is absent; SQLite evidence may remain."""

    def __init__(self, path: pathlib.Path):
        super().__init__("workspace not currently present")
        self.path = path


class ReceiptBoundaryError(ReceiptError):
    """The derived workspace violates the physical host boundary."""


def redact(value: object) -> object:
    """Redact credential-shaped text before returning or persisting it."""
    if isinstance(value, str):
        for pattern in SECRET_PATTERNS:
            value = pattern.sub("[REDACTED CREDENTIAL]", value)
        return value
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def task_workspace(profile, task: dict[str, object]) -> tuple[pathlib.Path, bool]:
    """Resolve only ``profile.workspace_root / task.identifier`` physically."""
    identifier = str(task["identifier"])
    if not re.fullmatch(r"T-[0-9]{6}", identifier):
        raise ReceiptBoundaryError("task identifier is not a valid workspace identity")
    root = pathlib.Path(profile.workspace_root)
    workspace = root / identifier
    try:
        physical_root = physical_directory(root)
    except FileNotFoundError:
        try:
            physical_directory(root.parent)
        except FileNotFoundError:
            return workspace, False
        return workspace, False
    except WorkspaceBoundaryError as exc:
        raise ReceiptBoundaryError(str(exc)) from exc
    try:
        physical_workspace = physical_directory(workspace)
    except FileNotFoundError:
        return workspace, False
    except WorkspaceBoundaryError as exc:
        raise ReceiptBoundaryError(str(exc)) from exc
    try:
        physical_workspace.relative_to(physical_root)
    except ValueError as exc:
        raise ReceiptBoundaryError("task workspace escapes the project workspace root") from exc
    return physical_workspace, True


def _git(workspace: pathlib.Path, *args: str) -> str:
    try:
        result = run_git(workspace, *args)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReceiptError(str(exc)) from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        raise ReceiptError(f"git {' '.join(args[:3])}: {str(redact(detail))[:300]}")
    return result.stdout.strip()


def _commits(workspace: pathlib.Path, base_sha: str, current_head: str) -> list[dict[str, object]]:
    shas = _git(workspace, "rev-list", "--reverse", f"{base_sha}..{current_head}").splitlines()
    commits: list[dict[str, object]] = []
    for sha in shas:
        fields = _git(
            workspace, "show", "-s",
            "--format=%H%x1f%s%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%aI%x1f%cI",
            sha,
        ).split("\x1f")
        if len(fields) != 8:
            raise ReceiptError("Git commit metadata is malformed")
        commits.append({
            "sha": fields[0], "subject": fields[1],
            "author_name": fields[2], "author_email": fields[3],
            "committer_name": fields[4], "committer_email": fields[5],
            "authored_at": fields[6], "committed_at": fields[7],
        })
    return commits


def _change_summary(workspace: pathlib.Path, base_sha: str, current_head: str) -> dict[str, object]:
    statuses = _git(
        workspace, "diff", "--name-status", "--find-renames", "--find-copies",
        f"{base_sha}..{current_head}",
    ).splitlines()
    counts: dict[str, tuple[int | None, int | None]] = {}
    for line in _git(workspace, "diff", "--numstat", f"{base_sha}..{current_head}").splitlines():
        fields = line.split("\t", 2)
        if len(fields) == 3:
            added = None if fields[0] == "-" else int(fields[0])
            deleted = None if fields[1] == "-" else int(fields[1])
            counts[fields[2]] = (added, deleted)
    files: list[dict[str, object]] = []
    insertions = deletions = 0
    for line in statuses:
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        status, path = fields[0], fields[-1]
        added, deleted = counts.get(path, (None, None))
        if added is not None:
            insertions += added
        if deleted is not None:
            deletions += deleted
        files.append({"path": path, "status": status, "insertions": added, "deletions": deleted})
    return {
        "files_changed": len(files), "insertions": insertions,
        "deletions": deletions, "files": files,
    }


def capture_live_receipt(profile, task: dict[str, object]) -> dict[str, object]:
    """Capture trusted evidence using the SQLite-authorized task range."""
    workspace, exists = task_workspace(profile, task)
    if not exists:
        raise WorkspaceMissing(workspace)
    try:
        validate_repository(workspace)
        remote = _git(workspace, "remote", "get-url", "origin")
        if remote != profile.git_remote:
            raise ReceiptError("task workspace remote differs from the registered repository")
        branch = _git(workspace, "branch", "--show-current")
        expected_branch = str(task["branch"])
        if branch != expected_branch:
            raise ReceiptError("task workspace is not on the SQLite-recorded task branch")
        head = _git(workspace, "rev-parse", "HEAD")
        current_head = task["current_head"]
        if current_head is None or str(current_head) != head:
            raise ReceiptError("workspace HEAD does not match the SQLite-recorded current head")
        clean = not _git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
        if not clean:
            raise ReceiptError("task workspace is not clean")
        base_sha = str(task["base_sha"])
        commits = _commits(workspace, base_sha, str(current_head))
        patch = _git(workspace, "diff", "--unified=3", f"{base_sha}..{current_head}")
        if base_sha == str(current_head):
            patch = "No committed task changes."
        receipt: dict[str, object] = {
            "receipt_version": RECEIPT_VERSION,
            "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source": "live",
            "workspace_path": str(workspace),
            "branch": expected_branch,
            "base_sha": base_sha,
            "current_head": current_head,
            "published_head": task["published_head"],
            "workspace": {"wsl_path": str(workspace), "exists": True, "clean": True},
            "git": {
                "branch": branch, "expected_branch": expected_branch, "head": head,
                "base_sha": base_sha, "current_head": current_head,
                "published_head": task["published_head"],
            },
            "commit": commits[-1] if commits else None,
            "commits": commits,
            "change_summary": _change_summary(workspace, base_sha, str(current_head)),
            "diff": {
                "unified_patch": patch[:MAX_DIFF_CHARS],
                "truncated": len(patch) > MAX_DIFF_CHARS,
            },
        }
        return redact(receipt)  # type: ignore[return-value]
    except ReceiptError:
        raise
    except (WorkspaceBoundaryError, OSError, subprocess.SubprocessError) as exc:
        raise ReceiptError(str(exc)) from exc


def missing_receipt(profile, task: dict[str, object]) -> dict[str, object]:
    workspace, exists = task_workspace(profile, task)
    if exists:
        raise ReceiptError("workspace evidence is unavailable")
    base_sha = str(task["base_sha"])
    return {
        "receipt_version": RECEIPT_VERSION,
        "source": "none",
        "workspace_path": str(workspace),
        "branch": str(task["branch"]),
        "base_sha": base_sha,
        "current_head": task["current_head"],
        "published_head": task["published_head"],
        "workspace": {"wsl_path": str(workspace), "exists": False, "clean": None},
        "git": {
            "branch": str(task["branch"]), "expected_branch": str(task["branch"]),
            "head": None, "base_sha": base_sha, "current_head": task["current_head"],
            "published_head": task["published_head"],
        },
        "commit": None, "commits": [],
        "change_summary": {"files_changed": None, "insertions": None, "deletions": None, "files": []},
        "diff": {"unified_patch": "Workspace not currently present.", "truncated": False},
    }


def persisted_receipt(events: list[dict[str, object]]) -> dict[str, object] | None:
    """Read the immutable snapshot nested in a trusted lifecycle event."""
    for event in reversed(events):
        if event.get("event_type") != "validation_passed":
            continue
        try:
            payload = json.loads(str(event["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        value = payload.get("execution_receipt") if isinstance(payload, dict) else None
        if isinstance(value, dict) and value.get("receipt_version") == RECEIPT_VERSION:
            saved = dict(value)
            saved["source"] = "persisted"
            workspace = dict(saved.get("workspace") or {})
            workspace["exists"] = False
            workspace["clean"] = None
            saved["workspace"] = workspace
            return redact(saved)  # type: ignore[return-value]
    return None


def verify_persisted_receipt(database, task_id: str, event_id: str, receipt: dict[str, object]) -> None:
    """Verify the just-written immutable event before reconciliation commits."""
    row = database.connection.execute(
        "SELECT payload_json FROM task_events WHERE id = ? AND task_id = ?",
        (event_id, task_id),
    ).fetchone()
    if row is None:
        raise ReceiptError("execution receipt event was not persisted")
    try:
        payload: Any = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReceiptError("persisted execution receipt payload is malformed") from exc
    if not isinstance(payload, dict) or payload.get("execution_receipt") != receipt:
        raise ReceiptError("persisted execution receipt did not verify")
