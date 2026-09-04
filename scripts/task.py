#!/usr/bin/env python3
"""Trusted operator intake for local Symphony tasks.

This is deliberately a host CLI, not a browser mutation endpoint. The
operator supplies project meaning only; the registered profile and host Git
derive repository identity, default ref, base SHA, UUID, local identifier, and
task branch.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from control_db import (ControlPlaneDatabase, ControlPlaneError,
                        default_database_path)  # noqa: E402
from project_registry import resolve_project  # noqa: E402


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
IDENTIFIER_RE = re.compile(r"^T-[0-9]{6}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class TaskCommandError(RuntimeError):
    pass


def resolve_remote_head(git_remote: str) -> tuple[str, str]:
    """Resolve an unambiguous remote HEAD using structured Git argv."""
    result = subprocess.run(
        ["git", "ls-remote", "--symref", git_remote, "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise TaskCommandError("registered Git remote HEAD could not be resolved")
    symbolic: list[str] = []
    heads: list[str] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 2:
            raise TaskCommandError("registered Git remote HEAD response is ambiguous")
        value, name = fields
        if name != "HEAD":
            raise TaskCommandError("registered Git remote returned an unexpected HEAD record")
        if value.startswith("ref: refs/heads/"):
            symbolic.append(value.removeprefix("ref: refs/heads/"))
        elif SHA_RE.fullmatch(value):
            heads.append(value)
        else:
            raise TaskCommandError("registered Git remote HEAD response is invalid")
    if (len(symbolic), len(heads)) != (1, 1):
        raise TaskCommandError("registered Git remote default ref/HEAD is not unambiguous")
    base_ref = symbolic[0]
    if (not REF_RE.fullmatch(base_ref) or ".." in base_ref or "//" in base_ref or
            base_ref.endswith(("/", ".")) or "@{" in base_ref):
        raise TaskCommandError("registered Git remote default ref is invalid")
    return base_ref, heads[0]


def _emit(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _profile(project: str):
    if not isinstance(project, str) or not project:
        raise TaskCommandError("--project is required")
    try:
        return resolve_project(project, ROOT / "projects")
    except Exception as exc:
        raise TaskCommandError(f"project is not registered: {project}") from exc


def _task_selector(value: str) -> tuple[str, str]:
    if IDENTIFIER_RE.fullmatch(value):
        return "identifier", value
    if UUID_RE.fullmatch(value):
        return "uuid", value
    raise TaskCommandError("task selector must be T-000001 or a canonical lowercase UUID")


def create(args: argparse.Namespace) -> int:
    profile = _profile(args.project)
    base_ref, base_sha = resolve_remote_head(profile.git_remote)
    with ControlPlaneDatabase.open(default_database_path()) as database:
        task = database.create_task(
            project_slug=profile.slug,
            title=args.title,
            objective=args.objective,
            base_ref=base_ref,
            base_sha=base_sha,
            state="PREPARED",
        )
    _emit(task)
    return 0


def queue(args: argparse.Namespace) -> int:
    profile = _profile(args.project)
    selector_kind, selector = _task_selector(args.task)
    with ControlPlaneDatabase.open(default_database_path()) as database:
        task = (database.read_task_by_identifier(selector, project_slug=profile.slug)
                if selector_kind == "identifier" else database.read_task(selector))
        if task["project_slug"] != profile.slug:
            raise TaskCommandError("task is not registered to this project")
        task = database.transition_task(
            task["id"], expected_state="PREPARED", new_state="QUEUED",
            event_type="queued",
            payload={"identifier": task["identifier"], "project_slug": profile.slug},
        )
    _emit(task)
    return 0


def show(args: argparse.Namespace) -> int:
    profile = _profile(args.project)
    selector_kind, selector = _task_selector(args.task)
    with ControlPlaneDatabase.open_readonly(default_database_path()) as database:
        task = (database.read_task_by_identifier(selector, project_slug=profile.slug)
                if selector_kind == "identifier" else database.read_task(selector))
        if task["project_slug"] != profile.slug:
            raise TaskCommandError("task is not registered to this project")
        _emit(database.read_projection(task["id"]))
    return 0


def list_tasks(args: argparse.Namespace) -> int:
    profile = _profile(args.project)
    with ControlPlaneDatabase.open_readonly(default_database_path()) as database:
        _emit(database.list_tasks(project_slug=profile.slug))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="task.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="create a PREPARED local task")
    create_parser.add_argument("--project", required=True)
    create_parser.add_argument("--title", required=True)
    create_parser.add_argument("--objective", required=True)
    create_parser.set_defaults(handler=create)

    queue_parser = subparsers.add_parser("queue", help="transition one PREPARED task to QUEUED")
    queue_parser.add_argument("--project", required=True)
    queue_parser.add_argument("--task", required=True)
    queue_parser.set_defaults(handler=queue)

    show_parser = subparsers.add_parser("show", help="show one local task and its projection")
    show_parser.add_argument("--project", required=True)
    show_parser.add_argument("--task", required=True)
    show_parser.set_defaults(handler=show)

    list_parser = subparsers.add_parser("list", help="list local tasks for one project")
    list_parser.add_argument("--project", required=True)
    list_parser.set_defaults(handler=list_tasks)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (TaskCommandError, ControlPlaneError, OSError, ValueError) as exc:
        print(f"symphony-pilot task command stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
