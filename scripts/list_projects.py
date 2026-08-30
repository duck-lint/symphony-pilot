"""List the complete validated canonical project registry."""
from __future__ import annotations
import pathlib
import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from project_registry import suggest_dashboard_port, validate_registry
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--suggest-dashboard-port", action="store_true")
args = parser.parse_args()
profiles = validate_registry(ROOT / "projects")
if args.suggest_dashboard_port:
    print(f"suggested_dashboard_port\t{suggest_dashboard_port(ROOT / 'projects')}")
for profile in profiles:
    print(f"{profile.slug}\t{profile.repository}\t{profile.workspace_root}")
