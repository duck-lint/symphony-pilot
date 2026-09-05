#!/usr/bin/env python3
"""Run one approved host command inside Pilot's Linux containment domain.

The Windows adapter supplies only the fixed project name and Linux argv.  This
trusted Linux-side supervisor resolves the project again, creates a dedicated
build/cache area, and reuses ``runtime.containment`` for the actual namespace,
mount, resource, and descendant-teardown boundary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile


DEPLOYMENT_SCHEMA = "symphony-pilot-deployment/v2"
DEPLOYMENT_NAMESPACE = pathlib.PurePosixPath(
    "/home/duck-lint/.local/share/symphony-pilot/deployments"
)
REQUIRED_AUTHORITY_FILES = (
    "runtime/wsl_contained_exec.py",
    "runtime/containment.py",
)
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
SAFE_PROFILE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
WORKSPACE_ROOT = pathlib.PurePosixPath("/home/duck-lint/symphony-workspaces")
QUOTA_INSPECTION_SCHEMA = "symphony-pilot-quota-inspection/v1"
QUOTA_ADMISSION_SCHEMA = "symphony-pilot-task-quota-admission/v1"
QUOTA_PROBE_NAME = ".symphony-quota-inspection-probe"
TASK_IDENTIFIER = re.compile(r"T-[0-9]{6}\Z")
# This is a capability-specific, operator-installed executable.  It is not a
# command broker: it accepts one host-derived task identity and fixed policy
# limits, binds the workspace to that project quota, and returns bounded
# kernel evidence.  Missing or unsafe installation fails closed.
QUOTA_HELPER_PATH = pathlib.PurePosixPath(
    "/usr/libexec/symphony-pilot/quota-admit-task"
)
QUOTA_HELPER_IDENTITY_PATH = pathlib.PurePosixPath(
    "/etc/symphony-pilot/quota-admit-task.identity.json"
)
QUOTA_HELPER_GROUP = "symphony-pilot"
QUOTA_HELPER_SOURCE_SHA256 = (
    "f212db1e8bcedf1246ba760643c6c56dca0f29e4d5dff5fead2fe21be66f498f"
)


class ContainmentError(RuntimeError):
    """A fail-closed supervisor validation error before containment imports."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(root: pathlib.Path, relative: str) -> pathlib.Path:
    candidate = pathlib.PurePosixPath(relative)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ContainmentError("deployment_manifest", "deployment manifest contains an unsafe file path")
    path = root.joinpath(*candidate.parts)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ContainmentError("deployment_manifest", "deployment manifest path leaves its root") from exc
    return path


def _validate_deployment(root: pathlib.Path) -> dict[str, object]:
    """Verify the deployed supervisor and its manifest before importing policy code."""
    if root.is_symlink() or not root.is_dir():
        raise ContainmentError("deployment_missing", "trusted Pilot deployment root is unavailable")
    manifest_path = root / "DEPLOYMENT.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ContainmentError("deployment_manifest", "trusted Pilot deployment manifest is unavailable")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContainmentError("deployment_manifest", "trusted Pilot deployment manifest is malformed") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != DEPLOYMENT_SCHEMA:
        raise ContainmentError("deployment_manifest", "trusted Pilot deployment schema is unsupported")
    profile = manifest.get("profile")
    source_commit = manifest.get("source_commit")
    files = manifest.get("files")
    if (not isinstance(profile, str) or not SAFE_PROFILE.fullmatch(profile) or
            not isinstance(source_commit, str) or not COMMIT_SHA.fullmatch(source_commit) or
            not isinstance(files, dict) or not files):
        raise ContainmentError("deployment_manifest", "trusted Pilot deployment identity is malformed")
    if profile != root.name:
        raise ContainmentError("deployment_identity", "trusted Pilot deployment profile does not match its root")
    for field in ("profile_sha256", "operator_contract_sha256", "deployment_identity"):
        if not isinstance(manifest.get(field), str) or not SHA256.fullmatch(manifest[field]):
            raise ContainmentError("deployment_manifest", f"trusted Pilot {field} is malformed")
    normalized_files: dict[str, str] = {}
    for relative, expected in files.items():
        if not isinstance(relative, str) or not isinstance(expected, str) or not SHA256.fullmatch(expected):
            raise ContainmentError("deployment_manifest", "trusted Pilot file inventory is malformed")
        path = _manifest_path(root, relative)
        if path.is_symlink() or not path.is_file():
            raise ContainmentError("deployment_manifest", f"trusted Pilot file is unavailable: {relative}")
        if _sha256(path) != expected:
            raise ContainmentError("deployment_identity", f"trusted Pilot file does not match its manifest: {relative}")
        normalized_files[relative] = expected
    for required in REQUIRED_AUTHORITY_FILES:
        if required not in normalized_files:
            raise ContainmentError("deployment_identity", f"trusted Pilot authority file is not deployed: {required}")
    for directory, subdirectories, names in os.walk(root, followlinks=False):
        if any(pathlib.Path(directory, name).is_symlink() for name in subdirectories + names):
            raise ContainmentError("deployment_identity", "trusted Pilot deployment contains a symlink")
        for name in names:
            path = pathlib.Path(directory) / name
            if path == manifest_path:
                continue
            relative = path.relative_to(root).as_posix()
            if relative not in normalized_files:
                raise ContainmentError("deployment_identity", f"unlisted file in trusted Pilot deployment: {relative}")
    identity_payload = {
        "files": normalized_files,
        "operator_contract_sha256": manifest["operator_contract_sha256"],
        "profile": profile,
        "profile_sha256": manifest["profile_sha256"],
    }
    encoded = json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != manifest["deployment_identity"]:
        raise ContainmentError("deployment_identity", "trusted Pilot deployment identity does not match its manifest")
    return manifest


def _deployment_root() -> pathlib.Path:
    script = pathlib.Path(__file__)
    if script.is_symlink():
        raise ContainmentError("deployment_identity", "trusted Pilot supervisor must not be a symlink")
    runtime_root = script.parent
    root = runtime_root.parent
    if runtime_root.name != "runtime" or root.parent != pathlib.Path(DEPLOYMENT_NAMESPACE):
        raise ContainmentError("deployment_identity", "trusted Pilot supervisor is outside its deployment namespace")
    return root


def _load_containment(root: pathlib.Path):
    """Import containment only after the deployed manifest has been verified."""
    # Keep the validated deployment immutable for the entire supervisor
    # lifetime.  The adapter also supplies ``python3 -B``; this local guard
    # protects the same authority boundary if the fixed entrypoint is invoked
    # by another reviewed Linux-side caller.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root / "runtime"))
    import containment
    return containment


PROJECT_ROOTS = {
    "symphony-pilot": pathlib.Path("/mnt/f/PROJECT-REPOS/symphony-pilot"),
    "symphony-runtime": pathlib.Path("/mnt/f/PROJECT-REPOS/symphony-runtime"),
}
STORAGE_PROJECTS = frozenset({"cleanroom", "symphony-pilot", "symphony-runtime", "symphony-canary"})


def _project_root(project: str) -> pathlib.Path:
    if project not in PROJECT_ROOTS:
        raise ContainmentError("project", "project is not admitted to the WSL containment domain")
    root = PROJECT_ROOTS[project]
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ContainmentError("project", "approved project root cannot be resolved") from exc
    if resolved != root:
        raise ContainmentError("project", "approved project root resolves through an unexpected alias")
    if os.path.ismount(resolved):
        raise ContainmentError("project", "approved project root is itself a mountpoint")
    return resolved


def _plain_directory(path: pathlib.Path, kind: str) -> pathlib.Path:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ContainmentError(kind, "persistent workspace storage path cannot be inspected") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ContainmentError(kind, "persistent workspace storage path must be a real directory")
    return path


def _ensure_plain_directory(path: pathlib.Path, kind: str) -> tuple[pathlib.Path, bool]:
    created = False
    try:
        path.lstat()
    except FileNotFoundError:
        _plain_directory(path.parent, kind)
        try:
            os.mkdir(path, 0o700)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise ContainmentError(kind, "persistent workspace storage directory cannot be created") from exc
    return _plain_directory(path, kind), created


def _workspace_storage_root(project: str) -> tuple[pathlib.Path, bool]:
    if project not in STORAGE_PROJECTS:
        raise ContainmentError("project", "project is not admitted to the WSL quota domain")
    namespace = pathlib.Path(WORKSPACE_ROOT)
    namespace, created = _ensure_plain_directory(namespace, "quota_workspace")
    # The filesystem boundary is the one shared Symphony pool. Project and
    # task directories are children inside it and are not quota domains.
    return namespace, created


def _quota_inspection(project: str) -> dict[str, object]:
    """Return bounded evidence for the persistent task-storage filesystem.

    This is deliberately inspection-only.  It never selects a quota backend,
    assigns project IDs, creates lifecycle state, or launches a caller-supplied
    command.  When the storage root is absent, the root itself is created as a
    host-owned namespace and an inert probe directory supplies the pathname
    required by ``findmnt``; no task-shaped directory is created.
    """
    storage_root, root_created = _workspace_storage_root(project)
    probe = storage_root / QUOTA_PROBE_NAME
    probe_created = False
    target = storage_root
    if root_created:
        try:
            os.mkdir(probe, 0o700)
            probe_created = True
        except OSError as exc:
            raise ContainmentError("quota_workspace", "inert quota inspection probe cannot be created") from exc
        target = probe
    try:
        try:
            result = subprocess.run(
                ["/bin/findmnt", "--json", "--target", str(target),
                 "--output", "TARGET,SOURCE,FSTYPE,OPTIONS"],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContainmentError("quota_inspection", "trusted mount inspection did not complete") from exc
        if result.returncode:
            raise ContainmentError("quota_inspection", "trusted mount inspection failed")
        try:
            payload = json.loads(result.stdout)
            entries = payload["filesystems"]
            entry = entries[0]
            mount_target = entry["target"]
            source = entry["source"]
            fstype = entry["fstype"]
            options = entry["options"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ContainmentError("quota_inspection", "trusted mount inspection returned malformed evidence") from exc
        if (not isinstance(mount_target, str) or not isinstance(source, str) or
                not isinstance(fstype, str) or not isinstance(options, str)):
            raise ContainmentError("quota_inspection", "trusted mount inspection fields are malformed")
        try:
            usage = os.statvfs(target)
        except OSError as exc:
            raise ContainmentError("quota_inspection", "persistent task filesystem statistics are unavailable") from exc
        try:
            ownership = os.stat(target, follow_symlinks=False)
        except OSError:
            # Some unit fixtures provide only statvfs evidence. A missing
            # ownership proof is represented as untrusted and is rejected by
            # the host verifier for real admission.
            ownership = None
        option_set = frozenset(options.split(","))
        project_quota = fstype in {"ext4", "xfs"} and bool(option_set & {"prjquota", "pquota"})
        trusted_ownership = (
            ownership is not None and
            ownership.st_uid == getattr(os, "getuid", lambda: -1)() and
            not (ownership.st_mode & 0o022)
        )
        return {
            "schema": QUOTA_INSPECTION_SCHEMA,
            "project": project,
        "scope": "persistent_symphony_workspace_pool",
            "probe_created": probe_created,
            "filesystem": {
                "target": mount_target,
                "source": source,
                "fstype": fstype,
                "options": options,
                "project_quota_mount": project_quota,
                "statvfs": {
                    "block_size": usage.f_frsize or usage.f_bsize,
                    "blocks": usage.f_blocks,
                    "free_blocks": usage.f_bfree,
                    "available_blocks": usage.f_bavail,
                    "inodes": usage.f_files,
                    "free_inodes": usage.f_ffree,
                    "available_inodes": usage.f_favail,
                },
            },
            "ownership": {
                "uid": ownership.st_uid if ownership is not None else None,
                "gid": ownership.st_gid if ownership is not None else None,
                "mode": ownership.st_mode & 0o777 if ownership is not None else None,
                "trusted": trusted_ownership,
            },
            # Mount flags alone do not prove that a task quota identity is
            # applicable or that either hard limit is enforced. Those facts
            # require the separate trusted provisioning/verifier proof.
            "quota": {
                "backend": "ext4-project-quota" if project_quota else None,
                "mount_support": project_quota,
            },
        }
    finally:
        if probe_created:
            try:
                os.rmdir(probe)
            except OSError as exc:
                raise ContainmentError("quota_workspace", "inert quota inspection probe could not be removed") from exc


def _quota_helper_fd() -> int:
    """Open and pin the reviewed helper without following a replacement link."""
    for parent in (
        QUOTA_HELPER_PATH.parent, QUOTA_HELPER_PATH.parent.parent,
        pathlib.PurePosixPath("/etc/symphony-pilot"), pathlib.PurePosixPath("/etc"),
    ):
        try:
            metadata = os.stat(parent, follow_symlinks=False)
        except OSError as exc:
            raise ContainmentError("quota_provisioning", "task quota helper parent is unavailable") from exc
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or
                metadata.st_mode & 0o022):
            raise ContainmentError("quota_provisioning", "task quota helper parent is not trusted")
    try:
        fd = os.open(
            str(QUOTA_HELPER_PATH),
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise ContainmentError("quota_provisioning", "task quota helper is unavailable") from exc
    try:
        metadata = os.fstat(fd)
        try:
            import grp
            expected_gid = grp.getgrnam(QUOTA_HELPER_GROUP).gr_gid
        except (ImportError, KeyError) as exc:
            raise ContainmentError("quota_provisioning", "task quota helper group is unavailable") from exc
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or
                metadata.st_gid != expected_gid or metadata.st_mode & 0o022 or
                not metadata.st_mode & 0o111 or not metadata.st_mode & stat.S_ISUID):
            raise ContainmentError("quota_provisioning", "task quota helper is not a trusted executable")
        try:
            identity_fd = os.open(
                str(QUOTA_HELPER_IDENTITY_PATH),
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                identity_metadata = os.fstat(identity_fd)
                if (not stat.S_ISREG(identity_metadata.st_mode) or
                        identity_metadata.st_uid != 0 or identity_metadata.st_mode & 0o022):
                    raise OSError("helper identity sidecar is not trusted")
                identity_payload = os.read(identity_fd, 4096)
            finally:
                os.close(identity_fd)
            identity_document = json.loads(identity_payload.decode("ascii"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ContainmentError("quota_provisioning", "task quota helper identity is unavailable") from exc
        if (not isinstance(identity_document, dict) or
                set(identity_document) != {"schema", "source_sha256", "helper_sha256", "group", "privilege"} or
                identity_document.get("schema") != "symphony-pilot-quota-helper/v1" or
                identity_document.get("source_sha256") != QUOTA_HELPER_SOURCE_SHA256 or
                identity_document.get("group") != QUOTA_HELPER_GROUP or
                identity_document.get("privilege") != "setuid-root" or
                not isinstance(identity_document.get("helper_sha256"), str) or
                not SHA256.fullmatch(identity_document["helper_sha256"])):
            raise ContainmentError("quota_provisioning", "task quota helper identity is malformed")
        digest = hashlib.sha256()
        os.lseek(fd, 0, os.SEEK_SET)
        for chunk in iter(lambda: os.read(fd, 1024 * 1024), b""):
            digest.update(chunk)
        if digest.hexdigest() != identity_document["helper_sha256"]:
            raise ContainmentError("quota_provisioning", "task quota helper identity differs from reviewed artifact")
        return fd
    except Exception:
        os.close(fd)
        raise


def _validate_task_quota_result(
    result: object, *, project: str, identifier: str,
    byte_limit: int, inode_limit: int,
) -> dict[str, object]:
    """Validate only the fixed helper's task-proof wire shape."""
    if not isinstance(result, dict) or result.get("schema") != "symphony-pilot-task-quota-proof/v1":
        raise ContainmentError("quota_provisioning", "task quota helper returned unsupported evidence")
    required = {
        "schema", "identifier", "workspace_path", "project_id", "workspace_project_id",
        "workspace_project_inherit", "inheritance_probe", "byte_hard_limit",
        "inode_hard_limit", "usage", "byte_probe", "inode_probe",
    }
    if set(result) != required:
        raise ContainmentError("quota_provisioning", "task quota helper returned incomplete evidence")
    expected_id = 1_000_000 + int(identifier[2:])
    expected_path = f"/home/duck-lint/symphony-workspaces/{project}/{identifier}"
    if (result["identifier"] != identifier or result["workspace_path"] != expected_path or
            result["project_id"] != expected_id or result["workspace_project_id"] != expected_id or
            result["workspace_project_inherit"] is not True or
            not isinstance(result["inheritance_probe"], dict) or
            result["inheritance_probe"].get("attempted") is not True or
            result["inheritance_probe"].get("result") != "project-id" or
            result["byte_hard_limit"] != byte_limit or result["inode_hard_limit"] != inode_limit):
        raise ContainmentError("quota_provisioning", "task quota helper returned mismatched identity or limits")
    usage = result["usage"]
    if (not isinstance(usage, dict) or set(usage) != {"bytes", "inodes"} or
            any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in usage.values())):
        raise ContainmentError("quota_provisioning", "task quota usage evidence is malformed")
    for field in ("byte_probe", "inode_probe"):
        probe = result[field]
        if (not isinstance(probe, dict) or set(probe) != {"attempted", "result"} or
                probe["attempted"] is not True or probe["result"] != "EDQUOT"):
            raise ContainmentError("quota_provisioning", "task quota hard-limit probe is not proven")
    return result


def _quota_task_admission(
    project: str, identifier: str, byte_limit: int, inode_limit: int,
) -> dict[str, object]:
    """Bind one task through the fixed privileged quota capability."""
    if not TASK_IDENTIFIER.fullmatch(identifier):
        raise ContainmentError("quota_identity", "task identifier is malformed")
    if (isinstance(byte_limit, bool) or not isinstance(byte_limit, int) or byte_limit <= 0 or
            isinstance(inode_limit, bool) or not isinstance(inode_limit, int) or inode_limit <= 0):
        raise ContainmentError("quota_policy", "task quota limits are malformed")
    pool = _quota_inspection(project)
    fd = _quota_helper_fd()
    try:
        try:
            result = subprocess.run(
                [f"/proc/self/fd/{fd}", "--operation", "admit", "--project", project, "--identifier", identifier,
                 "--byte-limit", str(byte_limit), "--inode-limit", str(inode_limit)],
                capture_output=True, text=True, timeout=10, check=False,
                pass_fds=(fd,), executable=f"/proc/self/fd/{fd}",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContainmentError("quota_provisioning", "task quota helper did not complete") from exc
    finally:
        os.close(fd)
    if result.returncode or len(result.stdout.encode("utf-8")) > 1024 * 1024:
        raise ContainmentError("quota_provisioning", "task quota helper failed")
    try:
        helper_evidence = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContainmentError("quota_provisioning", "task quota helper returned malformed JSON") from exc
    task = _validate_task_quota_result(
        helper_evidence, project=project, identifier=identifier,
        byte_limit=byte_limit, inode_limit=inode_limit,
    )
    return {
        "schema": QUOTA_ADMISSION_SCHEMA,
        "project": project,
        "pool": pool,
        "task_quota": task,
    }


def _quota_task_release(project: str, identifier: str) -> dict[str, object]:
    """Obtain exact cleanup evidence from the same fixed helper."""
    if project not in STORAGE_PROJECTS or not TASK_IDENTIFIER.fullmatch(identifier):
        raise ContainmentError("quota_identity", "task identifier is malformed")
    fd = _quota_helper_fd()
    try:
        try:
            result = subprocess.run(
                [f"/proc/self/fd/{fd}", "--operation", "release", "--project", project,
                 "--identifier", identifier], capture_output=True, text=True,
                timeout=10, check=False, pass_fds=(fd,), executable=f"/proc/self/fd/{fd}",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContainmentError("quota_cleanup", "task quota cleanup helper did not complete") from exc
    finally:
        os.close(fd)
    if result.returncode or len(result.stdout.encode("utf-8")) > 1024 * 1024:
        raise ContainmentError("quota_cleanup", "task quota cleanup helper failed")
    try:
        evidence = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContainmentError("quota_cleanup", "task quota cleanup returned malformed JSON") from exc
    expected_path = f"/home/duck-lint/symphony-workspaces/{project}/{identifier}"
    expected_id = 1_000_000 + int(identifier[2:])
    if (not isinstance(evidence, dict) or set(evidence) != {
            "schema", "project", "identifier", "workspace_path", "project_id",
            "workspace_state", "quota_state", "growth_possible", "remaining_bytes",
            "remaining_inodes"
        } or evidence.get("schema") != "symphony-pilot-task-quota-release/v1" or
            evidence.get("project") != project or evidence.get("identifier") != identifier or
            evidence.get("workspace_path") != expected_path or evidence.get("project_id") != expected_id or
            evidence.get("workspace_state") != "destroyed" or evidence.get("quota_state") != "removed" or
            evidence.get("growth_possible") is not False or evidence.get("remaining_bytes") != 0 or
            evidence.get("remaining_inodes") != 0):
        raise ContainmentError("quota_cleanup", "task quota cleanup proof is not exact")
    return evidence


def _contained_cwd(project_root: pathlib.Path, cwd: str) -> str:
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd or "\\" in cwd or ":" in cwd:
        raise ContainmentError("cwd", "Linux cwd is malformed")
    try:
        resolved = pathlib.Path(cwd).resolve(strict=True)
    except OSError as exc:
        raise ContainmentError("cwd", "Linux cwd cannot be resolved") from exc
    if not resolved.is_dir():
        raise ContainmentError("cwd", "Linux cwd is not a directory")
    try:
        relative = resolved.relative_to(project_root)
    except ValueError as exc:
        raise ContainmentError("cwd", "Linux cwd leaves the approved project root") from exc
    relative_text = relative.as_posix()
    return "/project" if relative_text == "." else "/project/" + relative_text


def _directory(path: pathlib.Path, kind: str, *, create: bool = False) -> pathlib.Path:
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ContainmentError(kind, f"{kind} directory cannot be prepared") from exc
    if not resolved.is_dir() or path.is_symlink():
        raise ContainmentError(kind, f"{kind} directory is not a plain directory")
    return resolved


def _file(path: pathlib.Path, kind: str) -> pathlib.Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ContainmentError(kind, f"{kind} executable cannot be prepared") from exc
    if path.is_symlink() or not resolved.is_file() or os.path.ismount(resolved):
        raise ContainmentError(kind, f"{kind} executable is not a plain file")
    return resolved


def _reject_mountpoint(path: pathlib.Path, kind: str) -> pathlib.Path:
    """Reject a writable source that is a bind/mount alias, not just a symlink."""
    resolved = _directory(path, kind)
    if os.path.ismount(resolved):
        raise ContainmentError(kind, f"{kind} directory is a mountpoint and cannot be rebound writable")
    return resolved


def _writable_outputs(project: str, root: pathlib.Path) -> list[tuple[pathlib.Path, str]]:
    if project != "symphony-runtime":
        return []
    outputs: list[tuple[pathlib.Path, str]] = []
    for name in ("bin", "burrito_out"):
        path = root / name
        try:
            path.mkdir(mode=0o700, exist_ok=True)
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ContainmentError("outputs", f"runtime output directory {name} cannot be prepared") from exc
        if path.is_symlink() or not resolved.is_dir():
            raise ContainmentError("outputs", f"runtime output directory {name} is not a plain directory")
        if os.path.ismount(resolved):
            raise ContainmentError("outputs", f"runtime output directory {name} is a mountpoint")
        outputs.append((resolved, f"/project/{name}"))
    return outputs


def _validate_outputs(project: str, root: pathlib.Path) -> None:
    """Reject poisoned artifact names before a trusted host step can consume them."""
    if project != "symphony-runtime":
        return
    for relative in ("bin/symphony", "burrito_out/symphony_linux_x86_64"):
        path = root / relative
        parent = path.parent
        if parent.is_symlink() or not parent.is_dir():
            raise ContainmentError("outputs", f"runtime output directory is no longer a plain directory: {parent.name}")
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ContainmentError("outputs", f"runtime output cannot be inspected: {relative}") from exc
        if path.is_symlink() or not stat.S_ISREG(mode):
            raise ContainmentError("outputs", f"runtime output is not a regular file: {relative}")


def run(project: str, cwd: str, command: list[str], wall_seconds: float) -> int:
    if os.name == "nt":
        raise ContainmentError("host_platform", "contained WSL execution requires Linux")
    if not isinstance(wall_seconds, (int, float)) or not 0 < wall_seconds <= 30 * 60:
        raise ContainmentError("timeout", "contained WSL wall-time limit is outside its bound")
    deployment_root = _deployment_root()
    _validate_deployment(deployment_root)
    containment = _load_containment(deployment_root)
    project_root = _project_root(project)
    contained_cwd = _contained_cwd(project_root, cwd)
    home = pathlib.Path.home().resolve()
    toolchain_executable = _file(home / ".local/bin/mise", "toolchain")
    toolchain_data = _directory(home / ".local/share/mise", "toolchain")
    identity = containment.require_backend()

    # Build/cache state is disposable by design.  A later hostile command must
    # not inherit data left by an earlier acceptance command.
    with tempfile.TemporaryDirectory(prefix="symphony-pilot-wsl-build-", dir="/tmp") as build_directory:
        build_root = _reject_mountpoint(pathlib.Path(build_directory), "build")
        with tempfile.TemporaryDirectory(prefix="symphony-pilot-wsl-domain-", dir="/tmp") as directory:
            root = pathlib.Path(directory) / "root"
            root.mkdir()
            domain = containment.acceptance_domain_command(
                identity,
                root,
                project_root,
                build_root,
                toolchain_executable,
                toolchain_data,
                contained_cwd,
                command,
                _writable_outputs(project, project_root),
            )
            try:
                result = containment.run_task_domain(domain, wall_seconds=wall_seconds)
            finally:
                _validate_outputs(project, project_root)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.timed_out:
        raise ContainmentError("timeout", "contained WSL command exceeded its wall-time limit")
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--cwd")
    parser.add_argument("--control", choices=("quota-inspect-root", "quota-admit-task", "quota-release-task"))
    parser.add_argument("--identifier")
    parser.add_argument("--byte-limit", type=int)
    parser.add_argument("--inode-limit", type=int)
    parser.add_argument("--wall-seconds", type=float, default=30 * 60)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        if args.control == "quota-inspect-root":
            if (args.cwd is not None or command or args.identifier is not None or
                    args.byte_limit is not None or args.inode_limit is not None):
                raise ContainmentError("control", "quota inspection arguments are malformed")
            # Control operations are still trusted deployment operations.  Do
            # not let their fixed nature bypass the manifest and inventory
            # gate that protects normal contained execution.
            deployment_root = _deployment_root()
            _validate_deployment(deployment_root)
        elif args.control == "quota-admit-task":
            if (args.cwd is not None or command or args.identifier is None or
                    args.byte_limit is None or args.inode_limit is None):
                raise ContainmentError("control", "quota admission arguments are malformed")
            deployment_root = _deployment_root()
            _validate_deployment(deployment_root)
        elif args.control == "quota-release-task":
            if (args.cwd is not None or command or args.identifier is None or
                    args.byte_limit is not None or args.inode_limit is not None):
                raise ContainmentError("control", "quota cleanup arguments are malformed")
            deployment_root = _deployment_root()
            _validate_deployment(deployment_root)
        elif args.cwd is None or not command:
            raise ContainmentError("command", "contained command is required")
        if args.control == "quota-inspect-root":
            print(json.dumps(_quota_inspection(args.project), sort_keys=True))
            return 0
        if args.control == "quota-admit-task":
            print(json.dumps(_quota_task_admission(
                args.project, args.identifier, args.byte_limit, args.inode_limit,
            ), sort_keys=True))
            return 0
        if args.control == "quota-release-task":
            print(json.dumps(_quota_task_release(
                args.project, args.identifier,
            ), sort_keys=True))
            return 0
        return run(args.project, args.cwd, command, args.wall_seconds)
    except Exception as exc:
        kind = getattr(exc, "kind", "containment")
        print(f"symphony-pilot contained WSL execution blocked: {kind}: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
