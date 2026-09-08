#!/usr/bin/env python3
"""Render the thin Runtime prompt from the Pilot execution projection."""
from __future__ import annotations

import argparse
import pathlib
import shlex
import sys
import tomllib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from prepare_workspace import Profile, control_database_path, load_profile

ROLE_FILES = {
    "PROJECT-MANAGER": "project-manager",
    "PLANNER": "planner",
    "IMPLEMENTER": "implementer",
    "REVIEWER": "reviewer",
    "ADVERSARY": "adversary",
    "ARCHIVIST": "archivist",
}


def render(profile: Profile, install_root: pathlib.Path) -> str:
    """Generate policy only; lifecycle meaning and grants remain Pilot-owned."""
    runtime = install_root / "runtime"
    profile_path = install_root / "profile.toml"
    shell = lambda value: shlex.quote(str(value))
    lines = [
        "---", "pilot:",
        f"  database_path: {shell(control_database_path(profile))}",
        f"  project_slug: {profile.slug}",
        "  execution_projection: read-only",
        "polling:", f"  interval_ms: {profile.poll_interval_ms}",
        f"  max_retry_backoff_ms: {profile.max_retry_backoff_ms}",
        "workspace:", f"  root: {profile.workspace_root}",
        "hooks:", "  after_create: |",
        f"    git clone --no-single-branch {shell(profile.git_remote)} .",
        "  before_run: |", "    set -eu",
        f"    exec python3 {shell(runtime / 'before_run.py')} --profile {shell(profile_path)} --workspace \"$PWD\"",
        "  after_run: |", "    set -eu",
        f"    exec python3 {shell(runtime / 'after_run.py')} --profile {shell(profile_path)} --workspace \"$PWD\"",
        "  before_remove: |",
        f"    python3 {shell(runtime / 'before_remove.py')} --profile {shell(profile_path)} --workspace \"$PWD\" || true",
        "agent:", f"  max_concurrent_agents: {profile.max_concurrent_agents}",
        "codex:", f"  command: {shell(runtime / 'launch_codex.sh')}",
        "  approval_policy: never", "---", "",
        "## Current Pilot dispatch", "",
        "Pilot owns task, lifecycle, working-round, planning-attempt, eligibility,",
        "grant, and transition authority. Runtime executes only the one dispatch",
        "whose exact grant it received and returns retained host evidence.", "",
        "- Task: {{ execution.task_id }}",
        "- Lifecycle: {{ execution.lifecycle_id }}",
        "- Working round: {{ execution.working_round_id }}",
        "- Planning attempt: {{ execution.planning_attempt_id }}",
        "- Expected role: {{ execution.role }}",
        "- Dispatch: {{ execution.dispatch_id }}",
        "- Starting HEAD: {{ execution.expected_starting_head }}",
        "- Capability grant: {{ execution.capability_grant }}", "",
        "The Runtime must not select another role, add filesystem scope, write Git",
        "metadata, create a commit, or assert lifecycle completion from model prose.", "",
    ]
    for role, filename in ROLE_FILES.items():
        role_path = install_root / "workflow" / "agents" / f"{filename}.toml"
        if not role_path.is_file():
            continue
        role_config = tomllib.loads(role_path.read_text(encoding="utf-8"))
        instructions = role_config.get("developer_instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError(f"role policy has no developer_instructions: {role_path}")
        lines += [f'{{% if execution.role == "{role}" %}}', instructions.strip(), "{% endif %}", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=pathlib.Path)
    parser.add_argument("--install-root", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    profile = load_profile(args.profile)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(profile, args.install_root), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
