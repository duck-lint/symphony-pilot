"""Host Git policy and descriptor-safe writes for stopped, task-writable clones.

Only ordinary standalone clones are admitted. Git objects remain evidence, but
local config may not install executable policy. Concurrent hostile execution is
not licensed by this boundary (the Step-8 execution gate remains mandatory).
"""
from __future__ import annotations

import contextlib
import errno
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
TASK_IDENTIFIER = re.compile(r"T-[0-9]{6}\Z")
PROJECT_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


def physical_directory(path: pathlib.Path) -> pathlib.Path:
    """Validate without resolve(), which would erase evidence of a symlink."""
    path = pathlib.Path(os.path.abspath(path))
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise WorkspaceBoundaryError("workspace metadata parent is not a physical directory")
    return path


def create_empty_task_workspace(workspace_root: pathlib.Path, identifier: str) -> pathlib.Path:
    """Create the exact unprivileged task namespace before quota mutation.

    The privileged quota helper never creates namespaces.  This host-side
    operation makes the project and task directories under the already
    trusted pool, then requires the task directory to remain empty and owned
    by the unprivileged caller so Git preparation can use it after QUEUED.
    """
    if not TASK_IDENTIFIER.fullmatch(identifier):
        raise WorkspaceBoundaryError("task identifier is malformed")
    project_root = pathlib.Path(os.path.abspath(workspace_root))
    pool_root = physical_directory(project_root.parent)
    try:
        project_root.lstat()
    except FileNotFoundError:
        os.mkdir(project_root, 0o700)
    project_root = physical_directory(project_root)
    try:
        task_root = project_root / identifier
        task_root.lstat()
    except FileNotFoundError:
        os.mkdir(task_root, 0o700)
    task_root = physical_directory(task_root)
    for path in (project_root, task_root):
        info = path.stat()
        if (os.name != "nt" and
                ((hasattr(os, "getuid") and info.st_uid != os.getuid()) or
                 info.st_mode & 0o077)):
            raise WorkspaceBoundaryError("task workspace is not owned by the unprivileged host account")
    if any(task_root.iterdir()):
        raise WorkspaceBoundaryError("new task workspace must be empty")
    # Keep the variable in the contract: the task path must be physically
    # below the pool we validated, not merely textually below a profile path.
    task_root.relative_to(pool_root)
    return task_root


def _workspace_is_process_owned(workspace: pathlib.Path) -> bool:
    """Conservatively reject a task whose cwd belongs to any live process.

    The release route has no authority to identify an arbitrary task process
    by executable name.  A live ``/proc/<pid>/cwd`` below the exact task tree
    is therefore sufficient evidence to retain the reservation.  Missing
    process entries are normal races while scanning /proc; an unavailable
    /proc is not proof of safety and fails closed on Linux.
    """
    if os.name != "posix":
        return False
    proc = pathlib.Path("/proc")
    if not proc.is_dir():
        raise WorkspaceBoundaryError("managed task process ownership cannot be checked")
    workspace_text = os.path.abspath(workspace).rstrip(os.sep)
    try:
        entries = list(proc.iterdir())
    except OSError as exc:
        raise WorkspaceBoundaryError("managed task process ownership cannot be checked") from exc
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cwd = os.readlink(entry / "cwd")
        except (FileNotFoundError, PermissionError, OSError):
            continue
        cwd = cwd.removesuffix(" (deleted)").rstrip(os.sep)
        if cwd == workspace_text or cwd.startswith(workspace_text + os.sep):
            return True
    return False


def _validate_reclamation_entry(path: pathlib.Path, root_device: int) -> os.stat_result:
    """Admit only ordinary files/directories on the task's filesystem."""
    try:
        info = path.lstat()
    except OSError as exc:
        raise WorkspaceBoundaryError("task workspace cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode):
        raise WorkspaceBoundaryError("task workspace contains a symlink")
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise WorkspaceBoundaryError("task workspace contains a special filesystem entry")
    if info.st_dev != root_device:
        raise WorkspaceBoundaryError("task workspace crosses a filesystem boundary")
    if os.path.ismount(path):
        raise WorkspaceBoundaryError("task workspace contains an unexpected mountpoint")
    return info


def _inspect_reclamation_tree(path: pathlib.Path, root_device: int) -> None:
    """Preflight one tree without following links or crossing mounts."""
    info = _validate_reclamation_entry(path, root_device)
    if not stat.S_ISDIR(info.st_mode):
        return
    try:
        entries = list(os.scandir(path))
    except OSError as exc:
        raise WorkspaceBoundaryError("task workspace cannot be enumerated") from exc
    for entry in entries:
        _inspect_reclamation_tree(path / entry.name, root_device)


def _remove_reclamation_tree(path: pathlib.Path) -> None:
    """Remove a preflighted Windows/test fallback tree without link traversal."""
    for entry in list(os.scandir(path)):
        child = path / entry.name
        info = _validate_reclamation_entry(child, os.lstat(path).st_dev)
        if stat.S_ISDIR(info.st_mode):
            _remove_reclamation_tree(child)
        else:
            os.unlink(child)
    os.rmdir(path)


def _inspect_reclamation_fd(fd: int, path: pathlib.Path, root_device: int) -> None:
    """Descriptor-based Linux preflight for the exact task directory."""
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_dev != root_device or os.path.ismount(path):
        raise WorkspaceBoundaryError("task workspace is not a plain directory on the pool")
    try:
        entries = list(os.scandir(fd))
    except OSError as exc:
        raise WorkspaceBoundaryError("task workspace cannot be enumerated") from exc
    for entry in entries:
        child = path / entry.name
        try:
            child_info = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceBoundaryError("task workspace entry cannot be inspected") from exc
        if stat.S_ISLNK(child_info.st_mode):
            raise WorkspaceBoundaryError("task workspace contains a symlink")
        if not (stat.S_ISDIR(child_info.st_mode) or stat.S_ISREG(child_info.st_mode)):
            raise WorkspaceBoundaryError("task workspace contains a special filesystem entry")
        if child_info.st_dev != root_device or os.path.ismount(child):
            raise WorkspaceBoundaryError("task workspace crosses a filesystem boundary")
        if stat.S_ISDIR(child_info.st_mode):
            try:
                child_fd = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except OSError as exc:
                raise WorkspaceBoundaryError("task workspace directory cannot be pinned") from exc
            try:
                _inspect_reclamation_fd(child_fd, child, root_device)
            finally:
                os.close(child_fd)


def _remove_reclamation_fd(fd: int) -> None:
    """Delete children through directory descriptors opened without links."""
    try:
        entries = list(os.scandir(fd))
    except OSError as exc:
        raise WorkspaceBoundaryError("task workspace cannot be enumerated for removal") from exc
    for entry in entries:
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise WorkspaceBoundaryError("task workspace changed to an unsafe entry")
        if stat.S_ISDIR(info.st_mode):
            try:
                child_fd = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except OSError as exc:
                raise WorkspaceBoundaryError("task workspace directory cannot be pinned for removal") from exc
            try:
                _remove_reclamation_fd(child_fd)
            finally:
                os.close(child_fd)
            try:
                os.rmdir(entry.name, dir_fd=fd)
            except OSError as exc:
                raise WorkspaceBoundaryError("task workspace directory could not be removed") from exc
        else:
            try:
                os.unlink(entry.name, dir_fd=fd)
            except OSError as exc:
                raise WorkspaceBoundaryError("task workspace file could not be removed") from exc


def reclaim_task_workspace(
    pool_root: pathlib.Path, project: str, identifier: str,
) -> pathlib.Path:
    """Destroy exactly one host-derived task tree before quota release.

    The project and T-N are supplied by trusted SQLite/profile resolution;
    callers never supply an arbitrary path.  Linux uses descriptor-relative
    operations so a replacement ancestor cannot redirect deletion.  The
    preflight rejects links, special files, mountpoints, and foreign devices
    before any child is removed.
    """
    if not PROJECT_IDENTIFIER.fullmatch(project) or not TASK_IDENTIFIER.fullmatch(identifier):
        raise WorkspaceBoundaryError("reclamation identity is malformed")
    pool = physical_directory(pathlib.Path(pool_root))
    project_root = pool / project
    try:
        project_info = project_root.lstat()
    except FileNotFoundError:
        return project_root / identifier
    except OSError as exc:
        raise WorkspaceBoundaryError("reclamation project root cannot be inspected") from exc
    if (not stat.S_ISDIR(project_info.st_mode) or stat.S_ISLNK(project_info.st_mode) or
            project_info.st_dev != pool.stat().st_dev or os.path.ismount(project_root)):
        raise WorkspaceBoundaryError("reclamation project root is not a plain pool directory")
    task_root = project_root / identifier
    if not os.path.lexists(task_root):
        return task_root
    task_info = _validate_reclamation_entry(task_root, project_info.st_dev)
    if not stat.S_ISDIR(task_info.st_mode):
        raise WorkspaceBoundaryError("task workspace is not a directory")
    if _workspace_is_process_owned(task_root):
        raise WorkspaceBoundaryError("task workspace is owned by a live process")

    if os.name == "posix":
        with _parent_descriptor(project_root) as (_, project_fd):
            try:
                task_fd = os.open(
                    identifier,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=project_fd,
                )
            except OSError as exc:
                raise WorkspaceBoundaryError("task workspace could not be pinned") from exc
            try:
                _inspect_reclamation_fd(task_fd, task_root, project_info.st_dev)
                if _workspace_is_process_owned(task_root):
                    raise WorkspaceBoundaryError("task workspace became owned by a live process")
                _remove_reclamation_fd(task_fd)
            finally:
                os.close(task_fd)
            try:
                os.rmdir(identifier, dir_fd=project_fd)
            except OSError as exc:
                raise WorkspaceBoundaryError("task workspace root could not be removed") from exc
            try:
                os.stat(identifier, dir_fd=project_fd, follow_symlinks=False)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return task_root
                raise WorkspaceBoundaryError("task workspace remained after removal") from exc
    else:
        _inspect_reclamation_tree(task_root, project_info.st_dev)
        if _workspace_is_process_owned(task_root):
            raise WorkspaceBoundaryError("task workspace became owned by a live process")
        _remove_reclamation_tree(task_root)
        if os.path.lexists(task_root):
            raise WorkspaceBoundaryError("task workspace remained after removal")
    return task_root


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
