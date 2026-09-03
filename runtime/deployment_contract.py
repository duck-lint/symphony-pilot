"""Bounded source/deployment compatibility identity.

The deployment contains a snapshot of runtime hooks and policy, but lifecycle
authority remains in the source checkout.  This digest covers only source
modules whose changes can alter generation or lifecycle meaning; registry
membership, documentation, and unrelated project profiles are deliberately
outside the contract.
"""
from __future__ import annotations

import hashlib
import json
import pathlib


CONTRACT_FILES = (
    "scripts/deploy.py",
    "scripts/project.py",
    "runtime/after_run.py",
    "runtime/broker.py",
    "runtime/before_remove.py",
    "runtime/host_integration.py",
    "runtime/prepare_workspace.py",
    "runtime/process_identity.py",
    "runtime/render_workflow.py",
    "runtime/project_registry.py",
    "runtime/deployment_contract.py",
    "runtime/containment.py",
    "runtime/wsl_contained_exec.py",
    "runtime/task_admission.py",
    "runtime/dispatch_provenance.py",
    "runtime/admit_task.py",
    "runtime/outbox.py",
    "runtime/rulesets.py",
    "runtime/publication.py",
    "runtime/runtime_lock.py",
)


def contract_digest(source_root: pathlib.Path) -> str:
    """Hash the bounded lifecycle/generation contract with path framing."""
    digest = hashlib.sha256()
    for relative in CONTRACT_FILES:
        path = source_root / relative
        if not path.is_file():
            raise FileNotFoundError(f"contract file is missing: {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def deployment_identity(profile: str, profile_sha256: str,
                        operator_contract_sha256: str, files: dict[str, str]) -> str:
    """Return the stable identity of one generated deployment snapshot."""
    payload = {
        "files": files,
        "operator_contract_sha256": operator_contract_sha256,
        "profile": profile,
        "profile_sha256": profile_sha256,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
