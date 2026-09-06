#!/usr/bin/env python3
"""Run the full Pilot suite and emit bounded failure identities for CI."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import traceback
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failure_records: list[tuple[str, str]] = []

    def addFailure(self, test, err):  # noqa: N802 - unittest API
        super().addFailure(test, err)
        self.failure_records.append((test.id(), "failure", "".join(traceback.format_exception(*err))))

    def addError(self, test, err):  # noqa: N802 - unittest API
        super().addError(test, err)
        self.failure_records.append((test.id(), "error", "".join(traceback.format_exception(*err))))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=pathlib.Path)
    args = parser.parse_args()
    stream = sys.stdout
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        log = args.log.open("w", encoding="utf-8")
    else:
        log = None

    class Tee:
        def write(self, value):
            stream.write(value)
            if log:
                log.write(value)

        def flush(self):
            stream.flush()
            if log:
                log.flush()

    try:
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test*.py", top_level_dir=str(ROOT))
        runner = unittest.TextTestRunner(stream=Tee(), verbosity=2, resultclass=RecordingResult)
        result = runner.run(suite)
        summary = {
            "schema": "symphony-pilot-step8-test-result/v1",
            "tests_run": result.testsRun,
            "failures": [
                {"test": test, "kind": kind, "detail": " ".join(detail.split())[:2400]}
                for test, kind, detail in result.failure_records
            ],
            "successful": result.wasSuccessful(),
        }
        print(json.dumps(summary, sort_keys=True))
        for record in summary["failures"]:
            print(f"::error file=tests::{record['kind']} {record['test']}: {record['detail']}")
        return 0 if result.wasSuccessful() else 1
    finally:
        if log:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
