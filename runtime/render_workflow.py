#!/usr/bin/env python3
"""Render an owned Symphony WORKFLOW.md from a validated project profile."""
from __future__ import annotations
import argparse
import pathlib
import shlex
import sys
import tomllib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from prepare_workspace import Profile, control_database_path, load_profile

def render(profile: Profile, install_root: pathlib.Path, policy: pathlib.Path) -> str:
    runtime = install_root / "runtime"
    profile_path = install_root / "profile.toml"
    shell = lambda value: shlex.quote(str(value))
    database_path = control_database_path(profile)
    lines = [
        "---", "tracker:", "  kind: sqlite",
        f"  database_path: {shell(database_path)}",
        f"  project_slug: {profile.slug}",
        "  active_states:",
        "    - QUEUED", "    - PLANNED", "    - IMPLEMENTED", "    - REVIEW",
        "    - ADVERSARIAL_REVIEW",
        "  terminal_states:",
        "    - FINAL_MECHANICAL_ACCEPTANCE", "    - READY_FOR_HUMAN_MERGE",
        "polling:", f"  interval_ms: {profile.poll_interval_ms}",
        f"  max_retry_backoff_ms: {profile.max_retry_backoff_ms}",
        # Symphony still needs the host workspace allocator. The Codex task
        # domain is separately contained; its inner cwd is /workspace.
        "workspace:", f"  root: {profile.workspace_root}",
        "hooks:",
        "  after_create: |", f"    git clone --no-single-branch {shell(profile.git_remote)} .",
        "  before_run: |", "    set -eu",
        f"    exec python3 {shell(runtime / 'before_run.py')} --profile {shell(profile_path)} --workspace \"$PWD\"",
        "  after_run: |", "    set -eu",
        f"    exec python3 {shell(runtime / 'after_run.py')} --profile {shell(profile_path)} --workspace \"$PWD\"",
        "  before_remove: |",
        f"    python3 {shell(runtime / 'before_remove.py')} --profile {shell(profile_path)} --workspace \"$PWD\" || true",
        "agent:", f"  max_concurrent_agents: {profile.max_concurrent_agents}",
        "  max_turns: 1",
        "codex:", f"  command: {shell(runtime / 'launch_codex.sh')}",
        "  approval_policy: never", "  thread_sandbox: read-only",
        "  turn_sandbox_policy:", "    type: readOnly",
    ]
    policy_text = pathlib.Path(policy).read_text(encoding="utf-8").rstrip()
    policy_marker = "## Host result protocol"
    if policy_marker not in policy_text:
        raise ValueError(f"Architect policy is missing the shared result protocol marker: {policy}")
    architect_policy, shared_result_protocol = policy_text.split(policy_marker, 1)
    lines += [
        "---", "", '{% if execution.role == "ARCHITECT" %}',
        architect_policy.rstrip(), "{% endif %}", "",
        policy_marker, shared_result_protocol.lstrip(), "",
        "## Current local task",
        "",
        "The Runtime supplies the current Pilot SQLite task below. Treat this as the task work order; do not substitute tracker or GitHub state.",
        "",
        "- Identifier: {{ issue.identifier }}",
        "- Title: {{ issue.title }}",
        "- Lifecycle state: {{ issue.state }}",
        "- Objective:",
        "{{ issue.description }}",
        "",
        "## Selected role policy",
        "",
        "The Runtime selects one role for this fresh execution. The following",
        "policy projection is generated from the six deployed role TOMLs; it is",
        "reasoning policy only. Sandbox and writable-root authority comes from",
        "Runtime's App Server dispatch configuration.",
        "",
    ]
    role_files = {
        "PROJECT-MANAGER": "project-manager",
        "PLANNER": "planner",
        "IMPLEMENTER": "implementer",
        "REVIEWER": "reviewer",
        "ADVERSARY": "adversary",
        "ARCHIVIST": "archivist",
    }
    for role, filename in role_files.items():
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
    parser.add_argument("--policy", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    profile = load_profile(args.profile)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(profile, args.install_root, args.policy), encoding="utf-8")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
