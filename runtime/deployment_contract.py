"""Bounded source/deployment compatibility identity.

The deployment contains a snapshot of runtime hooks and policy, but lifecycle
authority remains in the source checkout. This digest covers source modules
and constitutive policy files whose changes can alter generation or lifecycle
meaning; registry membership, documentation, and unrelated project profiles
are deliberately outside the contract.
"""
from __future__ import annotations

import hashlib
import json
import pathlib


ROLE_POLICY_FILES = tuple(
    f"workflow/agents/{name}.toml"
    for name in ("adversary", "archivist", "implementer", "planner", "project-manager", "reviewer")
)
POLICY_FILES = ("workflow/architect_policy.md", *ROLE_POLICY_FILES)


CONTRACT_FILES = (
    "scripts/deploy.py",
    "scripts/project.py",
    "scripts/task.py",
    "runtime/after_run.py",
    "runtime/before_run.py",
    "runtime/lifecycle.py",
    "runtime/before_remove.py",
    "runtime/host_integration.py",
    "runtime/prepare_workspace.py",
    "runtime/workspace_boundary.py",
    "runtime/process_identity.py",
    "runtime/render_workflow.py",
    "runtime/project_registry.py",
    "runtime/deployment_contract.py",
    "runtime/containment.py",
    "runtime/wsl_contained_exec.py",
    "runtime/control_db.py",
    "runtime/runtime_lock.py",
    "runtime/launch_codex.sh",
    # Step-7 publication remains host-only, but its source is authority-
    # relevant and must invalidate a reviewed deployment when it changes.
    "runtime/publication.py",
    "runtime/publication_key.py",
    "runtime/rulesets.py",
    "scripts/provision_publication_key.py",
    *POLICY_FILES,
)

# Complete runtime snapshot for a Step-6 deployment. Publication remains
# source-only; the SQLite lifecycle broker is deployed and manifest-covered.
DEPLOYED_RUNTIME_FILES = (
    "runtime/prepare_workspace.py",
    "runtime/workspace_boundary.py",
    "runtime/after_run.py",
    "runtime/before_run.py",
    "runtime/lifecycle.py",
    "runtime/before_remove.py",
    "runtime/host_integration.py",
    "runtime/process_identity.py",
    "runtime/launch_codex.sh",
    "runtime/deployment_contract.py",
    "runtime/containment.py",
    "runtime/wsl_contained_exec.py",
    "runtime/control_db.py",
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
