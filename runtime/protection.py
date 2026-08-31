#!/usr/bin/env python3
"""Pure branch-protection admission checks for server metadata."""
from __future__ import annotations


class ProtectionError(ValueError):
    pass


def require_protected_default(metadata: object, automation_actor: str) -> dict:
    """Accept only metadata proving PR-only human merge and no bypass."""
    if not isinstance(metadata, dict):
        raise ProtectionError("branch-protection metadata is unavailable")
    required = {"protected", "required_pull_request", "automation_can_bypass", "human_merge_actor"}
    if set(metadata) != required:
        raise ProtectionError("branch-protection metadata is incomplete")
    if metadata["protected"] is not True or metadata["required_pull_request"] is not True:
        raise ProtectionError("protected default branch is not PR-only")
    if metadata["automation_can_bypass"] is not False:
        raise ProtectionError("automation identity can bypass default-branch protection")
    if not isinstance(metadata["human_merge_actor"], str) or not metadata["human_merge_actor"]:
        raise ProtectionError("human merge authority is not identified")
    if metadata["human_merge_actor"] == automation_actor:
        raise ProtectionError("automation identity cannot be the human merge authority")
    return metadata
