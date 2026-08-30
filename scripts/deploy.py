#!/usr/bin/env python3
"""Install a profile's generated Symphony runtime atomically."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from prepare_workspace import Profile, PreparationError, deployment_path, load_profile
from project_registry import resolve_project
from render_workflow import render

ROLE_POLICY_FILES = tuple(sorted((ROOT / "workflow" / "agents").glob("*.toml")))
EXPECTED_ROLE_NAMES = {"project-manager", "planner", "implementer", "reviewer", "adversary", "archivist"}

def file_digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def selected_deployment(profile: Profile):
    """Resolve the derived deployment namespace; profiles cannot override it."""
    return deployment_path(profile)

def deploy(profile_path: pathlib.Path, destination: pathlib.Path | None, dry_run: bool) -> pathlib.Path:
    profile = load_profile(profile_path)
    role_names = {path.stem for path in ROLE_POLICY_FILES}
    if role_names != EXPECTED_ROLE_NAMES:
        raise SystemExit("role policy pack must contain exactly the six generic roles")
    raw_target = destination or selected_deployment(profile)
    target = (raw_target if isinstance(raw_target, pathlib.PurePosixPath) and os.name == "nt"
              else pathlib.Path(raw_target).expanduser().resolve())
    if destination is None and os.name != "nt" and not str(target).startswith("/home/"):
        raise SystemExit("deployment root must remain on the WSL-native filesystem")
    source_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                                  capture_output=True, check=True).stdout.strip()
    source_status = subprocess.run(["git", "status", "--porcelain=v1"], cwd=ROOT,
                                   text=True, capture_output=True, check=True).stdout
    if source_status and not dry_run:
        raise SystemExit("deployment requires a clean source checkout; commit the reviewed changes first")
    if dry_run:
        print(json.dumps({"profile": profile.slug, "install_root": str(target),
                          "source_commit": source_commit,
                          "source_clean": not bool(source_status),
                          "role_policies": sorted(role_names),
                          "files": 9 + len(ROLE_POLICY_FILES)}, sort_keys=True))
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = pathlib.Path(tempfile.mkdtemp(prefix=f".{profile.slug}.stage-", dir=target.parent))
    try:
        (stage / "runtime").mkdir()
        (stage / "workflow").mkdir()
        (stage / "workflow" / "agents").mkdir()
        (stage / "projects" / profile.slug).mkdir(parents=True)
        # The official executable is shared host infrastructure.  It is
        # intentionally absent from generated project deployments.
        for name in ("prepare_workspace.py", "after_run.py", "before_remove.py",
                     "host_integration.py", "process_identity.py", "launch_codex.sh"):
            shutil.copy2(ROOT / "runtime" / name, stage / "runtime" / name)
        shutil.copy2(ROOT / "workflow" / "architect_policy.md", stage / "workflow/architect_policy.md")
        for policy in ROLE_POLICY_FILES:
            shutil.copy2(policy, stage / "workflow" / "agents" / policy.name)
        shutil.copy2(profile_path, stage / "profile.toml")
        (stage / "runtime/launch_codex.sh").chmod(0o755)
        workflow = stage / "projects" / profile.slug / "WORKFLOW.md"
        workflow.write_text(render(profile, target, stage / "workflow/architect_policy.md"), encoding="utf-8")
        manifest = {"schema": "symphony-pilot-deployment/v1", "profile": profile.slug,
                    "source_commit": source_commit,
                    "deployed_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "files": {str(path.relative_to(stage)): file_digest(path)
                              for path in stage.rglob("*") if path.is_file()}}
        (stage / "DEPLOYMENT.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                               encoding="utf-8")
        if target.exists():
            backup = target.with_name(target.name + ".previous." +
                                      dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
            os.replace(target, backup)
        os.replace(stage, target)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    print(json.dumps({"profile": profile.slug, "install_root": str(target),
                      "source_commit": source_commit}, sort_keys=True))
    return target

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, help="registered project slug")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if not args.project:
            raise PreparationError("project", "ordinary source deployment requires --project <registered-slug>")
        profile_path = ROOT / "projects" / args.project / "profile.toml"
        profile = resolve_project(args.project, ROOT / "projects")
        deploy_path = deploy(profile_path, None, args.dry_run)
    except PreparationError as exc:
        print(f"symphony-pilot deployment stopped: {exc.kind}: {exc}", file=sys.stderr)
        return 78
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
