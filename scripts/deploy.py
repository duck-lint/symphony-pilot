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
from prepare_workspace import load_profile
from render_workflow import render

def file_digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def deployment_root(profile):
    return profile.deployment_root or pathlib.Path.home() / ".local/share/symphony-pilot/deployments" / profile.slug

def deploy(profile_path: pathlib.Path, install_root: pathlib.Path | None, dry_run: bool) -> pathlib.Path:
    profile = load_profile(profile_path)
    raw_target = install_root or deployment_root(profile)
    target = (raw_target if isinstance(raw_target, pathlib.PurePosixPath) and os.name == "nt"
              else pathlib.Path(raw_target).expanduser().resolve())
    if not str(target).startswith("/home/"):
        raise SystemExit("deployment root must remain on the WSL-native filesystem")
    source_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                                  capture_output=True, check=True).stdout.strip()
    if dry_run:
        print(json.dumps({"profile": profile.slug, "install_root": str(target),
                          "source_commit": source_commit, "files": 8}, sort_keys=True))
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = pathlib.Path(tempfile.mkdtemp(prefix=f".{profile.slug}.stage-", dir=target.parent))
    try:
        (stage / "runtime").mkdir()
        (stage / "workflow").mkdir()
        (stage / "projects" / profile.slug).mkdir(parents=True)
        # Keep the separately installed official Symphony executable when the
        # profile intentionally deploys into its existing runtime directory.
        if (target / "bin").is_dir():
            (stage / "bin").mkdir()
            for asset in (target / "bin").iterdir():
                if asset.is_file() and (asset.name.startswith("symphony-") or asset.name.endswith(".sha256")):
                    shutil.copy2(asset, stage / "bin" / asset.name)
        for name in ("prepare_workspace.py", "after_run.py", "before_remove.py", "launch_codex.sh"):
            shutil.copy2(ROOT / "runtime" / name, stage / "runtime" / name)
        shutil.copy2(ROOT / "workflow" / "architect_policy.md", stage / "workflow/architect_policy.md")
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
    parser.add_argument("--profile", default=str(ROOT / "projects/cleanroom/profile.toml"))
    parser.add_argument("--install-root", type=pathlib.Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    deploy(pathlib.Path(args.profile), args.install_root, args.dry_run)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
