#!/usr/bin/env python3
"""Provision one host secret without putting its value in shell history."""
from __future__ import annotations
import argparse
import getpass
import os
import pathlib
import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from prepare_workspace import load_profile, secret_path

parser = argparse.ArgumentParser()
parser.add_argument("profile", type=pathlib.Path)
args = parser.parse_args()
profile = load_profile(args.profile)
path = secret_path(profile)
path.parent.mkdir(parents=True, exist_ok=True)
os.chmod(path.parent, 0o700)
value = getpass.getpass(f"Enter tracker credential for {profile.slug} (hidden): ")
if not value or "\n" in value or "\r" in value:
    raise SystemExit("credential must be a non-empty single line")
temporary = path.with_name(path.name + ".new")
temporary.write_text(value + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
print(f"provisioned {path} with mode 0600")
