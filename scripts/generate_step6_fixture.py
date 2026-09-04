#!/usr/bin/env python3
"""Generate a disposable full-lifecycle SQLite projection for adapter checks.

The fixture deliberately contains more states and projects than one scheduler
configuration requests.  Runtime must filter by its configured active-state
set and project slug; this producer never imports Runtime code.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from control_db import ControlPlaneDatabase  # noqa: E402


BASE_SHA = "a" * 40
CREATED_AT = "2026-09-04T12:00:00+00:00"
STATES = (
    "PREPARED", "QUEUED", "PLANNED", "IMPLEMENTED", "REVIEW",
    "ADVERSARIAL_REVIEW", "FINAL_MECHANICAL_ACCEPTANCE", "ARCHIVIST",
)


def generate(output: pathlib.Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    with ControlPlaneDatabase.open(output) as database:
        for number, state in enumerate(STATES, start=1):
            task_id = f"{number:08d}-0000-0000-0000-000000000000"
            task = database.create_task(
                task_id=task_id,
                identifier=f"T-{number:06d}",
                project_slug="alpha",
                title=f"Alpha {state}",
                objective=f"Fixture task in {state}",
                base_ref="main",
                base_sha=BASE_SHA,
                current_head=BASE_SHA,
                state="PREPARED" if state == "QUEUED" else state,
                created_at=CREATED_AT,
            )
            if state == "QUEUED":
                database.queue_task(task["id"], project_slug="alpha")

        blocked = database.create_task(
            task_id="90000000-0000-0000-0000-000000000000",
            identifier="T-000009",
            project_slug="alpha",
            title="Alpha blocked active task",
            objective="Fixture blocked active task",
            base_ref="main",
            base_sha=BASE_SHA,
            current_head=BASE_SHA,
            state="REVIEW",
            created_at=CREATED_AT,
        )
        database.record_blocker(
            task_id=blocked["id"], kind="project", body="Fixture project decision pending",
            created_at=CREATED_AT,
        )
        database.create_task(
            task_id="a0000000-0000-0000-0000-000000000000",
            identifier="T-000010",
            project_slug="beta",
            title="Beta queued cross-project task",
            objective="Fixture task outside alpha scope",
            base_ref="main",
            base_sha=BASE_SHA,
            current_head=BASE_SHA,
            state="PREPARED",
            created_at=CREATED_AT,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=pathlib.Path)
    args = parser.parse_args()
    generate(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
