"""List non-secret project profiles."""
from __future__ import annotations
import pathlib
import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from prepare_workspace import load_profile
for path in sorted((ROOT / "projects").glob("*/profile.toml")):
    profile = load_profile(path)
    print(f"{profile.slug}\t{profile.repository}\t{profile.workspace_root}")
