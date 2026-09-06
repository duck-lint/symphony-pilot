#!/usr/bin/env python3
"""Bind a final CI result to the exact checkout and target contract."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess

from step8_host_contract import CONTRACT_PATH, contract_digest, load_contract


ROOT = pathlib.Path(__file__).resolve().parents[1]


def git_sha() -> str:
    supplied = os.environ.get("GITHUB_SHA", "")
    if len(supplied) == 40 and all(char in "0123456789abcdef" for char in supplied.lower()):
        return supplied.lower()
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                          capture_output=True, check=True).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", required=True)
    parser.add_argument("--result", choices=("success", "failure", "cancelled", "skipped"), required=True)
    parser.add_argument("--observed-file", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--deployment-identity")
    args = parser.parse_args()
    contract = load_contract(CONTRACT_PATH)
    evidence = json.loads(args.observed_file.read_text(encoding="utf-8"))
    if not isinstance(evidence, dict) or evidence.get("git_sha") != git_sha():
        raise SystemExit("cannot bind evidence from a different Git SHA")
    if evidence.get("target_contract_sha256") != contract_digest(CONTRACT_PATH):
        raise SystemExit("cannot bind evidence from a different target contract")
    final = {
        "schema": "symphony-pilot-step8-evidence/v1",
        "git_sha": git_sha(),
        "target_contract_schema": contract["schema"],
        "target_contract_version": contract["version"],
        "target_contract_sha256": contract_digest(CONTRACT_PATH),
        "runner_tier": args.tier,
        "observed": evidence.get("observed", {}),
        "host_verification": evidence.get("result"),
        "result": args.result,
        "failures": evidence.get("failures", []),
        "recorded_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if args.deployment_identity:
        final["deployment_identity"] = args.deployment_identity
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(final, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(final, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
