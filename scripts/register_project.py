"""Validate a profile before deployment; no secret is read."""
from __future__ import annotations
import argparse
import pathlib
import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from prepare_workspace import load_profile
parser = argparse.ArgumentParser()
parser.add_argument("profile", type=pathlib.Path)
args = parser.parse_args()
profile = load_profile(args.profile)
print(f"registered profile candidate: {profile.slug} ({profile.repository})")
print("Provision its 0600 host secret separately; no credential was read.")
