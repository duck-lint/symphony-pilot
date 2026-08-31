#!/usr/bin/env python3
"""Host-side publication validation.

This module does not run task-owned Git configuration or trust a requested
remote. It validates an outbox request against host task state before a future
host publication clone is allowed to perform network mutation.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

from outbox import read_request


class PublicationError(RuntimeError):
    pass


def safe_git_environment() -> dict[str, str]:
    """Disable ambient Git config and credential helpers for host reads."""
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GIT_CONFIG") or key in {"GIT_SSH", "GIT_SSH_COMMAND", "SSH_AUTH_SOCK"}:
            env.pop(key, None)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def task_head(workspace: pathlib.Path, requested: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", requested):
        raise PublicationError("publication head is not a lowercase commit SHA")
    result = subprocess.run(
        ["git", "-C", str(workspace), "-c", "core.hooksPath=/dev/null",
         "-c", "credential.helper=", "-c", "include.path=", "rev-parse", "--verify", requested],
        env=safe_git_environment(), capture_output=True, text=True, check=False,
    )
    if result.returncode or result.stdout.strip() != requested:
        raise PublicationError("requested task head is not present exactly in the task object store")
    return requested


def validate_publication_request(outbox_path: pathlib.Path, workspace: pathlib.Path,
                                task: dict[str, object]) -> dict[str, object]:
    request = read_request(outbox_path, task)
    if request["action"] != "publish":
        raise PublicationError("outbox request does not license publication")
    head = task_head(workspace, request["head"])
    licensed = task["published_head"] or task["base_sha"]
    if head == licensed:
        raise PublicationError("publication request does not advance the licensed task head")
    return {"repository": task["repository"], "branch": task["issue_branch"], "head": head}
