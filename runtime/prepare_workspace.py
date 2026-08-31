#!/usr/bin/env python3
"""Prepare one disposable issue workspace before an architect attempt.

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
import hashlib
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

from task_admission import read_task, task_state_path


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


@dataclasses.dataclass
class IssueFacts:
    issue: int
    branch: str
    target_sha: str
    default_ref: str
    base_sha: str
    mode: str
    remote_sha: str | None
    pr_number: int | None
    comments: list[dict]


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
               "notification_backend", "dashboard_port"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise PreparationError("profile", "unsupported profile fields: " + ",".join(unknown))
    required = ["slug", "repository", "git_remote", "secret_reference", "trusted_dispatchers", "dispatch_labels", "blocked_label",
                "max_concurrent_agents", "max_turns", "dashboard_port",
                "poll_interval_ms", "max_retry_backoff_ms", "codex_model",
                "codex_reasoning_effort"]
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
        raise PreparationError("credential_missing", "project tracker credential is unavailable") from exc
    if not value or "\n" in value or "\r" in value:
        raise PreparationError("credential_invalid", "project tracker credential file is invalid")
    return value


def git(workspace: pathlib.Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=workspace, text=True, capture_output=True)
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


def admitted_task_facts(profile: Profile, workspace: pathlib.Path) -> tuple[IssueFacts, dict[str, object]]:
    match = re.fullmatch(r"GH-(\d+)", workspace.name)
    if not match:
        raise PreparationError("workspace_identity", "workspace name is not GH-N")
    issue = int(match.group(1))
    try:
        record = read_task(task_state_path(require_physical_namespace(profile.state_root), issue))
    except Exception as exc:
        raise PreparationError("task_admission", str(exc)) from exc
    if record["repository"] != profile.repository or record["project_slug"] != profile.slug:
        raise PreparationError("task_admission", "host task record does not belong to this project")
    branch = str(record["issue_branch"])
    base_sha = str(record["base_sha"])
    # The task-local origin and its refs are not authority. The host record is
    # the only continuation identity; prepare() fetches the exact recorded
    # commit from the profile remote below.
    remote_sha = record["published_head"]
    target_sha = remote_sha or base_sha
    facts = IssueFacts(issue, branch, target_sha, str(record["default_ref"]), base_sha,
                       "continuation" if remote_sha else "initial", remote_sha,
                       ((record["draft_pr"] or {}).get("number") if record["draft_pr"] else None), [])
    return facts, record


def verify_repository(profile: Profile, workspace: pathlib.Path) -> None:
    git_dir = pathlib.Path(git(workspace, "rev-parse", "--git-dir")).resolve()
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


def prepare_toolchain(profile: Profile, workspace: pathlib.Path, issue: int) -> dict:
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
        target = windows_temp.rstrip("\\/") + "\\symphony-pilot-cargo\\" + profile.slug + "\\GH-" + str(issue)
    env_file = workspace / ".git/symphony-toolchain.env"
    env_file.write_text("# generated by host preparation; do not commit\n" +
                        f'export PATH="{pathlib.PurePosixPath(found["cargo"]).parent}:$PATH"\n' +
                        (f'export CARGO_TARGET_DIR="{target}"\n' if target else ""), encoding="utf-8")
    return {"kind": "rust", "commands": found, "target_directory": target}


def marker(profile: Profile, workspace: pathlib.Path, facts: IssueFacts, tools: dict) -> None:
    data = {"schema": "symphony-pilot-preparation/v2", "profile": profile.slug,
            "repo": profile.repository, "issue": facts.issue, "task_branch": facts.branch,
            "task_head": facts.target_sha, "published_head": facts.remote_sha,
            "default_ref": facts.default_ref, "base_sha": facts.base_sha,
            "mode": facts.mode, "clean_status": True,
            "prepared_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "run_id": uuid.uuid4().hex, "toolchain": tools,
            "publication": "host-only"}
    path = workspace / ".git/symphony-preparation.json"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def blocker_fingerprint(profile: Profile, workspace: pathlib.Path, facts: IssueFacts,
                        kind: str, detail: str) -> str:
    status = git(workspace, "status", "--porcelain=v1", "--untracked-files=all", check=False)
    value = "\0".join((profile.slug, str(facts.issue), facts.branch, facts.target_sha, kind, detail, status))
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def record_blocker(profile: Profile, workspace: pathlib.Path, facts: IssueFacts,
                   kind: str, detail: str) -> None:
    digest = blocker_fingerprint(profile, workspace, facts, kind, detail)
    path = require_physical_namespace(profile.state_root) / "blockers" / f"GH-{facts.issue}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(path.read_text()) if path.exists() else {}
    repeated = old.get("fingerprint") == digest
    path.write_text(json.dumps({"schema": "symphony-pilot-blocker/v1",
                                "fingerprint": digest, "kind": kind,
                                "issue": facts.issue, "detail": detail,
                                "status": "active"}, indent=2) + "\n")
    if repeated:
        return
    # A blocker record is host audit state. Workpad discovery and mutation are
    # intentionally absent: only the comment id in task.json is authoritative.


def prepare(profile: Profile, workspace: pathlib.Path) -> None:
    workspace = require_physical_namespace(workspace.resolve())
    if not str(workspace).startswith(str(profile.workspace_root) + os.sep):
        raise PreparationError("workspace_path", "workspace is outside the profile workspace root")
    if not workspace.exists():
        raise PreparationError("workspace_missing", "host dispatch must clone before task preparation")
    try:
        verify_repository(profile, workspace)
    except PreparationError:
        raise PreparationError("repository_identity", "task workspace origin is not the registered repository")
    lock_path = require_physical_namespace(profile.state_root) / "locks" / f"{profile.slug}-GH-{workspace.name.removeprefix('GH-')}.lock"
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
            raise PreparationError("workspace_locked", "issue workspace is already in use") from exc
        facts = None
        try:
            if process_owns_workspace(workspace):
                raise PreparationError("workspace_in_use", "another process currently owns the issue workspace")
            facts, record = admitted_task_facts(profile, workspace)
            fetch_ref = facts.branch if facts.remote_sha else facts.default_ref
            fetch = subprocess.run(["git", "fetch", profile.git_remote, fetch_ref],
                                   cwd=workspace, text=True, capture_output=True)
            if fetch.returncode:
                raise PreparationError("git_fetch", "licensed starting ref fetch failed")
            fetched_sha = git(workspace, "rev-parse", "FETCH_HEAD")
            expected_sha = facts.target_sha if facts.remote_sha else facts.base_sha
            if fetched_sha != expected_sha:
                raise PreparationError("server_ref_changed",
                                       "server-fetched starting ref does not match host admission")
            if facts.base_sha and subprocess.run(
                    ["git", "merge-base", "--is-ancestor", facts.base_sha, facts.target_sha],
                    cwd=workspace, check=False).returncode:
                raise PreparationError("ancestry", "issue continuation is not based on the required base")
            status = git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
            if status:
                record_blocker(profile, workspace, facts, "dirty_task_workspace",
                               "task workspace is not a fresh clean checkout; no legacy recovery import is attempted")
                raise PreparationError("dirty_task_workspace", "task workspace must be clean for straight-cutover admission", persisted=True)
            git(workspace, "switch", "-C", facts.branch, facts.target_sha)
            if git(workspace, "status", "--porcelain"):
                raise PreparationError("clean_verification", "workspace remained dirty")
            tools = prepare_toolchain(profile, workspace, facts.issue)
            marker(profile, workspace, facts, tools)
            print(json.dumps({"profile": profile.slug, "issue": facts.issue, "branch": facts.branch,
                              "head": facts.target_sha, "mode": facts.mode, "marker": ".git/symphony-preparation.json"}, sort_keys=True))
        except PreparationError as exc:
            if facts is not None and not exc.persisted:
                try:
                    record_blocker(profile, workspace, facts, exc.kind, str(exc))
                except PreparationError:
                    pass
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
