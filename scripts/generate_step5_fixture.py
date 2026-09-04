#!/usr/bin/env python3
"""Create a disposable Pilot-owned SQLite fixture for Runtime verification.

The fixture intentionally contains both projects and both dispatchable and
non-dispatchable states. It is validation evidence only; it is never a
production migration or a second scheduler implementation.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from control_db import open_database  # noqa: E402


BASE_SHA = "a" * 40
TASKS = (
    ("11111111-1111-1111-1111-111111111111", "alpha", "PREPARED", "Alpha prepared"),
    ("22222222-2222-2222-2222-222222222222", "alpha", "QUEUED", "Alpha queued"),
    ("33333333-3333-3333-3333-333333333333", "alpha", "QUEUED", "Alpha blocked"),
    ("44444444-4444-4444-4444-444444444444", "alpha", "READY_FOR_HUMAN_MERGE", "Alpha terminal"),
    ("55555555-5555-5555-5555-555555555555", "beta", "QUEUED", "Beta queued"),
)


def generate(output: pathlib.Path) -> pathlib.Path:
    output = output.expanduser().absolute()
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open_database(output) as database:
        rows = []
        for task_id, project_slug, state, title in TASKS:
            rows.append(database.create_task(
                task_id=task_id, project_slug=project_slug, title=title,
                objective=f"Objective for {title}", base_ref="main", base_sha=BASE_SHA,
                state=state, created_at="2026-09-03T12:00:00+00:00",
            ))
        database.record_blocker(
            task_id=rows[2]["id"], kind="project", body="Project decision pending",
            created_at="2026-09-03T12:00:00+00:00",
        )
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=pathlib.Path)
    args = parser.parse_args()
    try:
        print(generate(args.output))
        return 0
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"fixture generation stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
