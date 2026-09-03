#!/usr/bin/env python3
"""Run one approved host command inside Pilot's Linux containment domain.

The Windows adapter supplies only the fixed project name and Linux argv.  This
trusted Linux-side supervisor resolves the project again, creates a dedicated
build/cache area, and reuses ``runtime.containment`` for the actual namespace,
mount, resource, and descendant-teardown boundary.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import tempfile

from containment import (
    ContainmentError,
    acceptance_domain_command,
    require_backend,
    run_task_domain,
)


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
        outputs.append((resolved, f"/project/{name}"))
    return outputs


def run(project: str, cwd: str, command: list[str]) -> int:
    if os.name == "nt":
        raise ContainmentError("host_platform", "contained WSL execution requires Linux")
    project_root = _project_root(project)
    contained_cwd = _contained_cwd(project_root, cwd)
    home = pathlib.Path.home().resolve()
    toolchain_bin = _directory(home / ".local/bin", "toolchain")
    toolchain_data = _directory(home / ".local/share/mise", "toolchain")
    build_root = _directory(home / ".local/state/symphony-pilot/wsl-build" / project, "build", create=True)
    control_source = pathlib.Path(__file__).resolve().parents[1]
    control_source = _directory(control_source, "pilot-control")
    identity = require_backend()

    with tempfile.TemporaryDirectory(prefix="symphony-pilot-wsl-domain-", dir="/tmp") as directory:
        root = pathlib.Path(directory) / "root"
        root.mkdir()
        domain = acceptance_domain_command(
            identity,
            root,
            control_source,
            project_root,
            build_root,
            toolchain_bin,
            toolchain_data,
            contained_cwd,
            command,
            _writable_outputs(project, project_root),
        )
        result = run_task_domain(domain, wall_seconds=30 * 60)
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
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        if not command:
            raise ContainmentError("command", "contained command is required")
        return run(args.project, args.cwd, command)
    except ContainmentError as exc:
        print(f"symphony-pilot contained WSL execution blocked: {exc.kind}: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
