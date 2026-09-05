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
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--wall-seconds", type=float, default=30 * 60)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        if not command:
            raise ContainmentError("command", "contained command is required")
        return run(args.project, args.cwd, command, args.wall_seconds)
    except Exception as exc:
        kind = getattr(exc, "kind", "containment")
        print(f"symphony-pilot contained WSL execution blocked: {kind}: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
