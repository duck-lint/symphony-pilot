#!/usr/bin/env python3
"""Provision or explicitly adopt the host-only Ed25519 publication key."""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from prepare_workspace import PreparationError, publication_key_path  # noqa: E402
from publication_key import public_key_fingerprint, public_key_from_private  # noqa: E402
from project_registry import resolve_project  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, help="registered project slug")
    parser.add_argument("--adopt", action="store_true", help="explicitly verify and retain an existing key")
    args = parser.parse_args(argv)
    try:
        profile = resolve_project(args.project, ROOT / "projects")
        path = publication_key_path(profile)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        if path.exists() or path.is_symlink():
            if not args.adopt:
                raise PreparationError(
                    "publication_key",
                    "publication key already exists; use --adopt for explicit verification",
                )
            public = public_key_from_private(path)
            print(f"adopted publication key for {profile.slug}: {public}")
            print(f"fingerprint: {public_key_fingerprint(public)}")
            return 0
        executable = shutil.which("ssh-keygen")
        if not executable or not pathlib.Path(executable).is_absolute():
            raise PreparationError("publication_key", "host ssh-keygen is unavailable")
        with tempfile.TemporaryDirectory(prefix="symphony-pilot-key-", dir=path.parent) as directory:
            temporary = pathlib.Path(directory) / "publication-ssh-key"
            result = subprocess.run(
                [executable, "-q", "-t", "ed25519", "-N", "", "-f", str(temporary)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, check=False,
            )
            if result.returncode:
                raise PreparationError("publication_key", "host ssh-keygen could not create an Ed25519 key")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        public = public_key_from_private(path)
        print(f"provisioned publication key for {profile.slug}: {public}")
        print(f"fingerprint: {public_key_fingerprint(public)}")
        return 0
    except (PreparationError, OSError, ValueError, RuntimeError) as exc:
        print(f"symphony-pilot publication-key provisioning stopped: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
