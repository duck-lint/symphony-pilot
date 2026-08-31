#!/usr/bin/env python3
"""Write the reviewed host runtime identity lock for one project."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from prepare_workspace import load_profile, require_physical_namespace  # noqa: E402
from runtime_lock import build_lock, discover  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--symphony", help="reviewed official Symphony executable")
    parser.add_argument("--codex", help="reviewed Codex executable")
    args = parser.parse_args()
    try:
        profile = load_profile(ROOT / "projects" / args.project / "profile.toml")
        symphony = args.symphony or discover("symphony")
        codex = args.codex or discover("codex")
        lock = build_lock(symphony, codex)
        path = require_physical_namespace(profile.state_root) / "runtime-lock.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        path.chmod(0o600)
        print(json.dumps({"project": profile.slug, "path": str(path), "lock": lock}, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"runtime pinning stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
