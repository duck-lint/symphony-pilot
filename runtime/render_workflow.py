#!/usr/bin/env python3
"""Render an owned Symphony WORKFLOW.md from a validated project profile."""
from __future__ import annotations
import argparse
import pathlib
import shlex
import sys
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
        # One Runtime dispatch must correspond to one bounded Architect
        # attempt; SQLite reconciliation is the continuation mechanism.
        "  max_turns: 1",
        "codex:", f"  command: {shell(runtime / 'launch_codex.sh')}",
        "  approval_policy: never", "  thread_sandbox: danger-full-access",
        # Supervised-local mode deliberately permits the Architect to reach
        # Pilot's host-owned lifecycle inbox/outbox outside the checkout. The
        # launcher remains fail-closed unless the operator opts in explicitly.
        "  turn_sandbox_policy:", "    type: dangerFullAccess",
    ]
    lines += [
        "---", "", pathlib.Path(policy).read_text(encoding="utf-8").rstrip(), "",
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
    ]
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
