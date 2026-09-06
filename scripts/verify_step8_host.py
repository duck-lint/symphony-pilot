#!/usr/bin/env python3
"""Verify one Step-8 host tier and emit exact-SHA evidence."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

from step8_host_contract import (
    CONTRACT_PATH,
    contract_digest,
    collect_ubuntu_observation,
    load_contract,
    verify_exact_identity,
    verify_ubuntu_buildability,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _git_sha() -> str:
    supplied = os.environ.get("GITHUB_SHA", "")
    if len(supplied) == 40 and all(char in "0123456789abcdef" for char in supplied.lower()):
        return supplied.lower()
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                          capture_output=True, check=True).stdout.strip()


def _write(path: pathlib.Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", choices=("ubuntu-buildability", "windows-native", "target-twin"), required=True)
    parser.add_argument("--observed-file", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    contract = load_contract(CONTRACT_PATH)
    observed: dict[str, Any]
    if args.observed_file:
        observed = json.loads(args.observed_file.read_text(encoding="utf-8"))
    elif args.tier == "ubuntu-buildability":
        observed = collect_ubuntu_observation()
    else:
        raise SystemExit("an observed evidence file is required for Windows tiers")
    if not isinstance(observed, dict):
        raise SystemExit("observed host evidence must be a JSON object")

    if args.tier == "ubuntu-buildability":
        failures = verify_ubuntu_buildability(contract, observed)
        tier_name = "Tier A: native Ubuntu buildability"
    elif args.tier == "target-twin":
        failures = verify_exact_identity(contract, observed)
        tier_name = "Tier C: disposable Windows + WSL2 target twin"
    else:
        expected = contract["compatible_buildability"]["windows_native"]
        failures = [] if observed.get("windows", {}).get("architecture") == expected["architecture"] else ["windows.architecture"]
        tier_name = "Tier B: hosted Windows native contract"

    evidence = {
        "schema": "symphony-pilot-step8-evidence/v1",
        "git_sha": _git_sha(),
        "target_contract_schema": contract["schema"],
        "target_contract_version": contract["version"],
        "target_contract_sha256": contract_digest(CONTRACT_PATH),
        "runner_tier": args.tier,
        "tier_name": tier_name,
        "observed": observed,
        "result": "fail" if failures else "pass",
        "failures": failures,
        "recorded_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    _write(args.output, evidence)
    print(json.dumps(evidence, sort_keys=True))
    return 78 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
