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
from control_db import StateConflict  # noqa: E402
from project_registry import resolve_project  # noqa: E402
from storage import (StorageAdmissionProof, StorageContractError,
                     task_quota_binding_from_evidence,
                     verify_storage_evidence)  # noqa: E402


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


def verify_profile_storage(profile, identifier: str, *, database, task_id: str):
    """Obtain task proof only after SQLite has durably reserved capacity."""
    reservation = database.read_storage_reservation(task_id)
    if (reservation is None or reservation["status"] != "reserved" or
            reservation["project_slug"] != profile.slug):
        raise TaskCommandError("exact durable storage reservation is required before quota mutation")
    try:
        from wsl_adapter import WslAdapterError, admit_task_quota
        evidence = admit_task_quota(
            profile.slug, identifier,
            byte_limit=profile.storage_policy.task_bytes,
            inode_limit=profile.storage_policy.task_inodes,
            request_id=f"storage-{profile.slug}-{identifier}-admission",
        )
    except WslAdapterError as exc:
        raise TaskCommandError(f"trusted storage capability is unavailable: {exc.kind}") from exc
    try:
        pool_evidence = evidence.get("pool")
        if not isinstance(pool_evidence, dict):
            raise StorageContractError("task quota admission has no shared-pool proof")
        domain = verify_storage_evidence(
            profile.slug, pool_evidence, profile.storage_policy,
            # The project and T-N paths are children of one shared physical
            # pool; they are not independently mounted storage domains.
            expected_target=str(profile.workspace_root.parent),
        )
        binding = task_quota_binding_from_evidence(
            evidence, project=profile.slug, identifier=identifier,
            policy=profile.storage_policy,
        )
        return StorageAdmissionProof(domain=domain, binding=binding)
    except StorageContractError as exc:
        raise TaskCommandError(f"storage admission failed closed: {exc}") from exc


def verify_profile_storage_pool(profile):
    """Obtain only the shared-pool proof before reserving capacity."""
    try:
        from wsl_adapter import WslAdapterError, inspect_quota
        evidence = inspect_quota(
            profile.slug, request_id=f"storage-{profile.slug}-pool-admission",
        )
        return verify_storage_evidence(
            profile.slug, evidence, profile.storage_policy,
            expected_target=str(profile.workspace_root.parent),
        )
    except (WslAdapterError, StorageContractError) as exc:
        raise TaskCommandError(f"trusted storage pool capability is unavailable: {exc}") from exc


def queue(args: argparse.Namespace) -> int:
    profile = _profile(args.project)
    selector_kind, selector = _task_selector(args.task)
    with ControlPlaneDatabase.open(default_database_path()) as database:
        task = (database.read_task_by_identifier(selector, project_slug=profile.slug)
                if selector_kind == "identifier" else database.read_task(selector))
        if task["project_slug"] != profile.slug:
            raise TaskCommandError("task is not registered to this project")
        if task["state"] != "PREPARED":
            raise StateConflict("only PREPARED tasks may be queued")
        try:
            # The SQLite reservation commits before the privileged helper can
            # mutate project-quota state.  A retained PREPARED row is the
            # crash-recovery record for an uncertain helper outcome.
            domain = verify_profile_storage_pool(profile)
            database.reserve_storage_capacity(
                task["id"], project_slug=profile.slug, domain=domain,
                policy=profile.storage_policy,
            )
            admission = verify_profile_storage(
                profile, str(task["identifier"]), database=database, task_id=str(task["id"]),
            )
            task = database.queue_task_with_storage(
                task["id"], project_slug=profile.slug,
                domain=admission.domain, policy=profile.storage_policy,
                assignment=admission.binding,
            )
        except (TaskCommandError, StateConflict, StorageContractError) as exc:
            database.record_blocker(
                task_id=task["id"], kind="infrastructure",
                body=f"storage admission failed: {type(exc).__name__}: {exc}",
            )
            raise
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


def _read_project_task(database: ControlPlaneDatabase, profile, selector_kind: str, selector: str):
    task = (database.read_task_by_identifier(selector, project_slug=profile.slug)
            if selector_kind == "identifier" else database.read_task(selector))
    if task["project_slug"] != profile.slug:
        raise TaskCommandError("task is not registered to this project")
    return task


def blockers(args: argparse.Namespace) -> int:
    profile = _profile(args.project)
    selector_kind, selector = _task_selector(args.task)
    with ControlPlaneDatabase.open_readonly(default_database_path()) as database:
        task = _read_project_task(database, profile, selector_kind, selector)
        _emit([row for row in database.read_projection(task["id"])["blockers"] if row["status"] == "open"])
    return 0


def resolve_blocker(args: argparse.Namespace) -> int:
    profile = _profile(args.project)
    selector_kind, selector = _task_selector(args.task)
    if not UUID_RE.fullmatch(args.blocker):
        raise TaskCommandError("--blocker must be a canonical lowercase UUID")
    with ControlPlaneDatabase.open(default_database_path()) as database:
        task = _read_project_task(database, profile, selector_kind, selector)
        blocker = database.read_blocker(args.blocker)
        if blocker["task_id"] != task["id"]:
            raise TaskCommandError("blocker is not owned by the selected task")
        _emit(database.resolve_blocker(args.blocker))
    return 0


def fail_attempt(args: argparse.Namespace) -> int:
    from lifecycle import fail_attempt as fail_stale_attempt

    profile = _profile(args.project)
    selector_kind, selector = _task_selector(args.task)
    with ControlPlaneDatabase.open_readonly(default_database_path()) as database:
        task = _read_project_task(database, profile, selector_kind, selector)
    if not fail_stale_attempt(profile, str(task["id"]), detail="operator failed stale Architect attempt"):
        raise TaskCommandError("the selected task has no single started Architect attempt")
    _emit({"task_uuid": task["id"], "status": "failed", "blocker": "infrastructure"})
    return 0


def list_tasks(args: argparse.Namespace) -> int:
    profile = _profile(args.project)
    with ControlPlaneDatabase.open_readonly(default_database_path()) as database:
        _emit(database.list_tasks(project_slug=profile.slug))
    return 0


def bind_publication_key(args: argparse.Namespace) -> int:
    """Bind one already registered writable deploy key to the local key."""
    from publication_key import bind_server_key
    from prepare_workspace import github, read_secret

    profile = _profile(args.project)
    token = read_secret(profile)
    _emit(bind_server_key(profile, token, github))
    return 0


def publish(args: argparse.Namespace) -> int:
    """Publish one exact ARCHIVIST head through the trusted host broker."""
    from project import verify_deployment
    from publication import publish_task

    profile = _profile(args.project)
    selector_kind, selector = _task_selector(args.task)
    with ControlPlaneDatabase.open_readonly(default_database_path()) as database:
        task = _read_project_task(database, profile, selector_kind, selector)
    result = publish_task(
        profile, str(task["id"]), database_path=default_database_path(),
        deployment_check=verify_deployment,
    )
    _emit(result)
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

    blockers_parser = subparsers.add_parser("blockers", help="inspect open blockers for one task")
    blockers_parser.add_argument("--project", required=True)
    blockers_parser.add_argument("--task", required=True)
    blockers_parser.set_defaults(handler=blockers)

    resolve_parser = subparsers.add_parser("resolve-blocker", help="resolve one exact task blocker")
    resolve_parser.add_argument("--project", required=True)
    resolve_parser.add_argument("--task", required=True)
    resolve_parser.add_argument("--blocker", required=True)
    resolve_parser.set_defaults(handler=resolve_blocker)

    fail_parser = subparsers.add_parser("fail-attempt", help="fail the one stale Architect attempt")
    fail_parser.add_argument("--project", required=True)
    fail_parser.add_argument("--task", required=True)
    fail_parser.set_defaults(handler=fail_attempt)

    show_parser = subparsers.add_parser("show", help="show one local task and its projection")
    show_parser.add_argument("--project", required=True)
    show_parser.add_argument("--task", required=True)
    show_parser.set_defaults(handler=show)

    list_parser = subparsers.add_parser("list", help="list local tasks for one project")
    list_parser.add_argument("--project", required=True)
    list_parser.set_defaults(handler=list_tasks)

    bind_parser = subparsers.add_parser("bind-publication-key", help="bind the registered GitHub deploy key")
    bind_parser.add_argument("--project", required=True)
    bind_parser.set_defaults(handler=bind_publication_key)

    publish_parser = subparsers.add_parser("publish", help="publish one exact ARCHIVIST task head")
    publish_parser.add_argument("--project", required=True)
    publish_parser.add_argument("--task", required=True)
    publish_parser.set_defaults(handler=publish)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (TaskCommandError, ControlPlaneError, OSError, ValueError, RuntimeError) as exc:
        print(f"symphony-pilot task command stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
