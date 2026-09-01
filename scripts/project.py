#!/usr/bin/env python3
"""Operator-facing lifecycle controls for one Symphony project."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import pathlib
import re
import signal
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
ROOT = pathlib.Path(__file__).resolve().parents[1]
ROLE_POLICY_NAMES = ("project-manager", "planner", "implementer", "reviewer", "adversary", "archivist")
sys.path.insert(0, str(ROOT / "runtime"))
from host_integration import AWAKE_STATE, establish_awake_guard, release_awake_guard, release_awake_guard_at
from process_identity import capture, matches, read
from prepare_workspace import (
    DASHBOARD_PORT_MAX,
    DASHBOARD_PORT_MIN,
    PreparationError,
    deployment_path,
    github,
    load_profile,
    read_secret,
    require_physical_namespace,
    state_namespace_for_slug,
)
from deployment_contract import contract_digest, deployment_identity
from containment import ContainmentError, backend_identity, require_execution_capability
from runtime_lock import RuntimeLockError, identify, validate_lock, verify_entry
from rulesets import RulesetError, fetch_all_rulesets, fetch_ruleset_details, require_default_branch_ruleset
from project_registry import resolve_project


def file_digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def state_paths(profile):
    state_root = require_physical_namespace(profile.state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    return state_root / "symphony.pid", state_root / "symphony.log"


def recovery_state_path(slug: str) -> pathlib.Path:
    """Derive only the supplied slug's process-state path; do not create it."""
    return state_namespace_for_slug(slug) / "symphony.pid"


def read_recovery_state(slug: str):
    """Load and strictly validate one existing managed-process record."""
    path = recovery_state_path(slug)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PreparationError("recovery_state", "managed process state is malformed") from exc
    expected_fields = {
        "schema", "identity", "deployment_root", "deployment_identity",
        "deployed_profile_sha256", "dashboard_url",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise PreparationError("recovery_state", "managed process state fields are invalid")
    if value["schema"] != "symphony-pilot-process/v2":
        raise PreparationError("recovery_state", "managed process state schema is not accepted")
    identity = value["identity"]
    if (not isinstance(identity, dict) or set(identity) != {"pid", "boot_id", "start_time"} or
            not isinstance(identity["pid"], int) or isinstance(identity["pid"], bool) or
            identity["pid"] < 1 or not isinstance(identity["boot_id"], str) or
            not identity["boot_id"] or not isinstance(identity["start_time"], str) or
            not identity["start_time"]):
        raise PreparationError("recovery_state", "managed process identity is invalid")
    if (not isinstance(value["deployment_root"], str) or
            not pathlib.PurePosixPath(value["deployment_root"]).is_absolute()):
        raise PreparationError("recovery_state", "managed deployment root is invalid")
    for field in ("deployment_identity", "deployed_profile_sha256"):
        if not isinstance(value[field], str) or not re.fullmatch(r"[0-9a-f]{64}", value[field]):
            raise PreparationError("recovery_state", f"managed {field} is invalid")
    endpoint = value["dashboard_url"]
    match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]+)", endpoint) if isinstance(endpoint, str) else None
    if not match or not DASHBOARD_PORT_MIN <= int(match.group(1)) <= DASHBOARD_PORT_MAX:
        raise PreparationError("recovery_state", "managed dashboard endpoint is invalid")
    return value


def ensure_dashboard_port_available(port: int) -> None:
    """Report unrelated host occupancy without changing the persisted port."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise PreparationError(
            "dashboard_port",
            f"dashboard port {port} is occupied or unavailable; persisted allocation was not changed",
        ) from exc

def install_root(profile):
    return deployment_path(profile)


def resolve_symphony_binary() -> str:
    """Resolve the owned host runtime without consulting any deployment."""
    configured = os.environ.get("SYMPHONY_BIN")
    candidate = pathlib.Path(configured).expanduser() if configured else None
    if candidate:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise FileNotFoundError("SYMPHONY_BIN does not name an executable file")
    found = shutil.which("symphony")
    if found:
        return found
    raise FileNotFoundError("owned Symphony runtime executable was not found on PATH")

def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _identity_alive(identity):
    return matches(identity)


def _managed_identity(profile):
    pid_path, _ = state_paths(profile)
    if not pid_path.exists():
        return None
    state = read(pid_path)
    identity = state.get("identity") if state else None
    if not identity or not _identity_alive(identity):
        return None
    return identity


def _safe_pid(profile):
    identity = _managed_identity(profile)
    return int(identity["pid"]) if identity else None


def _issue_id(entry):
    if not isinstance(entry, dict):
        return None
    for key in ("issue_identifier", "issue_number", "identifier", "issue"):
        value = entry.get(key)
        if value is not None:
            return str(value)
    return None


def _active_entries(view):
    if not isinstance(view, dict):
        return []
    return list(view.get("running") or []) + list(view.get("retrying") or [])


def _complete_process_stop(state_path, release=None):
    """Remove process bookkeeping only after stop, then reconcile host state."""
    state_path.unlink(missing_ok=True)
    try:
        complete = release() if release else True
    except (OSError, PreparationError) as exc:
        print(f"Symphony stopped, but host-awake cleanup is incomplete: {exc}")
        return 1
    if complete is False:
        print("Symphony stopped, but host-awake cleanup is incomplete; "
              "SAFE TO SHUT DOWN cannot be reported.")
        return 1
    print("Symphony stopped - SAFE TO SHUT DOWN")
    return 0


def _stop_process_at(state_path, identity, release=None):
    pid = int(identity["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return _complete_process_stop(state_path, release)
    except (PermissionError, OSError) as exc:
        print(f"Cannot stop Symphony safely: {exc}")
        return 1
    try:
        while _identity_alive(identity):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stop cancelled; Symphony remains running.")
        return 130
    return _complete_process_stop(state_path, release)


def _stop_process(profile, identity):
    return _stop_process_at(
        state_paths(profile)[0], identity,
        release=lambda: release_awake_guard(profile),
    )


def _write_process_state(profile, identity, verification):
    dashboard_url = verification.get("dashboard_url", f"http://127.0.0.1:{profile.dashboard_port}")
    state_paths(profile)[0].write_text(
        json.dumps({
            "schema": "symphony-pilot-process/v2",
            "identity": identity,
            "deployment_root": str(verification["root"]),
            "deployment_identity": verification["deployment_identity"],
            "deployed_profile_sha256": verification["profile_sha256"],
            "dashboard_url": dashboard_url,
        },
                   sort_keys=True) + "\n", encoding="utf-8")


def _terminate_unverified_child(child, profile):
    """Bound cleanup for a just-created child whose /proc identity is unavailable."""
    try:
        child.terminate()
    except (PermissionError, OSError):
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
        except (PermissionError, OSError):
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print("Cannot start Symphony: its process identity could not be verified "
                  "and the child did not exit after terminate/kill; the awake guard "
                  "was retained for operator attention.")
            return False
    except OSError:
        if child.poll() is None:
            print("Cannot start Symphony: its process identity could not be verified "
                  "and child exit could not be confirmed; the awake guard was retained "
                  "for operator attention.")
            return False
    if child.poll() is None:
        print("Cannot start Symphony: its process identity could not be verified "
              "and child exit could not be confirmed; the awake guard was retained "
              "for operator attention.")
        return False
    if release_awake_guard(profile):
        return True
    print("Cannot start Symphony: its process exited, but host-awake cleanup is incomplete.")
    return False

def _dashboard_url(profile):
    if not profile.dashboard_port:
        return None
    try:
        state = read(state_paths(profile)[0]) if state_paths(profile)[0].exists() else None
    except (OSError, ValueError, TypeError):
        state = None
    endpoint = state.get("dashboard_url") if isinstance(state, dict) else None
    match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]+)", endpoint) if isinstance(endpoint, str) else None
    if not match or not DASHBOARD_PORT_MIN <= int(match.group(1)) <= DASHBOARD_PORT_MAX:
        endpoint = f"http://127.0.0.1:{profile.dashboard_port}"
    return endpoint


def dashboard(profile):
    endpoint = _dashboard_url(profile)
    if endpoint is None:
        return None
    try:
        with urllib.request.urlopen(endpoint + "/", timeout=2) as response:
            payload = response.read()
            try:
                return json.loads(payload)
            except ValueError:
                return {"state": "running"}
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None

def runtime_state_at(endpoint):
    try:
        with urllib.request.urlopen(
            endpoint + "/api/v1/state", timeout=2
        ) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def runtime_state(profile):
    endpoint = _dashboard_url(profile)
    return runtime_state_at(endpoint) if endpoint is not None else None

def active_issues(profile, token):
    issues = github(profile, token, "GET", "/issues?state=open&labels=" +
                    urllib.parse.quote(",".join(profile.dispatch_labels)) + "&per_page=100")
    return issues if isinstance(issues, list) else []


def branch_protection_preflight(profile, token):
    """Require the one supported real GitHub repository-ruleset contract."""
    repository = github(profile, token, "GET", "")
    default = repository.get("default_branch") if isinstance(repository, dict) else None
    if not isinstance(default, str) or not default:
        raise RulesetError("GitHub did not report the protected default branch")
    summaries = fetch_all_rulesets(lambda page: github(
        profile, token, "GET", f"/rulesets?includes_parents=true&per_page=100&page={page}"))
    rulesets = fetch_ruleset_details(summaries, lambda ruleset_id: github(
        profile, token, "GET", f"/rulesets/{ruleset_id}?includes_parents=true"))
    return require_default_branch_ruleset(rulesets, default)

def project_name(profile):
    return profile.display_name or profile.slug

def start(profile):
    pid_path, log_path = state_paths(profile)
    try:
        verification = verify_deployment(profile)
    except (OSError, ValueError, TypeError, PreparationError) as exc:
        print(f"Cannot start Symphony: deployment coherence verification failed: {exc}")
        return 1
    root = verification["root"]
    workflow = root / "projects" / profile.slug / "WORKFLOW.md"
    try:
        ensure_dashboard_port_available(profile.dashboard_port)
    except PreparationError as exc:
        print(f"Cannot start Symphony: {exc}")
        return 1
    try:
        binary = resolve_symphony_binary()
        codex = shutil.which("codex")
        if not codex:
            raise RuntimeLockError("official Codex executable was not found on PATH")
        containment = backend_identity()
        lock_path = require_physical_namespace(profile.state_root) / "runtime-lock.json"
        lock = validate_lock(json.loads(lock_path.read_text(encoding="utf-8")))
        verify_entry(lock["symphony"], identify(binary), "Symphony")
        verify_entry(lock["codex"], identify(codex), "Codex")
        verify_entry(lock["containment"], {
            "executable": containment.executable,
            "version": containment.version,
            "sha256": containment.sha256,
        }, "containment")
    except (OSError, ValueError, TypeError, RuntimeLockError, PreparationError, ContainmentError) as exc:
        print(f"Cannot start Symphony: reviewed runtime identity is unavailable: {exc}")
        return 78
    # This gate runs before tracker credential acquisition or process launch.
    # It is intentionally not a fallback to the old same-user architecture.
    try:
        require_execution_capability()
    except ContainmentError as exc:
        print(f"Cannot start Symphony: containment capability blocker: {exc}")
        return 78
    if pid_path.exists():
        identity = read(pid_path)
        identity = identity.get("identity") if identity else None
        if identity and _identity_alive(identity):
            print("Cannot start Symphony: a pre-existing managed process must be stopped before cutover")
            return 78
        print("Cannot start Symphony: stale process state requires explicit recovery before cutover")
        return 78
    try:
        token = read_secret(profile)
    except PreparationError as exc:
        print(f"Cannot start Symphony: {exc}")
        return 1
    try:
        branch_protection_preflight(profile, token)
    except (RulesetError, PreparationError) as exc:
        print(f"Cannot start Symphony: protected default-branch preflight failed: {exc}")
        return 78
    try:
        issues = active_issues(profile, token)
    except Exception as exc:
        print(f"Cannot verify GitHub dispatch state: {type(exc).__name__}. No process was started.")
        return 1
    if len(issues) > profile.max_concurrent_agents:
        print(f"Refusing to start: {len(issues)} dispatchable issues exceed the one-issue pilot limit.")
        return 1
    try:
        establish_awake_guard(profile)
    except (RuntimeError, PreparationError) as exc:
        print(f"Cannot start {project_name(profile)}: {exc}")
        return 1
    env = os.environ.copy()
    env["SYMPHONY_PILOT_GITHUB_TOKEN"] = token
    env["SYMPHONY_PROFILE"] = str(root / "profile.toml")
    env["SYMPHONY_WORKFLOW"] = str(workflow)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    log = log_path.open("ab")
    command = [binary, "--i-understand-that-this-will-be-running-without-the-usual-guardrails",
               "--logs-root", str(profile.log_root)]
    if profile.dashboard_port:
        command += ["--port", str(profile.dashboard_port)]
    command.append(str(workflow))
    try:
        child = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=log,
                                 start_new_session=True, close_fds=True)
    except OSError as exc:
        log.close()
        release_awake_guard(profile)
        print(f"Cannot start Symphony: {exc}. Check the official binary and deployment.")
        return 1
    finally:
        if not log.closed:
            log.close()
    identity = capture(child.pid)
    if identity is None:
        if _terminate_unverified_child(child, profile):
            print("Cannot start Symphony: its process identity could not be verified; "
                  "the child exited and the awake guard was released.")
        return 1
    _write_process_state(profile, identity, verification)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if dashboard(profile) is not None:
            print("Symphony is running; dashboard is reachable.")
            return 0
        if child.poll() is not None:
            print("Symphony exited during startup. Review the project log.")
            pid_path.unlink(missing_ok=True)
            _report_startup_cleanup(profile, "Symphony exited during startup")
            return 1
        time.sleep(0.5)
    print("Symphony dashboard did not become reachable within 30 seconds; requesting normal shutdown.")
    if not _identity_alive(identity):
        if pid_alive(int(identity["pid"])):
            print("Startup identity could not be confirmed; PID and awake guard were retained for operator attention.")
            return 1
        pid_path.unlink(missing_ok=True)
        _report_startup_cleanup(profile, "Symphony exited during startup")
        return 1
    try:
        os.kill(int(identity["pid"]), signal.SIGTERM)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and _identity_alive(identity):
            time.sleep(0.5)
    except ProcessLookupError:
        pass
    except (PermissionError, OSError) as exc:
        print(f"Startup cleanup could not signal Symphony; PID and awake guard were retained: {exc}")
        return 1
    except KeyboardInterrupt:
        print("Startup cleanup cancelled; Symphony may still be running. PID and awake guard were retained.")
        return 1
    if _identity_alive(identity):
        print("Startup failed; Symphony may still be running. PID and awake guard were retained for operator attention.")
        return 1
    pid_path.unlink(missing_ok=True)
    _report_startup_cleanup(profile, "Symphony exited after startup timeout")
    return 1


def _release_awake_for_shutdown(profile) -> bool:
    try:
        return release_awake_guard(profile)
    except (OSError, PreparationError):
        return False


def _report_stopped(profile, prefix: str) -> int:
    """Report process state separately from complete pilot-owned cleanup."""
    if _release_awake_for_shutdown(profile):
        print(f"{prefix} - SAFE TO SHUT DOWN")
        return 0
    print(f"{prefix}, but host-awake cleanup is incomplete.")
    print("SAFE TO SHUT DOWN cannot be reported.")
    return 1


def _report_startup_cleanup(profile, prefix: str) -> bool:
    if _release_awake_for_shutdown(profile):
        print(f"{prefix}; awake guard released.")
        return True
    print(f"{prefix}; host-awake cleanup is incomplete.")
    return False

def stop(profile, force=False):
    pid_path, _ = state_paths(profile)
    if not pid_path.exists():
        return _report_stopped(profile, "Symphony is stopped")
    state = read(pid_path)
    identity = state.get("identity") if state else None
    if not identity:
        print("Cannot stop safely: managed Symphony identity is missing or stale; no process was terminated.")
        return 1
    if not _identity_alive(identity):
        if pid_alive(int(identity["pid"])):
            print("Cannot stop safely: managed Symphony PID identity is stale or reused; no process was terminated.")
            return 1
        pid_path.unlink(missing_ok=True)
        return _report_stopped(profile, "Symphony is stopped")
    if not force:
        view = runtime_state(profile)
        if view is None:
            print("Cannot verify Symphony activity; the process was not stopped.")
            return 1
        if view.get("running") or view.get("retrying"):
            print("Finish/drain must be performed before stopping active work.")
            return 2
    return _stop_process(profile, identity)


def finish(profile):
    """Drain one pilot issue, then perform the normal stop operation."""
    pid = _safe_pid(profile)
    if pid is None:
        return _report_stopped(profile, "STOPPED")
    view = runtime_state(profile)
    if view is None:
        print("Cannot finish safely: authoritative Symphony runtime state is unavailable.")
        return 1
    initial = {_issue_id(entry) for entry in _active_entries(view)} - {None}
    if len(initial) > 1:
        print("Cannot finish safely: more than one issue is active in the one-issue pilot.")
        return 1
    if initial:
        print(f"{project_name(profile)} is finishing; current work will be allowed to finish before Symphony stops.")
    try:
        while True:
            active = _active_entries(view)
            current = {_issue_id(entry) for entry in active} - {None}
            if len(current) > 1 or (initial and current and current != initial):
                print("Cannot finish safely: a different or additional issue appeared while draining.")
                return 1
            if not active:
                break
            time.sleep(max(0.5, profile.poll_interval_ms / 1000))
            view = runtime_state(profile)
            if view is None:
                print("Cannot finish safely: authoritative Symphony runtime state became unavailable.")
                return 1
    except KeyboardInterrupt:
        print("Finish cancelled; Symphony and active work remain running.")
        return 130
    return stop(profile)

def status(profile):
    pid_path, _ = state_paths(profile)
    identity = _managed_identity(profile)
    running = identity is not None
    if not running:
        pid_path.unlink(missing_ok=True)
        return _report_stopped(profile, "STOPPED")
    view = runtime_state(profile) or dashboard(profile)
    if isinstance(view, dict):
        blocked = view.get("blocked") or []
        running_entries = view.get("running") or []
        retrying_entries = view.get("retrying") or []
        state = str(view.get("state") or view.get("status") or "").lower()
        issue = view.get("issue") or view.get("issue_number") or "?"
        entries = blocked or running_entries or retrying_entries
        if entries and isinstance(entries[0], dict):
            issue = (entries[0].get("issue_identifier") or entries[0].get("issue_number")
                     or entries[0].get("identifier") or issue)
        if blocked or "human" in state or "blocked" in state:
            print(f"NEEDS YOU #{issue} - WORK SAFELY PAUSED")
        elif running_entries or retrying_entries:
            print(f"WORKING ON #{issue} - DO NOT SHUT DOWN")
        elif "finish" in state or "drain" in state:
            print(f"FINISHING #{issue} - DO NOT SHUT DOWN YET")
        elif "work" in state or "run" in state:
            print(f"WORKING ON #{issue} - DO NOT SHUT DOWN")
        else:
            print("IDLE - SAFE TO STOP")
    else:
        print("WORKING - DO NOT SHUT DOWN")
    return 0


def recovery_status(slug: str, state: dict) -> int:
    """Inspect only the process identity persisted for the supplied slug."""
    identity = state["identity"]
    if not _identity_alive(identity):
        if pid_alive(identity["pid"]):
            print(f"RECOVERY {slug}: managed PID identity is stale or reused; no process was touched.")
            return 1
        print(f"RECOVERY {slug}: managed process is no longer running.")
        return 0
    view = runtime_state_at(state["dashboard_url"])
    if isinstance(view, dict):
        current = view.get("running") or view.get("retrying") or []
        activity = "active" if current else "idle"
        print(f"RECOVERY {slug}: managed PID {identity['pid']} is {activity}; "
              f"deployment {state['deployment_identity']} at {state['dashboard_url']}.")
    else:
        print(f"RECOVERY {slug}: managed PID {identity['pid']} is running; "
              f"runtime state unavailable at {state['dashboard_url']}.")
    return 0


def recovery_stop_now(slug: str, state: dict) -> int:
    """Emergency-stop exactly the previously recorded process, without a profile."""
    identity = state["identity"]
    process_state_path = recovery_state_path(slug)
    awake_state_path = process_state_path.with_name(AWAKE_STATE)
    release = lambda: release_awake_guard_at(awake_state_path)
    if not _identity_alive(identity):
        if pid_alive(identity["pid"]):
            print(f"RECOVERY {slug}: managed PID identity is stale or reused; no process was terminated.")
            return 1
        result = _complete_process_stop(process_state_path, release)
        if result == 0:
            print(f"RECOVERY {slug}: process is already stopped - SAFE TO SHUT DOWN")
        return result
    return _stop_process_at(
        process_state_path,
        identity,
        release=release,
    )

def verify_manifest(root, manifest_path, manifest):
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("manifest files inventory is missing")
    root = root.resolve()
    actual_files = {str(path.relative_to(root)).replace("\\", "/")
                    for path in root.rglob("*")
                    if path.is_file() and path.resolve() != manifest_path.resolve()}
    if actual_files != set(files):
        missing = sorted(set(files) - actual_files)
        unexpected = sorted(actual_files - set(files))
        raise ValueError(f"manifest inventory mismatch: missing={missing}, unexpected={unexpected}")
    for relative, expected in files.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise ValueError("manifest files inventory is malformed")
        path = (root / pathlib.PurePosixPath(relative)).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"manifest path escapes deployment root: {relative}") from exc
        if path == manifest_path.resolve() or not path.is_file():
            raise ValueError(f"manifest file is missing: {relative}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected != actual:
            raise ValueError(f"deployed file does not match its manifest: {relative}")


REQUIRED_DEPLOYMENT_FILES = (
    "profile.toml",
    "runtime/prepare_workspace.py",
    "runtime/after_run.py",
    "runtime/broker.py",
    "runtime/before_remove.py",
    "runtime/host_integration.py",
    "runtime/process_identity.py",
    "runtime/launch_codex.sh",
    "runtime/deployment_contract.py",
    "runtime/containment.py",
    "runtime/task_admission.py",
    "runtime/admit_task.py",
    "runtime/outbox.py",
    "runtime/rulesets.py",
    "runtime/publication.py",
    "runtime/runtime_lock.py",
    "workflow/architect_policy.md",
    "projects/{slug}/WORKFLOW.md",
    "workflow/agents/project-manager.toml",
    "workflow/agents/planner.toml",
    "workflow/agents/implementer.toml",
    "workflow/agents/reviewer.toml",
    "workflow/agents/adversary.toml",
    "workflow/agents/archivist.toml",
)


def verify_deployment(profile):
    """Verify the selected source profile and generated deployment as one snapshot."""
    root = require_physical_namespace(install_root(profile))
    manifest_path = root / "DEPLOYMENT.json"
    if not manifest_path.is_file():
        raise PreparationError("deployment", "DEPLOYMENT.json is missing; redeploy the selected project")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreparationError("deployment", "DEPLOYMENT.json cannot be read") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != "symphony-pilot-deployment/v2":
        raise PreparationError("deployment", "DEPLOYMENT.json schema is not accepted")
    if manifest.get("profile") != profile.slug:
        raise PreparationError("deployment", "deployment belongs to a different project slug")
    required = [root / relative.format(slug=profile.slug) for relative in REQUIRED_DEPLOYMENT_FILES]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise PreparationError("deployment", "required generated files are missing: " + ", ".join(missing))
    try:
        verify_manifest(root, manifest_path, manifest)
        source_profile_path = profile.source_profile_path
        if source_profile_path is None or not source_profile_path.is_file():
            raise ValueError("selected canonical profile path is unavailable")
        deployed_profile_path = root / "profile.toml"
        source_bytes = source_profile_path.read_bytes()
        deployed_bytes = deployed_profile_path.read_bytes()
        if source_bytes != deployed_bytes:
            raise ValueError("deployed profile snapshot differs from selected canonical profile")
        deployed_profile = load_profile(deployed_profile_path)
        if deployed_profile.slug != profile.slug:
            raise ValueError("deployed profile slug does not match selected project")
        profile_sha256 = file_digest(deployed_profile_path)
        if manifest.get("profile_sha256") != profile_sha256:
            raise ValueError("deployed profile digest does not match DEPLOYMENT.json")
        operator_contract_sha256 = contract_digest(ROOT)
        if manifest.get("operator_contract_sha256") != operator_contract_sha256:
            raise ValueError("source operator/runtime contract differs; redeploy the selected project")
        expected_identity = deployment_identity(
            profile.slug, profile_sha256, operator_contract_sha256, manifest["files"]
        )
        if manifest.get("deployment_identity") != expected_identity:
            raise ValueError("deployment identity does not match its accepted contents")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise PreparationError("deployment", str(exc)) from exc
    return {
        "root": root,
        "deployment_identity": manifest["deployment_identity"],
        "profile_sha256": manifest["profile_sha256"],
        "dashboard_url": f"http://127.0.0.1:{profile.dashboard_port}",
    }

def test(profile):
    try:
        verification = verify_deployment(profile)
    except PreparationError as exc:
        print(f"Deployment validation failed: {exc}")
        return 1
    print(f"Deployment validation passed for {profile.slug} at {verification['root']}")
    return 0

def main():
    parser = argparse.ArgumentParser(description="symphony-pilot project lifecycle")
    parser.add_argument("--project", required=True, help="registered project slug")
    parser.add_argument("action", choices=("start", "status", "stop", "stop-now", "finish", "test"))
    args = parser.parse_args()
    try:
        if not args.project:
            raise PreparationError("project", "ordinary source operation requires --project <registered-slug>")
        profile = resolve_project(args.project, ROOT / "projects")
    except PreparationError as exc:
        if args.action in ("status", "stop-now"):
            try:
                recovery = read_recovery_state(args.project)
            except PreparationError as recovery_error:
                print(f"symphony-pilot recovery stopped: {recovery_error.kind}: {recovery_error}",
                      file=sys.stderr)
                return 78
            if recovery is not None:
                if args.action == "status":
                    return recovery_status(args.project, recovery)
                return recovery_stop_now(args.project, recovery)
            print(f"symphony-pilot project resolution stopped: {exc.kind}: {exc}; "
                  "no valid managed process state exists for recovery", file=sys.stderr)
            return 78
        if args.action in ("stop", "finish"):
            print(f"symphony-pilot project resolution stopped: {exc.kind}: {exc}; "
                  "normal control is unavailable, use --project <slug> stop-now only "
                  "after reviewing the managed process state", file=sys.stderr)
            return 78
        print(f"symphony-pilot project resolution stopped: {exc.kind}: {exc}", file=sys.stderr)
        return 78
    try:
        if args.action == "start": return start(profile)
        if args.action == "stop": return stop(profile)
        if args.action == "stop-now": return stop(profile, force=True)
        if args.action == "finish": return finish(profile)
        if args.action == "status": return status(profile)
        return test(profile)
    except PreparationError as exc:
        print(f"symphony-pilot operation stopped: {exc.kind}: {exc}", file=sys.stderr)
        return 78

if __name__ == "__main__":
    raise SystemExit(main())
