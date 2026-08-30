"""List the complete validated canonical project registry."""
from __future__ import annotations
import pathlib
import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from project_registry import validate_registry
for profile in validate_registry(ROOT / "projects"):
    print(f"{profile.slug}\t{profile.repository}\t{profile.workspace_root}")
