#!/usr/bin/env python3
"""Trusted operator verification and cleanup of registered storage.

Linux work is restricted to fixed quota capabilities exposed by the WSL
adapter. This command never provisions devices, selects quota IDs, or accepts
paths and limits from the caller. Release accepts only proof that the exact
task workspace and quota cannot grow.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from control_db import ControlPlaneDatabase, ControlPlaneError, default_database_path  # noqa: E402
from project_registry import resolve_project  # noqa: E402
from storage import (StorageContractError, capacity_snapshot,
                     storage_release_proof_from_evidence,
                     verify_storage_evidence)  # noqa: E402


def verify(args: argparse.Namespace) -> int:
    profile = resolve_project(args.project, ROOT / "projects")
    from wsl_adapter import WslAdapterError, inspect_quota

    try:
        evidence = inspect_quota(profile.slug, request_id=f"storage-{profile.slug}-verify")
        domain = verify_storage_evidence(
            profile.slug, evidence, profile.storage_policy,
            expected_target=str(profile.workspace_root.parent),
        )
    except (WslAdapterError, StorageContractError) as exc:
        raise ControlPlaneError(f"storage domain verification failed closed: {exc}") from exc
    with ControlPlaneDatabase.open(default_database_path()) as database:
        saved = database.record_storage_domain(domain)
        totals = database.storage_reservation_totals(profile.slug)
    values = capacity_snapshot(
        saved, totals["reserved_bytes"], totals["reserved_inodes"],
        configured_pool_bytes=profile.storage_policy.allocatable_pool_bytes,
        configured_backing_bytes=profile.storage_policy.pool_bytes,
    )
    print(json.dumps({"project": profile.slug, "domain": saved, "capacity": values}, sort_keys=True))
    return 0


def release(args: argparse.Namespace) -> int:
    profile = resolve_project(args.project, ROOT / "projects")
    from wsl_adapter import WslAdapterError, release_task_quota

    with ControlPlaneDatabase.open(default_database_path()) as database:
        task = database.read_task_by_identifier(args.task, project_slug=profile.slug)
        try:
            evidence = release_task_quota(
                profile.slug, str(task["identifier"]),
                request_id=f"storage-{profile.slug}-{task['identifier']}-release",
            )
            proof = storage_release_proof_from_evidence(
                evidence, project=profile.slug, identifier=str(task["identifier"]),
            )
            released = database.release_storage_reservation(task["id"], proof=proof)
        except (WslAdapterError, StorageContractError) as exc:
            raise ControlPlaneError(f"storage cleanup failed closed: {exc}") from exc
    print(json.dumps({"project": profile.slug, "task": task["identifier"],
                      "reservation": released}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="storage_cli.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify_parser = subparsers.add_parser("verify", help="verify and record the dedicated quota domain")
    verify_parser.add_argument("--project", required=True)
    verify_parser.set_defaults(handler=verify)
    release_parser = subparsers.add_parser("release", help="release after fixed-helper cleanup proof")
    release_parser.add_argument("--project", required=True)
    release_parser.add_argument("--task", required=True)
    release_parser.set_defaults(handler=release)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (ControlPlaneError, OSError, ValueError, RuntimeError) as exc:
        print(f"symphony-pilot storage command stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
