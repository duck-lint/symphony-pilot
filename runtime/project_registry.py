"""Canonical project registry discovery and cardinality-wide validation.

The tracked ``projects/<slug>/profile.toml`` files are the registry.  This
module deliberately contains no project names: identity enters only through
validated profile data or an operator-selected slug.
"""
from __future__ import annotations

import pathlib
import re
from collections.abc import Iterable

from prepare_workspace import (
    DASHBOARD_PORT_MAX,
    DASHBOARD_PORT_MIN,
    Profile,
    PreparationError,
    load_profile,
    project_namespaces,
)


def discover_profile_paths(registry_root: pathlib.Path) -> tuple[pathlib.Path, ...]:
    """Discover every canonical profile in deterministic path order."""
    return tuple(sorted(registry_root.glob("*/profile.toml")))


def validate_profiles(
    profiles: Iterable[Profile],
    profile_paths: Iterable[pathlib.Path] | None = None,
) -> tuple[Profile, ...]:
    """Validate global uniqueness and non-overlap for an arbitrary registry.

    Resource namespaces are compared across projects, including containment,
    because a project that owns a recursively removable parent can destroy a
    different project's child namespace even when the strings differ.
    """
    items = tuple(profiles)
    paths = tuple(profile_paths or ())
    errors: list[str] = []
    if paths and len(paths) != len(items):
        raise PreparationError("registry", "profile paths and profiles have different cardinality")

    seen_slugs: dict[str, Profile] = {}
    seen_repositories: dict[str, Profile] = {}
    seen_services: dict[str, Profile] = {}
    seen_ports: dict[int, Profile] = {}
    shared_storage_policy = None
    for index, profile in enumerate(items):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", profile.slug):
            errors.append(f"unsafe slug: {profile.slug!r}")
        previous = seen_slugs.get(profile.slug)
        if previous:
            errors.append(f"duplicate slug: {profile.slug}")
        seen_slugs[profile.slug] = profile
        previous = seen_repositories.get(profile.repository)
        if previous:
            errors.append(f"duplicate repository identity: {profile.repository}")
        seen_repositories[profile.repository] = profile
        previous = seen_services.get(profile.service_identity)
        if previous:
            errors.append(f"duplicate service identity: {profile.service_identity}")
        seen_services[profile.service_identity] = profile
        previous = seen_ports.get(profile.dashboard_port)
        if previous:
            errors.append(f"duplicate dashboard port: {profile.dashboard_port}")
        seen_ports[profile.dashboard_port] = profile
        policy = profile.storage_policy
        policy_values = (
            policy.pool_bytes, policy.allocatable_pool_bytes, policy.task_bytes, policy.task_inodes,
            policy.emergency_reserve_bytes, policy.emergency_reserve_inodes,
        )
        if shared_storage_policy is None:
            shared_storage_policy = policy_values
        elif policy_values != shared_storage_policy:
            errors.append("registered projects must use one shared storage-pool policy")
        if (not isinstance(profile.dashboard_port, int) or isinstance(profile.dashboard_port, bool) or
                not DASHBOARD_PORT_MIN <= profile.dashboard_port <= DASHBOARD_PORT_MAX):
            errors.append(
                f"dashboard port outside supported finite range: {profile.dashboard_port}"
            )
        if paths:
            expected = paths[index].parent.name
            if expected != profile.slug:
                errors.append(
                    f"profile directory/slug mismatch: {paths[index].parent.name} != {profile.slug}"
                )

    namespace_entries: list[tuple[str, str, pathlib.PurePath]] = []
    for profile in items:
        for name, path in project_namespaces(profile).items():
            namespace_entries.append((profile.slug, name, path))
    for index, (left_slug, left_name, left_path) in enumerate(namespace_entries):
        for right_slug, right_name, right_path in namespace_entries[index + 1:]:
            if left_slug == right_slug:
                continue
            if _paths_overlap(left_path, right_path):
                errors.append(
                    f"project namespace overlap: {left_slug}.{left_name}={left_path} "
                    f"and {right_slug}.{right_name}={right_path}"
                )
    if errors:
        raise PreparationError("registry", "; ".join(dict.fromkeys(errors)))
    return items


def validate_registry(registry_root: pathlib.Path) -> tuple[Profile, ...]:
    """Load and validate the complete tracked registry, including the empty set."""
    paths = discover_profile_paths(registry_root)
    profiles: list[Profile] = []
    for path in paths:
        try:
            profiles.append(load_profile(path))
        except PreparationError as exc:
            raise PreparationError("registry", f"{path}: {exc}") from exc
    return validate_profiles(profiles, paths)


def resolve_project(slug: str, registry_root: pathlib.Path) -> Profile:
    """Resolve one operator-selected slug through the canonical registry."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
        raise PreparationError("project", "project slug is not a safe identifier")
    profiles = validate_registry(registry_root)
    matches = [profile for profile in profiles if profile.slug == slug]
    if not matches:
        raise PreparationError("project", f"project is not registered: {slug}")
    return matches[0]


def suggest_dashboard_port(registry_root: pathlib.Path) -> int:
    """Choose the lowest unassigned port after validating the whole registry."""
    profiles = validate_registry(registry_root)
    assigned = {profile.dashboard_port for profile in profiles}
    for port in range(DASHBOARD_PORT_MIN, DASHBOARD_PORT_MAX + 1):
        if port not in assigned:
            return port
    raise PreparationError("dashboard_port", "the finite dashboard port range is exhausted")


def _paths_overlap(left: pathlib.PurePath, right: pathlib.PurePath) -> bool:
    return _contains(left, right) or _contains(right, left)


def _contains(parent: pathlib.PurePath, child: pathlib.PurePath) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
