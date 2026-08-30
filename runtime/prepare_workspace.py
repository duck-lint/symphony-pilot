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
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

from process_identity import matches as process_identity_matches


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
    required_ref: str | None
    required_sha: str | None
    base_ref: str | None
    base_sha: str | None
    mode: str
    remote_sha: str | None
    pr_number: int | None
    comments: list[dict]


def configured_path(value: str) -> pathlib.Path:
    """Keep WSL paths textual when validation is invoked by Windows Python."""
    if os.name == "nt" and value.startswith("/"):
        return pathlib.PurePosixPath(value)  # type: ignore[return-value]
    return pathlib.Path(value).expanduser().resolve()


DASHBOARD_PORT_MIN = 4040
DASHBOARD_PORT_MAX = 4999


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
    allowed = {"slug", "repository", "git_remote", "secret_reference", "dispatch_labels",
               "blocked_label", "max_concurrent_agents", "max_turns", "poll_interval_ms",
               "max_retry_backoff_ms", "codex_model", "codex_reasoning_effort", "toolchain",
               "prevent_host_sleep", "notifications_enabled", "display_name",
               "notification_backend", "dashboard_port"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise PreparationError("profile", "unsupported profile fields: " + ",".join(unknown))
    required = ["slug", "repository", "git_remote", "secret_reference", "dispatch_labels", "blocked_label",
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


def github_all(profile: Profile, token: str, path: str, page_size: int = 100) -> list[dict]:
    """Fetch a complete paginated collection before applying cardinality rules."""
    result: list[dict] = []
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        batch = github(profile, token, "GET",
                       f"{path}{separator}per_page={page_size}&page={page}")
        if not isinstance(batch, list):
            raise PreparationError("github_response", f"GitHub collection was not a list: {path}")
        result.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < page_size:
            return result
        page += 1
        if page > 1000:
            raise PreparationError("github_pagination", f"GitHub collection exceeded pagination bound: {path}")


def workpad_candidates(items: list[dict], marker: str = "<!-- symphony-workpad:v1 -->") -> list[dict]:
    return [item for item in items if marker in (item.get("body") or "")]


def workpad(items: list[dict], marker: str = "<!-- symphony-workpad:v1 -->") -> dict | None:
    """Return the sole workpad; ambiguity must never select an arbitrary one."""
    pads = workpad_candidates(items, marker)
    return pads[0] if len(pads) == 1 else None


def comments(profile: Profile, token: str, issue: int) -> list[dict]:
    return github_all(profile, token, f"/issues/{issue}/comments")


def parse_sha(text: str) -> str | None:
    match = re.search(r"\b([0-9a-fA-F]{40})\b", text)
    return match.group(1).lower() if match else None


def parse_ref(body: str, pattern: str) -> str | None:
    match = re.search(pattern, body, re.I)
    return match.group(1).strip("`.,)") if match else None


def issue_facts(profile: Profile, workspace: pathlib.Path, token: str) -> IssueFacts:
    match = re.fullmatch(r"GH-(\d+)", workspace.name)
    if not match:
        raise PreparationError("workspace_identity", "workspace name is not GH-N")
    issue = int(match.group(1))
    issue_json = github(profile, token, "GET", f"/issues/{issue}")
    issue_comments = comments(profile, token, issue)
    body = issue_json.get("body") or ""
    pads = workpad_candidates(issue_comments)
    if len(pads) > 1:
        raise PreparationError("ambiguous_workpad", "issue has more than one symphony-workpad:v1 comment")
    pad_body = (pads[0] if pads else {}).get("body") or ""
    required_match = re.search(r"required starting commit\s*:\s*`?([0-9a-fA-F]{40})", body, re.I)
    required_sha = required_match.group(1).lower() if required_match else parse_sha(body)
    required_ref = parse_ref(body, r"required starting ref\s*:\s*`?([A-Za-z0-9._/-]+)")
    if not required_ref:
        required_ref = parse_ref(body, r"Work from:\s*[\r\n]+`?(origin/[A-Za-z0-9._/-]+)")
    base_sha_match = re.search(r"(?:baseline SHA|base commit)\s*:\s*`?([0-9a-fA-F]{40})", body, re.I)
    base_sha = base_sha_match.group(1).lower() if base_sha_match else required_sha
    base_ref = parse_ref(body, r"(?:PR base|base ref)\s*:\s*`?([A-Za-z0-9._/-]+)")
    if not base_ref:
        base_ref = parse_ref(body, r"(?:PR base|base ref):\s*[\r\n]+`?([A-Za-z0-9._/-]+)") or required_ref
    pull_requests = github_all(profile, token, "/pulls?state=open")
    matching = [pr for pr in pull_requests if
                (pr.get("head") or {}).get("repo", {}).get("full_name") == profile.repository and
                (pr.get("head") or {}).get("ref", "").startswith(f"codex/gh-{issue}-")]
    if len(matching) > 1:
        raise PreparationError("ambiguous_issue_pr", "issue has more than one matching published issue PR")
    pr = matching[0] if matching else None
    if pr and not pr.get("draft", False):
        raise PreparationError("non_draft_issue_pr", "matching issue PR is not a draft pull request")
    branch = (pr or {}).get("head", {}).get("ref")
    if not branch:
        branch = parse_ref(body + "\n" + pad_body,
                           r"(?:issue branch|published remote branch|remote issue branch)\s*:\s*`?([A-Za-z0-9._/-]+)")
    branch = (branch or f"codex/gh-{issue}-work").removeprefix("origin/")
    remote_line = git(workspace, "ls-remote", "origin", f"refs/heads/{branch}", check=False)
    remote_sha = remote_line.split()[0].lower() if remote_line else None
    target_sha = remote_sha or required_sha
    if not target_sha:
        raise PreparationError("starting_state", "issue has no licensed starting commit or published issue branch")
    return IssueFacts(issue, branch, target_sha, required_ref, required_sha, base_ref, base_sha,
                      "continuation" if remote_sha else "initial", remote_sha,
                      pr.get("number") if pr else None, issue_comments)


def verify_repository(profile: Profile, workspace: pathlib.Path) -> None:
    origin = git(workspace, "remote", "get-url", "origin")
    normalized = re.sub(r"\.git$", "", origin).replace(":", "/")
    if not normalized.endswith(profile.repository):
        raise PreparationError("repository_identity", "workspace origin is not the profile repository")


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


ROLE_HOME_SCHEMA = "symphony-pilot-role-home/v2"
ROLE_HOME_MARKER = pathlib.PurePosixPath(".git/symphony-role-home.json")
ROLE_HOME_ROOT = pathlib.Path("/tmp")
ROLE_HOME_PREFIX = "symphony-pilot-codex-home."
ROLE_HOME_LEASE_FILE = ".symphony-pilot-role-home.json"


def _role_home_path(value: object) -> pathlib.Path:
    if not isinstance(value, str):
        raise PreparationError("role_home_recovery", "role-home marker path is invalid")
    path = pathlib.Path(value).resolve()
    if path.parent != ROLE_HOME_ROOT.resolve() or not path.name.startswith(ROLE_HOME_PREFIX):
        raise PreparationError("role_home_recovery", "role-home marker escapes the pilot staging directory")
    return path


def reconcile_role_home(workspace: pathlib.Path) -> bool:
    """Remove the exact external role home left by a completed or crashed run.

    The marker is host-owned state under .git, not a target-project file. Its
    path is accepted only when it is the launcher-created /tmp prefix, so a
    malformed marker fails closed instead of becoming a deletion primitive.
    """
    marker = workspace / pathlib.Path(ROLE_HOME_MARKER)
    if not marker.exists():
        return False
    if process_owns_workspace(workspace):
        raise PreparationError("workspace_in_use", "cannot reconcile a role home while its workspace is active")
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreparationError("role_home_recovery", "role-home marker cannot be read") from exc
    if not isinstance(data, dict) or data.get("schema") != ROLE_HOME_SCHEMA:
        raise PreparationError("role_home_recovery", "role-home marker schema is invalid")
    if data.get("workspace") != str(workspace.resolve()):
        raise PreparationError("role_home_recovery", "role-home marker does not belong to this workspace")
    lease_id = data.get("lease_id")
    owner = data.get("owner")
    if (not isinstance(lease_id, str) or not re.fullmatch(r"symphony-pilot-codex-home\.[A-Za-z0-9_]+", lease_id) or
            not isinstance(owner, dict) or not isinstance(owner.get("pid"), int) or
            isinstance(owner.get("pid"), bool) or owner.get("pid") < 1 or
            not isinstance(owner.get("boot_id"), str) or not owner.get("boot_id") or
            not isinstance(owner.get("start_time"), str) or not owner.get("start_time")):
        raise PreparationError("role_home_recovery", "role-home owner identity is invalid")
    role_home = _role_home_path(data.get("path"))
    if role_home.name != lease_id:
        raise PreparationError("role_home_recovery", "role-home lease does not match its path")
    if process_identity_matches(owner):
        raise PreparationError("workspace_in_use", "cannot reconcile a role home while its App Server is active")
    # A reboot may remove the ephemeral /tmp home while preserving .git. Once
    # the recorded owner is stale, clearing only that durable marker is safe:
    # there is no external path left to delete. If any path entry remains,
    # bilateral lease equality is still mandatory before recursive deletion.
    if not role_home.exists() and not role_home.is_symlink():
        try:
            marker.unlink()
        except OSError as exc:
            raise PreparationError("role_home_recovery", "stale role-home marker could not be removed") from exc
        return True
    lease_file = role_home / ROLE_HOME_LEASE_FILE
    try:
        lease = json.loads(lease_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreparationError("role_home_recovery", "role-home lease cannot be read") from exc
    if lease != data:
        raise PreparationError("role_home_recovery", "role-home lease does not match its workspace marker")
    try:
        if role_home.is_symlink():
            role_home.unlink()
        elif role_home.is_dir():
            shutil.rmtree(role_home)
        elif role_home.exists():
            role_home.unlink()
        marker.unlink()
    except OSError as exc:
        raise PreparationError("role_home_recovery", "stale role home could not be removed") from exc
    return True


def recovery_path_is_safe(path: pathlib.Path) -> bool:
    """Apply the pre-existing host rule that secrets never enter archives."""
    sensitive_names = {".env", "credentials", "credential", "secret", "secrets",
                       "token", "tokens", "private-key", "private_keys"}
    sensitive_suffixes = {".key", ".pem", ".p12", ".pfx", ".kdbx", ".token", ".secret"}
    for part in path.parts:
        name = part.lower()
        if name in sensitive_names or name.startswith(".env."):
            return False
    name = path.name.lower()
    return not name.endswith(".env") and not any(name.endswith(suffix) for suffix in sensitive_suffixes)


def recovery_entries(workspace: pathlib.Path) -> tuple[list[tuple[pathlib.Path, str]], list[str]]:
    """Walk recovery input recursively without crossing excluded paths."""
    entries: list[tuple[pathlib.Path, str]] = []
    excluded: list[str] = []

    def visit(path: pathlib.Path, relative: pathlib.PurePath) -> None:
        relative_text = relative.as_posix()
        if not recovery_path_is_safe(relative):
            excluded.append(relative_text)
            return
        if path.is_symlink():
            excluded.append(relative_text)
            return
        if path.is_dir():
            entries.append((path, relative_text))
            for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
                visit(child, relative / child.name)
        else:
            entries.append((path, relative_text))

    for child in sorted(workspace.iterdir(), key=lambda item: item.name.lower()):
        if child.name in {".git", "target"}:
            continue
        visit(child, pathlib.PurePath(child.name))
    return entries, excluded


def archive_recovery(profile: Profile, workspace: pathlib.Path, facts: IssueFacts, status: str) -> pathlib.Path:
    directory = profile.state_root / "recovery" / f"GH-{facts.issue}"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = directory / f"{stamp}-{uuid.uuid4().hex[:8]}.tar.gz"
    entries, excluded = recovery_entries(workspace)
    with tarfile.open(archive, "w:gz") as output:
        for path, relative in entries:
            output.add(path, arcname=relative, recursive=False)
        manifest = json.dumps({"repo": profile.repository, "issue": facts.issue,
                               "local_head": git(workspace, "rev-parse", "HEAD", check=False),
                               "remote_head": facts.target_sha, "status": status,
                               "created_utc": stamp, "excluded_paths": excluded}, sort_keys=True).encode()
        import io
        info = tarfile.TarInfo("RECOVERY-MANIFEST.json")
        info.size = len(manifest)
        output.addfile(info, io.BytesIO(manifest))
    return archive


def snapshot(workspace: pathlib.Path, commit: str) -> tuple[dict[str, str], dict[str, str]]:
    expected = {}
    for row in git(workspace, "ls-tree", "-r", "--full-tree", commit).splitlines():
        left, path = row.split("\t", 1)
        expected[path] = left.split()[2]
    actual = {}
    for path in git(workspace, "ls-files", "-co", "--exclude-standard", "-z").split("\0"):
        if path:
            actual[path] = git(workspace, "hash-object", "--path", path, "--", path)
    return expected, actual


def dirty_equals_remote(workspace: pathlib.Path, commit: str) -> bool:
    expected, actual = snapshot(workspace, commit)
    return expected == actual


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
    data = {"schema": "symphony-pilot-preparation/v1", "profile": profile.slug,
            "repo": profile.repository, "issue": facts.issue, "issue_branch": facts.branch,
            "resolved_head_sha": facts.target_sha, "remote_issue_branch_sha": facts.remote_sha,
            "required_start_ref": facts.required_ref, "required_start_sha": facts.required_sha,
            "base_ref": facts.base_ref, "base_sha": facts.base_sha,
            "upstream": "origin/" + facts.branch if facts.remote_sha else None,
            "mode": facts.mode, "clean_status": True,
            "prepared_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "run_id": uuid.uuid4().hex, "toolchain": tools,
            "publication_preflight": "git-push-dry-run"}
    path = workspace / ".git/symphony-preparation.json"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def compact_workpad(body: str, limit: int = 24000) -> str:
    sections = re.split(r"(?=^## |\n## )", body, flags=re.M)
    seen: set[str] = set()
    kept = []
    for section in reversed(sections):
        digest = hashlib.sha256(section.strip().encode()).hexdigest()
        if digest not in seen:
            seen.add(digest)
            kept.append(section)
    result = "".join(reversed(kept)).strip()
    if len(result) <= limit:
        return result
    header = "<!-- symphony-workpad:v1 -->\n## Symphony Workpad\n\n"
    return header + result[len(header):][-max(0, limit - len(header)):]


def blocker_fingerprint(profile: Profile, workspace: pathlib.Path, facts: IssueFacts,
                        kind: str, detail: str) -> str:
    status = git(workspace, "status", "--porcelain=v1", "--untracked-files=all", check=False)
    stable = re.sub(r"; recovery artifact [^;]+", "", detail)
    value = "\0".join((profile.slug, str(facts.issue), facts.branch, facts.target_sha, kind, stable, status))
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def record_blocker(profile: Profile, token: str, workspace: pathlib.Path, facts: IssueFacts,
                   kind: str, detail: str) -> None:
    digest = blocker_fingerprint(profile, workspace, facts, kind, detail)
    path = profile.state_root / "blockers" / f"GH-{facts.issue}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(path.read_text()) if path.exists() else {}
    repeated = old.get("fingerprint") == digest
    path.write_text(json.dumps({"fingerprint": digest, "kind": kind,
                                "issue": facts.issue, "status": "active"}, indent=2))
    if repeated:
        return
    pad = workpad(facts.comments)
    body = ("<!-- symphony-workpad:v1 -->\n## Symphony Workpad\n\n"
            "### Infrastructure blocker\n"
            f"- class: {kind}\n- issue: #{facts.issue}\n- detail: {detail}\n"
            f"- blocker fingerprint: {digest}\n- worker was not started.\n")
    if pad:
        body += "\n### Preserved history\n" + compact_workpad(pad.get("body") or "")[-12000:] + "\n"
        github(profile, token, "PATCH", f"/issues/comments/{pad['id']}", {"body": body})
    else:
        github(profile, token, "POST", f"/issues/{facts.issue}/comments", {"body": body})
    labels = github(profile, token, "GET", f"/issues/{facts.issue}/labels?per_page=100")
    names = [item["name"] for item in labels if item.get("name") not in set(profile.dispatch_labels) | {profile.blocked_label}]
    github(profile, token, "PUT", f"/issues/{facts.issue}/labels", {"labels": names + [profile.blocked_label]})


def prepare(profile: Profile, workspace: pathlib.Path) -> None:
    token = read_secret(profile)
    workspace = workspace.resolve()
    if not str(workspace).startswith(str(profile.workspace_root) + os.sep):
        raise PreparationError("workspace_path", "workspace is outside the profile workspace root")
    if not workspace.exists():
        workspace.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--no-single-branch", profile.git_remote, str(workspace)], check=True)
    try:
        verify_repository(profile, workspace)
    except PreparationError:
        if process_owns_workspace(workspace):
            raise PreparationError("workspace_in_use", "cannot replace a workspace owned by another process")
        if workspace.exists():
            shutil.move(str(workspace), str(workspace.parent / (workspace.name + ".stale." + uuid.uuid4().hex[:8])))
        subprocess.run(["git", "clone", "--no-single-branch", profile.git_remote, str(workspace)], check=True)
        verify_repository(profile, workspace)
    lock_path = profile.state_root / "locks" / f"{profile.slug}-GH-{workspace.name.removeprefix('GH-')}.lock"
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
            reconcile_role_home(workspace)
            facts = issue_facts(profile, workspace, token)
            fetch = subprocess.run(["git", "fetch", "origin", "refs/heads/" + facts.branch + ":refs/remotes/origin/" + facts.branch],
                                   cwd=workspace, text=True, capture_output=True)
            if fetch.returncode:
                if facts.remote_sha:
                    raise PreparationError("git_fetch", "authoritative issue branch fetch failed")
                requested = (facts.required_ref or "master").removeprefix("origin/")
                if subprocess.run(["git", "fetch", "origin", requested], cwd=workspace).returncode:
                    raise PreparationError("git_fetch", "licensed starting ref fetch failed")
            if facts.base_sha:
                subprocess.run(["git", "fetch", "origin", (facts.base_ref or "master").removeprefix("origin/")], cwd=workspace, check=False)
                if subprocess.run(["git", "merge-base", "--is-ancestor", facts.base_sha, facts.target_sha], cwd=workspace).returncode:
                    raise PreparationError("ancestry", "issue continuation is not based on the required base")
            status = git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
            if status:
                if dirty_equals_remote(workspace, facts.target_sha):
                    archive_recovery(profile, workspace, facts, status)
                    git(workspace, "reset", "--hard", facts.target_sha)
                    git(workspace, "clean", "-fdx")
                else:
                    archive = archive_recovery(profile, workspace, facts, status)
                    record_blocker(profile, token, workspace, facts, "unique_unpublished_changes",
                                   "dirty workspace differs from authoritative remote; recovery artifact " + archive.name)
                    raise PreparationError("unique_unpublished_changes", "dirty workspace differs from authoritative remote", persisted=True)
            git(workspace, "switch", "-C", facts.branch, facts.target_sha)
            if facts.remote_sha:
                git(workspace, "branch", "--set-upstream-to", "origin/" + facts.branch, facts.branch)
            if git(workspace, "status", "--porcelain"):
                raise PreparationError("clean_verification", "workspace remained dirty")
            tools = prepare_toolchain(profile, workspace, facts.issue)
            push = subprocess.run(["git", "push", "--dry-run", "origin", f"HEAD:refs/heads/{facts.branch}"], cwd=workspace, capture_output=True, text=True)
            if push.returncode:
                raise PreparationError("publication_preflight", "Git publication dry-run failed")
            marker(profile, workspace, facts, tools)
            print(json.dumps({"profile": profile.slug, "issue": facts.issue, "branch": facts.branch,
                              "head": facts.target_sha, "mode": facts.mode, "marker": ".git/symphony-preparation.json"}, sort_keys=True))
        except PreparationError as exc:
            if facts is not None and not exc.persisted:
                try:
                    record_blocker(profile, token, workspace, facts, exc.kind, str(exc))
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
