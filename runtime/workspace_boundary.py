"""Host Git policy and descriptor-safe writes for stopped, task-writable clones.

Only ordinary standalone clones are admitted. Git objects remain evidence, but
local config may not install executable policy. Concurrent hostile execution is
not licensed by this boundary (the Step-8 execution gate remains mandatory).
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import uuid


class WorkspaceBoundaryError(RuntimeError):
    pass


MAX_METADATA_BYTES = 128 * 1024
GIT_EXECUTABLE = shutil.which("git")


def physical_directory(path: pathlib.Path) -> pathlib.Path:
    """Validate without resolve(), which would erase evidence of a symlink."""
    path = pathlib.Path(os.path.abspath(path))
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise WorkspaceBoundaryError("workspace metadata parent is not a physical directory")
    return path


@contextlib.contextmanager
def _parent_descriptor(parent: pathlib.Path):
    parent = physical_directory(parent)
    descriptor = None
    if os.name == "posix":
        # Walk from the root using descriptors: replacing an ancestor cannot
        # redirect the eventual write to another physical directory.
        descriptor = os.open(parent.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parent.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield parent, descriptor
        finally:
            os.close(descriptor)
    else:
        yield parent, None


def atomic_metadata_write(path: pathlib.Path, content: str) -> None:
    """Replace a leaf, never follow it; old predictable .tmp names are unused."""
    raw = content.encode("utf-8")
    if len(raw) > MAX_METADATA_BYTES:
        raise WorkspaceBoundaryError("host metadata exceeds byte bound")
    with _parent_descriptor(path.parent) as (parent, parent_fd):
        name = ".symphony-" + uuid.uuid4().hex + ".tmp"
        temporary = name if parent_fd is not None else parent / name
        destination = path.name if parent_fd is not None else parent / path.name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise WorkspaceBoundaryError("metadata temporary is not regular")
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _read_regular(path: pathlib.Path) -> bytes:
    physical_directory(path.parent)
    if path.is_symlink():
        raise WorkspaceBoundaryError("Git configuration must not be a symlink")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise WorkspaceBoundaryError("Git configuration must be a regular file")
        data = stream.read(MAX_METADATA_BYTES + 1)
    if len(data) > MAX_METADATA_BYTES:
        raise WorkspaceBoundaryError("Git configuration exceeds byte bound")
    return data


def sterile_environment(workspace: pathlib.Path, *, transport: bool = False) -> dict[str, str]:
    # PATH is host-owned; remove relative entries and workspace descendants.
    paths = [entry for entry in os.get_exec_path() if os.path.isabs(entry)
             and not pathlib.Path(entry).is_relative_to(workspace)]
    env = {key: os.environ[key] for key in
           ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE")
           if key in os.environ}
    env.update(PATH=os.pathsep.join(paths), GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", PAGER="cat",
               GIT_NO_REPLACE_OBJECTS="1", GIT_ATTR_NOSYSTEM="1",
               GIT_ALLOW_PROTOCOL="file:https:ssh", GCM_INTERACTIVE="never")
    if transport:
        # Operator-provisioned, explicit transport authority, outside the task.
        config = pathlib.Path.home() / ".config/symphony-pilot/git-transport.config"
        if config.exists():
            if config.is_relative_to(workspace):
                raise WorkspaceBoundaryError("trusted transport config is inside the task")
            _read_regular(config)
            env["GIT_CONFIG_GLOBAL"] = str(config)
        if "SSH_AUTH_SOCK" in os.environ:
            env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
    return env


def _command() -> list[str]:
    if not GIT_EXECUTABLE or not os.path.isabs(GIT_EXECUTABLE):
        raise WorkspaceBoundaryError("host Git executable is unavailable")
    return [GIT_EXECUTABLE, "--no-pager", "--no-replace-objects",
            "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
            "-c", "core.attributesFile=" + os.devnull, "-c", "credential.interactive=false",
            "-c", "protocol.ext.allow=never", "-c", "maintenance.auto=false",
            "-c", "gc.auto=0"]


def validate_repository(workspace: pathlib.Path) -> pathlib.Path:
    workspace = physical_directory(workspace)
    git_dir = physical_directory(workspace / ".git")
    # Reject indirection to external Git metadata and object replacements.
    for root, directories, files in os.walk(git_dir, followlinks=False):
        for name in directories + files:
            path = pathlib.Path(root) / name
            info = path.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                    or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
                raise WorkspaceBoundaryError("Git metadata contains a link or special file")
    for relative in ("commondir", "config.worktree", "objects/info/alternates",
                     "objects/info/http-alternates", "info/grafts", "refs/replace"):
        if (git_dir / relative).exists():
            raise WorkspaceBoundaryError("Git metadata contains unsupported indirection")
    raw = _read_regular(git_dir / "config")
    # Git parses its own syntax as DATA, outside any repository, with includes
    # disabled. No repository command runs until every key/value is admitted.
    with tempfile.TemporaryDirectory(prefix="symphony-git-config-") as directory:
        result = subprocess.run(_command() + ["config", "--null", "--list", "--no-includes", "--file", "-"],
                                input=raw, cwd=directory, env=sterile_environment(workspace),
                                capture_output=True, timeout=15)
    if result.returncode:
        raise WorkspaceBoundaryError("Git configuration is malformed")
    booleans = {"core.filemode", "core.logallrefupdates", "core.ignorecase",
                "core.symlinks", "core.precomposeunicode"}
    seen = set()
    for record in result.stdout.decode("utf-8").split("\0"):
        if not record:
            continue
        key, _, value = record.partition("\n")
        if key in seen:
            raise WorkspaceBoundaryError("duplicate local Git configuration")
        seen.add(key)
        allowed = (
            (key in booleans and value.lower() in {"true", "false"})
            or (key == "core.repositoryformatversion" and value == "0")
            or (key == "core.bare" and value == "false")
            or (key in {"user.name", "user.email"} and bool(value))
            or (key == "remote.origin.url" and bool(value) and not value.startswith("-"))
            or (key == "remote.origin.fetch" and value == "+refs/heads/*:refs/remotes/origin/*")
            or (re.fullmatch(r"branch\.[^\n]+\.remote", key) and value == "origin")
            or (re.fullmatch(r"branch\.[^\n]+\.merge", key) and value.startswith("refs/heads/"))
        )
        if not allowed or any(ord(c) < 32 for c in value):
            raise WorkspaceBoundaryError("local Git configuration is outside the host allowlist")
    return workspace


def run_git(workspace: pathlib.Path, *args: str, transport: bool = False) -> subprocess.CompletedProcess[str]:
    workspace = validate_repository(workspace)
    return subprocess.run(_command() + list(args), cwd=workspace, text=True,
                          capture_output=True, env=sterile_environment(workspace, transport=transport),
                          stdin=subprocess.DEVNULL, timeout=120, check=False)
