#!/usr/bin/env python3
"""Prepare one disposable local-task workspace before an architect attempt.

This module owns execution state only. Project semantics remain in the target
repository and are never inferred here.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
try:
    import fcntl
except ImportError:  # host-side validation may run under Windows Python
    fcntl = None
    import msvcrt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from workspace_boundary import (atomic_metadata_write,
                                physical_directory, run_git, validate_repository)
from storage import StoragePolicy, StorageContractError

class PreparationError(RuntimeError):
    def __init__(self, kind: str, message: str, persisted: bool = False):
        super().__init__(message)
        self.kind = kind
        self.persisted = persisted


@dataclasses.dataclass(frozen=True)
class Profile:
    slug: str
    repository: str
    git_remote: str
    workspace_root: pathlib.PurePath
    state_root: pathlib.PurePath
    log_root: pathlib.PurePath
    secret_reference: str
    trusted_dispatchers: tuple[str, ...]
    dispatch_labels: tuple[str, ...]
    blocked_label: str
    service_identity: str
    dashboard_port: int | None
    max_concurrent_agents: int
    max_turns: int
    poll_interval_ms: int
    max_retry_backoff_ms: int
    codex_model: str
    codex_reasoning_effort: str
    toolchain: str | None
    prevent_host_sleep: bool = False
    notifications_enabled: bool = False
    display_name: str = ""
    notification_backend: str = "windows-toast"
    source_profile_path: pathlib.Path | None = None
    storage_policy: StoragePolicy = dataclasses.field(default_factory=StoragePolicy)


@dataclasses.dataclass(frozen=True)
class LocalTaskFacts:
    task_uuid: str
    identifier: str
    branch: str
    selected_head: str
    base_ref: str
    base_sha: str
    mode: str
    current_head: str | None
    published_head: str | None


def configured_path(value: str) -> pathlib.Path:
    """Keep WSL paths textual when validation is invoked by Windows Python."""
    if os.name == "nt" and value.startswith("/"):
        return pathlib.PurePosixPath(value)  # type: ignore[return-value]
    return pathlib.Path(value).expanduser().resolve()


# Linux/WSL allows an unprivileged process to bind TCP ports from 1024 upward.
# The registry allocator owns uniqueness among Symphony projects; the runtime
# bind check separately reports conflicts with unrelated host processes.
DASHBOARD_PORT_MIN = 1024
DASHBOARD_PORT_MAX = 65535


def resolve_host_root() -> pathlib.Path:
    """Return the one physical namespace root for the installed control plane.

    Native Windows Python has no authority to resolve the WSL operator home.
    Refusing here is safer than turning the Windows account name into a
    fabricated ``/home/<name>`` path.
    """
    if os.name == "nt":
        raise PreparationError(
            "host_platform",
            "physical namespace operations must run under the WSL/Linux operator environment",
        )
    return pathlib.Path.home().resolve()


def host_namespace_root() -> pathlib.Path | pathlib.PurePosixPath:
    """Return a physical root on Linux or a non-physical marker on Windows."""
    if os.name == "nt":
        return pathlib.PurePosixPath("<wsl-home>")
    return resolve_host_root()


def require_physical_namespace(path: pathlib.PurePath) -> pathlib.Path:
    """Reject symbolic Windows paths before any host mutation or credential read."""
    if os.name == "nt" and isinstance(path, pathlib.PurePosixPath):
        raise PreparationError(
            "host_platform",
            "physical namespace operations must run under the WSL/Linux operator environment",
        )
    return pathlib.Path(path).resolve()


def state_namespace_for_slug(slug: str) -> pathlib.Path:
    """Resolve exactly one slug-owned state namespace for recovery control."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
        raise PreparationError("project", "project slug is not a safe identifier")
    root = host_namespace_root() / ".local" / "state" / "symphony-pilot" / slug
    return require_physical_namespace(root)


def project_namespaces(profile: Profile) -> dict[str, pathlib.PurePath]:
    """Return every project-owned host namespace used by the control plane."""
    home = host_namespace_root()
    data = home / ".local" / "share" / "symphony-pilot" / "deployments" / profile.slug
    state = home / ".local" / "state" / "symphony-pilot" / profile.slug
    workspace = home / "symphony-workspaces" / profile.slug
    return {
        "deployment": data,
        "workspace": workspace,
        "state": state,
        "logs": state / "logs",
        "process_state": state / "symphony.pid",
        "lock": state / "locks",
        "awake_guard": state / "symphony-awake.json",
        "workflow": data / "projects" / profile.slug / "WORKFLOW.md",
        "credentials": home / ".config" / "symphony-pilot" / "secrets" / profile.slug,
    }


def control_database_path(profile: Profile) -> pathlib.PurePath:
    """Return the one host-wide database shared by registered projects."""
    raw = str(profile.state_root)
    # Windows Python may represent a configured WSL path as ``\home\...``.
    # Preserve the deployment's Linux spelling instead of emitting a Windows
    # path into the Runtime workflow.
    if raw.startswith("\\") and not re.match(r"^[A-Za-z]:", raw):
        return pathlib.PurePosixPath(raw.replace("\\", "/")).parent / "control.sqlite3"
    return pathlib.PurePath(profile.state_root).parent / "control.sqlite3"


def deployment_path(profile: Profile) -> pathlib.Path | pathlib.PurePosixPath:
    return project_namespaces(profile)["deployment"]


def load_profile(path: pathlib.Path) -> Profile:
    import tomllib

    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    allowed = {"slug", "repository", "git_remote", "secret_reference", "trusted_dispatchers", "dispatch_labels",
               "blocked_label", "max_concurrent_agents", "max_turns", "poll_interval_ms",
               "max_retry_backoff_ms", "codex_model", "codex_reasoning_effort", "toolchain",
               "prevent_host_sleep", "notifications_enabled", "display_name",
               "notification_backend", "dashboard_port", "storage_pool_bytes",
               "storage_allocatable_pool_bytes",
               "task_storage_bytes", "task_storage_inodes", "storage_emergency_reserve_bytes",
               "storage_emergency_reserve_inodes"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise PreparationError("profile", "unsupported profile fields: " + ",".join(unknown))
    required = ["slug", "repository", "git_remote", "secret_reference", "trusted_dispatchers", "dispatch_labels", "blocked_label",
                "max_concurrent_agents", "max_turns", "dashboard_port",
                "poll_interval_ms", "max_retry_backoff_ms", "codex_model",
                "codex_reasoning_effort", "storage_pool_bytes", "storage_allocatable_pool_bytes",
                "task_storage_bytes",
                "task_storage_inodes", "storage_emergency_reserve_bytes",
                "storage_emergency_reserve_inodes"]
    missing = [key for key in required if key not in raw]
    if missing:
        raise PreparationError("profile", "missing profile fields: " + ",".join(missing))
    slug = str(raw["slug"])
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
        raise PreparationError("profile", "profile slug is not a safe identifier")
    for key in ("repository", "git_remote", "secret_reference", "blocked_label",
                "display_name", "notification_backend"):
        if any(character in str(raw.get(key, "")) for character in ("\n", "\r", "\0")):
            raise PreparationError("profile", f"profile field {key} contains control characters")
    if re.search(r"://[^/\s]+@|(?:token|password|secret|private[_-]?key)\s*[:=]", str(raw["git_remote"]), re.I):
        raise PreparationError("profile", "git_remote must not contain embedded credentials")
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", str(raw["repository"])):
        raise PreparationError("profile", "repository must be an owner/name pair")
    forbidden_keys = {"token", "password", "credential", "secret", "pat", "api_key"}
    suspicious = [key for key in raw if key.lower() in forbidden_keys]
    if suspicious:
        raise PreparationError("profile", "profiles may contain only secret_reference, not credential values")
    if int(raw["max_concurrent_agents"]) != 1:
        raise PreparationError("profile", "the pilot permits exactly one concurrent agent")
    if not raw["dispatch_labels"]:
        raise PreparationError("profile", "at least one dispatch label is required")
    if not isinstance(raw["trusted_dispatchers"], list):
        raise PreparationError("profile", "trusted_dispatchers must be a list")
    if not isinstance(raw["dispatch_labels"], list):
        raise PreparationError("profile", "dispatch_labels must be a list")
    dispatchers = tuple(str(actor) for actor in raw["trusted_dispatchers"])
    if not dispatchers or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", actor) for actor in dispatchers):
        raise PreparationError("profile", "trusted_dispatchers must contain valid non-empty GitHub logins")
    if len(set(dispatchers)) != len(dispatchers):
        raise PreparationError("profile", "trusted_dispatchers must not contain duplicates")
    dashboard_port = int(raw["dashboard_port"])
    if not DASHBOARD_PORT_MIN <= dashboard_port <= DASHBOARD_PORT_MAX:
        raise PreparationError(
            "profile",
            f"dashboard_port must be between {DASHBOARD_PORT_MIN} and {DASHBOARD_PORT_MAX}",
        )
    if not isinstance(raw.get("prevent_host_sleep", False), bool):
        raise PreparationError("profile", "prevent_host_sleep must be boolean")
    if not isinstance(raw.get("notifications_enabled", False), bool):
        raise PreparationError("profile", "notifications_enabled must be boolean")
    try:
        storage_policy = StoragePolicy(
            pool_bytes=raw["storage_pool_bytes"],
            allocatable_pool_bytes=raw["storage_allocatable_pool_bytes"],
            task_bytes=raw["task_storage_bytes"],
            task_inodes=raw["task_storage_inodes"],
            emergency_reserve_bytes=raw["storage_emergency_reserve_bytes"],
            emergency_reserve_inodes=raw["storage_emergency_reserve_inodes"],
        ).validate()
    except (TypeError, ValueError, StorageContractError) as exc:
        raise PreparationError("storage_policy", "profile storage policy is invalid") from exc
    profile = Profile(
        slug=slug,
        repository=str(raw["repository"]),
        git_remote=str(raw["git_remote"]),
        workspace_root=pathlib.PurePosixPath(),
        state_root=pathlib.PurePosixPath(),
        log_root=pathlib.PurePosixPath(),
        secret_reference=str(raw["secret_reference"]),
        trusted_dispatchers=dispatchers,
        dispatch_labels=tuple(str(label) for label in raw["dispatch_labels"]),
        blocked_label=str(raw["blocked_label"]),
        service_identity=f"symphony-pilot-{slug}",
        dashboard_port=dashboard_port,
        max_concurrent_agents=int(raw["max_concurrent_agents"]),
        max_turns=int(raw["max_turns"]),
        poll_interval_ms=int(raw["poll_interval_ms"]),
        max_retry_backoff_ms=int(raw["max_retry_backoff_ms"]),
        codex_model=str(raw["codex_model"]),
        codex_reasoning_effort=str(raw["codex_reasoning_effort"]),
        toolchain=str(raw["toolchain"]) if raw.get("toolchain") else None,
        prevent_host_sleep=bool(raw.get("prevent_host_sleep", False)),
        notifications_enabled=bool(raw.get("notifications_enabled", False)),
        display_name=str(raw.get("display_name", slug)),
        notification_backend=str(raw.get("notification_backend", "windows-toast")),
        source_profile_path=path.resolve(),
        storage_policy=storage_policy,
    )
    namespaces = project_namespaces(profile)
    return dataclasses.replace(
        profile,
        workspace_root=(namespaces["workspace"] if os.name == "nt"
                        else pathlib.Path(namespaces["workspace"]).resolve()),
        state_root=(namespaces["state"] if os.name == "nt"
                    else pathlib.Path(namespaces["state"]).resolve()),
        log_root=(namespaces["logs"] if os.name == "nt"
                  else pathlib.Path(namespaces["logs"]).resolve()),
    )


def secret_path(profile: Profile) -> pathlib.PurePath:
    reference = pathlib.Path(profile.secret_reference)
    if reference.is_absolute() or ".." in reference.parts:
        raise PreparationError("secret_reference", "secret reference escapes its project boundary")
    return host_namespace_root() / ".config/symphony-pilot/secrets" / profile.slug / reference


def publication_key_path(profile: Profile) -> pathlib.Path:
    """Return the one deterministic host publication-key location."""
    return require_physical_namespace(
        host_namespace_root() / ".config/symphony-pilot/secrets" / profile.slug / "publication-ssh-key"
    )


def read_secret(profile: Profile) -> str:
    path = require_physical_namespace(secret_path(profile))
    try:
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            raise PreparationError("secret_permissions", "project secret file must have mode 0600")
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise PreparationError("credential_missing", "project GitHub API credential is unavailable") from exc
    if not value or "\n" in value or "\r" in value:
        raise PreparationError("credential_invalid", "project GitHub API credential file is invalid")
    return value


def git(workspace: pathlib.Path, *args: str, check: bool = True) -> str:
    result = run_git(workspace, *args)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        raise PreparationError("git_failure", f"git {' '.join(args[:3])}: {detail[:300]}")
    return result.stdout.strip()


def github(profile: Profile, token: str, method: str, path: str, body: object | None = None) -> object:
    url = "https://api.github.com/repos/" + profile.repository + path
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json",
                 "User-Agent": "symphony-pilot-host"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        safe = re.sub(r"token|secret|authorization", "[redacted]",
                      exc.read(512).decode("utf-8", "replace"), flags=re.I)
        raise PreparationError("github_http", f"GitHub {method} {path} returned HTTP {exc.code}: {safe[:240]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PreparationError("github_transport", f"GitHub transport failure: {exc}") from exc


def local_task_facts(profile: Profile, workspace: pathlib.Path) -> tuple[LocalTaskFacts, dict[str, object]]:
    match = re.fullmatch(r"T-(\d{6})", workspace.name)
    if not match:
        raise PreparationError("workspace_identity", "workspace name is not T-N")
    try:
        from control_db import ControlPlaneDatabase

        with ControlPlaneDatabase.open_readonly(control_database_path(profile)) as database:
            record = database.read_task_by_identifier(match.group(0), project_slug=profile.slug)
    except Exception as exc:
        raise PreparationError("task_lookup", str(exc)) from exc
    branch = str(record["branch"])
    base_sha = str(record["base_sha"])
    current_head = record["current_head"]
    published_head = record["published_head"]
    # Step 6 continuation authority is the host-accepted current HEAD plus
    # the retained physical workspace. A published HEAD belongs to the later
    # publication contract and must not turn an initial preparation into an
    # implicit remote continuation.
    selected_head = current_head or base_sha
    facts = LocalTaskFacts(
        task_uuid=str(record["id"]), identifier=str(record["identifier"]), branch=branch,
        selected_head=str(selected_head), base_ref=str(record["base_ref"]), base_sha=base_sha,
        mode="continuation" if current_head else "initial",
        current_head=str(current_head) if current_head else None,
        published_head=str(published_head) if published_head else None,
    )
    return facts, record


def verify_repository(profile: Profile, workspace: pathlib.Path) -> None:
    validate_repository(workspace)
    if git(workspace, "remote", "get-url", "origin") != profile.git_remote:
        raise PreparationError("repository_identity", "workspace remote differs from registered remote")
    git_dir = pathlib.Path(git(workspace, "rev-parse", "--git-dir"))
    git_dir = (workspace / git_dir).resolve() if not git_dir.is_absolute() else git_dir.resolve()
    try:
        git_dir.relative_to(workspace / ".git")
    except ValueError as exc:
        raise PreparationError("repository_identity", "task Git directory escapes the task workspace") from exc


def process_owns_workspace(workspace: pathlib.Path) -> bool:
    ancestors = {os.getpid()}
    parent = os.getppid()
    while parent > 1:
        ancestors.add(parent)
        try:
            parent = int(pathlib.Path(f"/proc/{parent}/stat").read_text().split()[3])
        except (FileNotFoundError, ValueError, IndexError):
            break
    wanted = str(workspace.resolve())
    for entry in pathlib.Path("/proc").glob("[0-9]*"):
        try:
            if int(entry.name) in ancestors:
                continue
            if str((entry / "cwd").resolve()) == wanted:
                return True
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return False


def find_tool(name: str) -> str | None:
    value = shutil.which(name)
    if value:
        return value
    result = subprocess.run(["cmd.exe", "/c", "where", name + ".exe"], text=True, capture_output=True)
    if result.returncode and not result.stdout.strip():
        return None
    path = result.stdout.splitlines()[0].strip()
    return "/mnt/" + path[0].lower() + "/" + path[3:].replace("\\", "/") if re.match(r"^[A-Za-z]:\\", path) else path


def prepare_toolchain(profile: Profile, workspace: pathlib.Path, identifier: str) -> dict:
    if profile.toolchain != "rust":
        return {"kind": profile.toolchain, "commands": {}, "target_directory": None}
    found = {name: find_tool(name) for name in ("cargo", "rustc", "rustfmt", "rustdoc")}
    missing = [name for name, path in found.items() if not path]
    if missing:
        raise PreparationError("toolchain_missing", "required tools unavailable: " + ",".join(missing))
    result = subprocess.run(["cmd.exe", "/c", "echo", "%TEMP%"], text=True, capture_output=True)
    windows_temp = result.stdout.strip()
    target = None
    if re.match(r"^[A-Za-z]:\\", windows_temp):
        target = windows_temp.rstrip("\\/") + "\\symphony-pilot-cargo\\" + profile.slug + "\\" + identifier
    env_file = workspace / ".git/symphony-toolchain.env"
    atomic_metadata_write(env_file, "# generated by host preparation; do not commit\n" +
                        f'export PATH="{pathlib.PurePosixPath(found["cargo"]).parent}:$PATH"\n' +
                        (f'export CARGO_TARGET_DIR="{target}"\n' if target else ""))
    return {"kind": "rust", "commands": found, "target_directory": target}


def marker(profile: Profile, workspace: pathlib.Path, facts: LocalTaskFacts, tools: dict) -> None:
    data = {"schema": "symphony-pilot-preparation/v3", "project": profile.slug,
            "task_uuid": facts.task_uuid, "identifier": facts.identifier,
            "task_branch": facts.branch, "selected_head": facts.selected_head,
            "current_head": facts.current_head, "published_head": facts.published_head,
            "base_ref": facts.base_ref, "base_sha": facts.base_sha,
            "mode": facts.mode, "clean_status": True,
            "prepared_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "run_id": uuid.uuid4().hex, "toolchain": tools,
            "publication": "host-only"}
    path = workspace / ".git/symphony-preparation.json"
    atomic_metadata_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def record_blocker(profile: Profile, workspace: pathlib.Path, facts: LocalTaskFacts,
                   kind: str, detail: str) -> None:
    # Blockers are now rows in the same host authority the Runtime adapter
    # reads. A repeated preparation failure is intentionally not deduplicated
    # here; Step 6 owns richer lifecycle reconciliation.
    from control_db import ControlPlaneDatabase

    with ControlPlaneDatabase.open(control_database_path(profile)) as database:
        database.record_blocker(task_id=facts.task_uuid, kind=kind if kind in {"human", "project", "infrastructure"} else "infrastructure",
                                body=f"{kind}: {detail}")


def step7_publication_applies(profile: Profile, task_uuid: str) -> bool:
    """Detect a publication row without assigning it a Step-6 meaning."""
    from control_db import ControlPlaneDatabase

    with ControlPlaneDatabase.open_readonly(control_database_path(profile)) as database:
        publication = database.read_publication(task_uuid)
    return bool(publication and publication["publication_status"] != "not_started")


def prepare(profile: Profile, workspace: pathlib.Path) -> None:
    # SQLite identity is recovered from the original basename before any
    # filesystem/Git inspection. Never derive compensation identity from Git.
    facts, _ = local_task_facts(profile, workspace)
    try:
        _prepare(profile, workspace, facts)
    except Exception as exc:
        if not isinstance(exc, PreparationError) or not exc.persisted:
            record_blocker(profile, workspace, facts, "infrastructure",
                           f"preparation failed: {type(exc).__name__}")
        if isinstance(exc, PreparationError):
            raise
        raise PreparationError("workspace_boundary", "trusted workspace preparation failed", persisted=True) from exc


def _prepare(profile: Profile, workspace: pathlib.Path, facts: LocalTaskFacts) -> None:
    workspace = physical_directory(workspace)
    if not str(workspace).startswith(str(profile.workspace_root) + os.sep):
        raise PreparationError("workspace_path", "workspace is outside the profile workspace root")
    if not workspace.exists():
        raise PreparationError("workspace_missing", "host dispatch must clone before task preparation")
    try:
        verify_repository(profile, workspace)
    except PreparationError:
        raise PreparationError("repository_identity", "task workspace origin is not the registered repository")
    lock_path = require_physical_namespace(profile.state_root) / "locks" / f"{profile.slug}-{workspace.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                lock.write("0")
                lock.flush()
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, OSError) as exc:
            raise PreparationError("workspace_locked", "task workspace is already in use") from exc
        try:
            if process_owns_workspace(workspace):
                raise PreparationError("workspace_in_use", "another process currently owns the task workspace")
            if facts.current_head is not None:
                # An accepted implementation HEAD is deliberately unpublished
                # until Step 7. The retained local checkout is therefore the
                # only continuation source. Every comparison below is
                # read-only: a mismatch is evidence of divergence, never a
                # reason to reset the checkout or rewrite SQLite.
                recorded_head = facts.current_head
                if run_git(workspace, "cat-file", "-e", recorded_head + "^{commit}").returncode:
                    raise PreparationError("continuation_head", "SQLite current_head is not a local commit")
                if git(workspace, "branch", "--show-current") != facts.branch:
                    record_blocker(profile, workspace, facts, "continuation_divergence",
                                   "retained workspace is not on the host-owned task branch")
                    raise PreparationError(
                        "continuation_branch",
                        "retained workspace branch diverges from the host-owned task branch",
                        persisted=True,
                    )
                local_head = git(workspace, "rev-parse", "HEAD")
                if local_head != recorded_head:
                    record_blocker(profile, workspace, facts, "continuation_divergence",
                                   f"retained local HEAD {local_head} differs from SQLite current_head {recorded_head}")
                    raise PreparationError(
                        "continuation_head",
                        "retained local HEAD diverges from SQLite current_head; explicit recovery is required",
                        persisted=True,
                    )
                status = git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
                if status:
                    record_blocker(profile, workspace, facts, "dirty_task_workspace",
                                   "unpublished continuation workspace is not clean")
                    raise PreparationError("dirty_task_workspace", "unpublished continuation workspace must be clean", persisted=True)
                if run_git(
                        workspace, "merge-base", "--is-ancestor", facts.base_sha, recorded_head,
                ).returncode:
                    record_blocker(profile, workspace, facts, "continuation_divergence",
                                   "SQLite current_head is not based on the recorded task base")
                    raise PreparationError(
                        "ancestry",
                        "SQLite current_head is not based on the required base",
                        persisted=True,
                    )
            else:
                if facts.published_head is not None or step7_publication_applies(profile, facts.task_uuid):
                    # Step 7 owns any published-active continuation contract.
                    # Step 6 has no fallback or implicit interpretation for it.
                    raise PreparationError(
                        "publication_state",
                        "published task state requires the Step-7 preparation contract",
                    )
                fetch = run_git(workspace, "fetch", "--", profile.git_remote, facts.base_ref, transport=True)
                if fetch.returncode:
                    raise PreparationError("git_fetch", "licensed starting ref fetch failed")
                fetched_sha = git(workspace, "rev-parse", "FETCH_HEAD")
                # The recorded base remains the task's starting identity. A
                # normal default-branch fast-forward must not move it.
                if run_git(
                        workspace, "cat-file", "-e", facts.base_sha + "^{commit}",
                ).returncode:
                    raise PreparationError("base_history_rewritten", "recorded task base commit is unavailable")
                if run_git(
                        workspace, "merge-base", "--is-ancestor", facts.base_sha, fetched_sha,
                ).returncode:
                    raise PreparationError(
                        "base_history_rewritten",
                        "recorded task base is not an ancestor of the registered base ref",
                    )
                status = git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
                if status:
                    record_blocker(profile, workspace, facts, "dirty_task_workspace",
                                   "task workspace is not a fresh clean checkout; no legacy recovery import is attempted")
                    raise PreparationError("dirty_task_workspace", "task workspace must be clean for straight-cutover admission", persisted=True)
                git(workspace, "switch", "-C", facts.branch, facts.base_sha)
            if git(workspace, "status", "--porcelain"):
                raise PreparationError("clean_verification", "workspace remained dirty")
            tools = prepare_toolchain(profile, workspace, facts.identifier)
            marker(profile, workspace, facts, tools)
            print(json.dumps({"project": profile.slug, "identifier": facts.identifier,
                              "task_uuid": facts.task_uuid, "branch": facts.branch,
                              "head": facts.selected_head, "mode": facts.mode,
                              "marker": ".git/symphony-preparation.json"}, sort_keys=True))
        except PreparationError:
            raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=pathlib.Path)
    parser.add_argument("--workspace", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        profile = load_profile(args.profile)
        prepare(profile, args.workspace)
        return 0
    except PreparationError as exc:
        print(f"symphony-pilot preparation stopped: {exc.kind}: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
