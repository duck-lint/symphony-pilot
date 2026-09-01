#!/usr/bin/env python3
"""Host-side publication validation.

This module does not run task-owned Git configuration or trust a requested
remote. It validates an outbox request against host task state before the host
publication clone performs network mutation.
"""
from __future__ import annotations

import os
import pathlib
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from typing import Iterator

from outbox import read_request
from prepare_workspace import publication_key_path


class PublicationError(RuntimeError):
    pass


def safe_git_environment() -> dict[str, str]:
    """Disable ambient Git config and credential helpers for host reads."""
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GIT_") or key == "SSH_AUTH_SOCK":
            env.pop(key, None)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


MAX_BUNDLE_BYTES = 256 * 1024 * 1024


def _git(repository: pathlib.Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), "-c", "core.hooksPath=/dev/null",
         "-c", "credential.helper=", "-c", "include.path=/dev/null", *args],
        env=env, capture_output=True, text=True, check=False,
    )


@contextmanager
def ingest_bundle(path: pathlib.Path) -> Iterator[pathlib.Path]:
    """Copy one opened task artifact to host-owned storage before Git reads it.

    The descriptor is opened without following a final symlink and is the only
    reference to the task-controlled pathname. Git receives a separate file
    under a host-owned temporary directory, so replacement of the task path
    after this function returns cannot change the bytes being validated.
    """
    # O_NONBLOCK is material for the rejected FIFO case: validation must not
    # wait for a hostile writer before fstat can identify the object type.
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    source_fd: int | None = None
    staged: pathlib.Path | None = None
    try:
        try:
            source_fd = os.open(path, flags)
        except OSError as exc:
            raise PublicationError("fixed publication bundle cannot be opened without following a symlink") from exc
        try:
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise PublicationError("fixed publication bundle is not a regular file")
            if before.st_size > MAX_BUNDLE_BYTES:
                raise PublicationError("publication bundle exceeds the host size bound")
            with tempfile.NamedTemporaryFile(prefix="symphony-pilot-bundle-", delete=False) as target:
                staged = pathlib.Path(target.name)
                os.chmod(staged, 0o600)
                remaining = before.st_size
                while remaining:
                    chunk = os.read(source_fd, min(1024 * 1024, remaining))
                    if not chunk:
                        raise PublicationError("publication bundle changed during bounded ingestion")
                    target.write(chunk)
                    remaining -= len(chunk)
                target.flush()
                os.fsync(target.fileno())
            after = os.fstat(source_fd)
            if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) !=
                    (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)):
                raise PublicationError("publication bundle changed during bounded ingestion")
        except PublicationError:
            raise
        except (OSError, ValueError) as exc:
            raise PublicationError("fixed publication bundle ingestion failed") from exc
        finally:
            if source_fd is not None:
                fd, source_fd = source_fd, None
                os.close(fd)
        if staged is None:
            raise PublicationError("fixed publication bundle staging failed")
        yield staged
    finally:
        # This outer cleanup also covers failures after the host temp path is
        # created but before the context manager reaches its yield point.
        if staged is not None:
            staged.unlink(missing_ok=True)


def _publish_staged_bundle(profile, task: dict[str, object], staged_bundle: pathlib.Path, head: str) -> str:
    """Import and publish one exact task commit through a sterile bare repo."""
    key = publication_key_path(profile)
    if key.is_symlink() or not key.is_file() or (key.stat().st_mode & 0o777) != 0o600:
        raise PublicationError("dedicated publication deploy key is unavailable or not mode 0600")
    requested = task["published_head"] or task["base_sha"]
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise PublicationError("publication requested head is invalid")
    env = safe_git_environment()
    env["GIT_SSH_COMMAND"] = "ssh -i " + str(key) + " -o IdentitiesOnly=yes -o BatchMode=yes"
    with tempfile.TemporaryDirectory(prefix="symphony-pilot-publication-") as directory:
        bare = pathlib.Path(directory) / "repo.git"
        initialized = subprocess.run(["git", "init", "--bare", str(bare)], env=env,
                                     capture_output=True, text=True, check=False)
        if initialized.returncode:
            raise PublicationError("sterile publication repository could not be initialized")
        default_fetch = _git(bare, "fetch", profile.git_remote, task["default_ref"], env=env)
        if default_fetch.returncode:
            detail = (default_fetch.stderr or default_fetch.stdout).strip().replace("\n", " ")
            raise PublicationError(f"canonical default branch could not be fetched: {detail[:240]}")
        base = _git(bare, "rev-parse", "FETCH_HEAD", env=env)
        if base.returncode or base.stdout.strip() != task["base_sha"]:
            raise PublicationError("canonical base changed since task admission")
        branch_ref = "refs/heads/" + task["issue_branch"]
        remote_branch = _git(bare, "ls-remote", profile.git_remote, branch_ref, env=env)
        if remote_branch.returncode:
            raise PublicationError("canonical task branch could not be inspected")
        if task["published_head"]:
            branch_fetch = _git(bare, "fetch", profile.git_remote, task["issue_branch"], env=env)
            if branch_fetch.returncode or _git(bare, "rev-parse", "FETCH_HEAD", env=env).stdout.strip() != task["published_head"]:
                raise PublicationError("canonical published task head changed since admission")
        elif remote_branch.stdout.strip():
            raise PublicationError("unexpected pre-existing task branch has no licensed publication head")
        bundle_check = subprocess.run(["git", "-C", str(bare), "bundle", "verify", str(staged_bundle)],
                                      env=env, capture_output=True, text=True, check=False)
        if bundle_check.returncode:
            raise PublicationError("publication bundle verification failed")
        imported = _git(bare, "fetch", str(staged_bundle), "HEAD:refs/quarantine/task", env=env)
        if imported.returncode:
            raise PublicationError("publication bundle import failed")
        fsck = _git(bare, "fsck", "--strict", "--full", env=env)
        if fsck.returncode:
            raise PublicationError("publication object verification failed")
        object_type = _git(bare, "cat-file", "-t", head, env=env)
        if object_type.returncode or object_type.stdout.strip() != "commit":
            detail = (object_type.stderr or object_type.stdout).strip().replace("\n", " ")
            raise PublicationError(f"requested publication object is not exactly a commit: {detail[:240]}")
        ancestor = _git(bare, "merge-base", "--is-ancestor", requested, head, env=env)
        if ancestor.returncode:
            raise PublicationError("publication rewrites or does not advance the licensed task head")
        if task["published_head"]:
            update = _git(bare, "update-ref", branch_ref, head, requested, env=env)
        else:
            update = _git(bare, "update-ref", branch_ref, head, "0" * 40, env=env)
        if update.returncode:
            raise PublicationError("publication branch update is not a fast-forward")
        pushed = _git(bare, "push", profile.git_remote,
                      branch_ref + ":" + branch_ref, env=env)
        if pushed.returncode:
            raise PublicationError("host publication push failed")
    return head


def publish_bundle(profile, task: dict[str, object], bundle_path: pathlib.Path, head: str) -> str:
    """Ingest the task bundle once, then publish only the host-owned copy."""
    with ingest_bundle(bundle_path) as staged_bundle:
        return _publish_staged_bundle(profile, task, staged_bundle, head)


def validate_publication_request(outbox_path: pathlib.Path, task: dict[str, object]) -> dict[str, object]:
    request = read_request(outbox_path, task)
    if request["disposition"] != "ready_for_human_merge":
        raise PublicationError("outbox disposition does not license publication")
    head = request["head"]
    licensed = task["published_head"] or task["base_sha"]
    if head == licensed and task["published_head"] is None:
        raise PublicationError("publication request does not advance the licensed task head")
    return {"repository": task["repository"], "branch": task["issue_branch"], "head": head,
            "request": request}
