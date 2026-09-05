"""Host-only publication-key identity and GitHub deploy-key verification."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator

from prepare_workspace import Profile, host_namespace_root, publication_key_path, require_physical_namespace
from workspace_boundary import atomic_metadata_write, physical_directory


class PublicationKeyError(RuntimeError):
    pass


BINDING_SCHEMA = "symphony-pilot-publication-key-binding/v1"


def binding_path(profile: Profile) -> pathlib.Path:
    return require_physical_namespace(
        host_namespace_root() / ".config/symphony-pilot/secrets" / profile.slug / "publication-key-binding.json"
    )


def _opened_key(path: pathlib.Path) -> bytes:
    path = pathlib.Path(path)
    physical_directory(path.parent)
    if path.is_symlink():
        raise PublicationKeyError("publication private key must not be a symlink")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                             getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    except OSError as exc:
        raise PublicationKeyError("publication private key cannot be opened safely") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (info.st_mode & 0o777) != 0o600:
            raise PublicationKeyError("publication private key must be a regular mode-0600 file")
        data = os.read(descriptor, 1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise PublicationKeyError("publication private key exceeds the host size bound")
        return data
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def stable_private_key(profile: Profile) -> Iterator[pathlib.Path]:
    """Copy one opened key so later pathname replacement cannot affect a run."""
    data = _opened_key(publication_key_path(profile))
    descriptor, name = tempfile.mkstemp(prefix="symphony-pilot-publication-key-")
    path = pathlib.Path(name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:  # pragma: no cover - native Windows fallback
            os.chmod(path, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        yield path
    finally:
        path.unlink(missing_ok=True)


def public_key_from_private(path: pathlib.Path) -> str:
    executable = shutil.which("ssh-keygen")
    if not executable or not pathlib.Path(executable).is_absolute():
        raise PublicationKeyError("host ssh-keygen is unavailable")
    result = subprocess.run(
        [executable, "-y", "-f", str(path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
        env={"PATH": str(pathlib.Path(executable).parent), "HOME": str(path.parent)},
    )
    if result.returncode or not result.stdout.strip():
        raise PublicationKeyError("publication private key is not a usable SSH key")
    fields = result.stdout.strip().split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise PublicationKeyError("publication key must be Ed25519")
    return " ".join(fields[:2])


def public_key_fingerprint(public_key: str) -> str:
    try:
        fields = public_key.split()
        if len(fields) < 2 or fields[0] != "ssh-ed25519":
            raise ValueError
        raw = base64.b64decode(fields[1], validate=True)
    except (ValueError, TypeError, base64.binascii.Error) as exc:
        raise PublicationKeyError("derived public key is malformed") from exc
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def _read_binding(path: pathlib.Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file() or (path.stat().st_mode & 0o777) != 0o600:
        raise PublicationKeyError("publication-key binding is unavailable or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PublicationKeyError("publication-key binding is malformed") from exc
    expected = {"schema", "project_slug", "repository", "github_deploy_key_id", "public_key_fingerprint"}
    if not isinstance(value, dict) or set(value) != expected or value.get("schema") != BINDING_SCHEMA:
        raise PublicationKeyError("publication-key binding fields are invalid")
    if (not isinstance(value["project_slug"], str) or not isinstance(value["repository"], str) or
            not isinstance(value["public_key_fingerprint"], str) or
            not isinstance(value["github_deploy_key_id"], int) or
            isinstance(value["github_deploy_key_id"], bool) or value["github_deploy_key_id"] < 1):
        raise PublicationKeyError("publication-key binding identity is invalid")
    return value


def read_binding(profile: Profile) -> dict[str, object]:
    value = _read_binding(binding_path(profile))
    if value["project_slug"] != profile.slug or value["repository"] != profile.repository:
        raise PublicationKeyError("publication-key binding does not belong to the registered project")
    return value


def _key_matches(server_value: object, derived: str) -> bool:
    return isinstance(server_value, str) and server_value.split()[:2] == derived.split()[:2]


def fetch_all_deploy_keys(fetch_page) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for page in range(1, 101):
        value = fetch_page(page)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise PublicationKeyError("GitHub deploy-key response is malformed")
        result.extend(value)
        if len(value) < 100:
            return result
    raise PublicationKeyError("GitHub deploy-key listing exceeded pagination bound")


def _verify_server_binding_with_key(profile: Profile, token: str, github_call,
                                    key: pathlib.Path) -> dict[str, object]:
    binding = read_binding(profile)
    derived = public_key_from_private(key)
    fingerprint = public_key_fingerprint(derived)
    if fingerprint != binding["public_key_fingerprint"]:
        raise PublicationKeyError("local publication key fingerprint differs from binding")
    keys = fetch_all_deploy_keys(lambda page: github_call(
        profile, token, "GET", f"/keys?per_page=100&page={page}"))
    matches = [item for item in keys if item.get("id") == binding["github_deploy_key_id"]]
    if len(matches) != 1:
        raise PublicationKeyError("bound GitHub deploy-key ID was not found exactly once")
    selected = matches[0]
    if (not _key_matches(selected.get("key"), derived) or
            selected.get("read_only") is not False or
            selected.get("enabled") is False):
        raise PublicationKeyError("GitHub deploy-key binding is not exact and write-enabled")
    return {"id": selected["id"], "fingerprint": fingerprint}


@contextlib.contextmanager
def verified_private_key(profile: Profile, token: str, github_call) -> Iterator[tuple[pathlib.Path, dict[str, object]]]:
    """Hold the one validated temporary key across proof and SSH publication."""
    with stable_private_key(profile) as key:
        yield key, _verify_server_binding_with_key(profile, token, github_call, key)


def verify_server_binding(profile: Profile, token: str, github_call) -> dict[str, object]:
    """Verify the exact writable repository key immediately before push."""
    with stable_private_key(profile) as key:
        return _verify_server_binding_with_key(profile, token, github_call, key)


def bind_server_key(profile: Profile, token: str, github_call) -> dict[str, object]:
    """Bind exactly one manually registered writable deploy key."""
    with stable_private_key(profile) as key:
        derived = public_key_from_private(key)
        fingerprint = public_key_fingerprint(derived)
    matches = [item for item in fetch_all_deploy_keys(lambda page: github_call(
        profile, token, "GET", f"/keys?per_page=100&page={page}")) if _key_matches(item.get("key"), derived)]
    if len(matches) != 1:
        raise PublicationKeyError("GitHub must contain exactly one matching publication deploy key")
    selected = matches[0]
    if (not isinstance(selected.get("id"), int) or isinstance(selected.get("id"), bool) or
            selected["id"] < 1 or selected.get("read_only") is not False or selected.get("enabled") is False):
        raise PublicationKeyError("matching GitHub deploy key is not writable and usable")
    value = {
        "schema": BINDING_SCHEMA,
        "project_slug": profile.slug,
        "repository": profile.repository,
        "github_deploy_key_id": selected["id"],
        "public_key_fingerprint": fingerprint,
    }
    path = binding_path(profile)
    physical_directory(path.parent.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    physical_directory(path.parent)
    atomic_metadata_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(path, 0o600)
    return value
