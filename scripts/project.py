#!/usr/bin/env python3
"""Operator-facing lifecycle controls for one Symphony project."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from host_integration import establish_awake_guard, release_awake_guard
from process_identity import capture, matches, read
from prepare_workspace import github, load_profile, read_secret

def state_paths(profile):
    profile.state_root.mkdir(parents=True, exist_ok=True)
    return profile.state_root / "symphony.pid", profile.state_root / "symphony.log"

def install_root(profile):
    return profile.deployment_root or pathlib.Path.home() / ".local/share/symphony-pilot/deployments" / profile.slug

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


def _stop_process(profile, identity):
    pid = int(identity["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        state_paths(profile)[0].unlink(missing_ok=True)
        release_awake_guard(profile)
        print("Symphony stopped - SAFE TO SHUT DOWN")
        return 0
    except (PermissionError, OSError) as exc:
        print(f"Cannot stop Symphony safely: {exc}")
        return 1
    try:
        while _identity_alive(identity):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stop cancelled; Symphony remains running.")
        return 130
    state_paths(profile)[0].unlink(missing_ok=True)
    release_awake_guard(profile)
    print("Symphony stopped - SAFE TO SHUT DOWN")
    return 0


def _write_process_state(profile, identity):
    state_paths(profile)[0].write_text(
        json.dumps({"schema": "symphony-pilot-process/v1", "identity": identity},
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
    release_awake_guard(profile)
    return True

def dashboard(profile):
    if not profile.dashboard_port:
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{profile.dashboard_port}/", timeout=2) as response:
            payload = response.read()
            try:
                return json.loads(payload)
            except ValueError:
                return {"state": "running"}
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None

def runtime_state(profile):
    if not profile.dashboard_port:
        return None
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{profile.dashboard_port}/api/v1/state", timeout=2
        ) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None

def active_issues(profile, token):
    issues = github(profile, token, "GET", "/issues?state=open&labels=" +
                    urllib.parse.quote(",".join(profile.dispatch_labels)) + "&per_page=100")
    return issues if isinstance(issues, list) else []

def start(profile):
    token = read_secret(profile)
    pid_path, log_path = state_paths(profile)
    if pid_path.exists():
        identity = read(pid_path)
        identity = identity.get("identity") if identity else None
        if identity and _identity_alive(identity):
            try:
                establish_awake_guard(profile)
            except RuntimeError as exc:
                print(f"Cannot keep CLEANROOM awake: {exc}")
                return 1
            print("Symphony is already running.")
            return 0
        pid_path.unlink(missing_ok=True)
    try:
        issues = active_issues(profile, token)
    except Exception as exc:
        print(f"Cannot verify GitHub dispatch state: {type(exc).__name__}. No process was started.")
        return 1
    if len(issues) > profile.max_concurrent_agents:
        print(f"Refusing to start: {len(issues)} dispatchable issues exceed the one-issue pilot limit.")
        return 1
    root = install_root(profile)
    workflow = root / "projects" / profile.slug / "WORKFLOW.md"
    if not workflow.exists():
        print(f"Deployment is missing its generated workflow: {workflow}")
        return 1
    try:
        establish_awake_guard(profile)
    except RuntimeError as exc:
        print(f"Cannot start CLEANROOM: {exc}")
        return 1
    env = os.environ.copy()
    env["SYMPHONY_PILOT_GITHUB_TOKEN"] = token
    env["SYMPHONY_PROFILE"] = str(root / "profile.toml")
    env["SYMPHONY_WORKFLOW"] = str(workflow)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    binary = os.environ.get("SYMPHONY_BIN")
    if not binary:
        candidates = sorted((root / "bin").glob("symphony-*"))
        binary = str(candidates[0]) if candidates else "symphony"
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
    _write_process_state(profile, identity)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if dashboard(profile) is not None:
            print("Symphony is running; dashboard is reachable.")
            return 0
        if child.poll() is not None:
            print("Symphony exited during startup. Review the project log.")
            pid_path.unlink(missing_ok=True)
            release_awake_guard(profile)
            return 1
        time.sleep(0.5)
    print("Symphony dashboard did not become reachable within 30 seconds; requesting normal shutdown.")
    if not _identity_alive(identity):
        if pid_alive(int(identity["pid"])):
            print("Startup identity could not be confirmed; PID and awake guard were retained for operator attention.")
            return 1
        pid_path.unlink(missing_ok=True)
        release_awake_guard(profile)
        print("Symphony exited during startup; awake guard released.")
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
    release_awake_guard(profile)
    print("Symphony exited after startup timeout; awake guard released.")
    return 1

def stop(profile, force=False):
    pid_path, _ = state_paths(profile)
    if not pid_path.exists():
        release_awake_guard(profile)
        print("Symphony is stopped - SAFE TO SHUT DOWN")
        return 0
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
        release_awake_guard(profile)
        print("Symphony is stopped - SAFE TO SHUT DOWN")
        return 0
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
        release_awake_guard(profile)
        print("STOPPED - SAFE TO SHUT DOWN")
        return 0
    view = runtime_state(profile)
    if view is None:
        print("Cannot finish safely: authoritative Symphony runtime state is unavailable.")
        return 1
    initial = {_issue_id(entry) for entry in _active_entries(view)} - {None}
    if len(initial) > 1:
        print("Cannot finish safely: more than one issue is active in the one-issue pilot.")
        return 1
    if initial:
        print("CLEANROOM is finishing; current work will be allowed to finish before Symphony stops.")
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
        release_awake_guard(profile)
        print("STOPPED - SAFE TO SHUT DOWN")
        return 0
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

def test(profile):
    root = install_root(profile)
    manifest_path = root / "DEPLOYMENT.json"
    required = [root / "profile.toml", manifest_path,
                root / "scripts" / "project.py", root / "runtime" / "prepare_workspace.py",
                root / "runtime" / "host_integration.py", root / "runtime" / "process_identity.py",
                root / "projects" / profile.slug / "WORKFLOW.md"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        print("Deployment validation failed; missing: " + ", ".join(missing))
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        cli_name = manifest.get("operator_cli")
        if cli_name != "scripts/project.py":
            raise ValueError("manifest operator_cli is not scripts/project.py")
        expected = manifest.get("files", {}).get(cli_name)
        actual = hashlib.sha256((root / cli_name).read_bytes()).hexdigest()
        if expected != actual:
            raise ValueError("deployed operator command does not match its manifest")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Deployment validation failed: {exc}")
        return 1
    print(f"Deployment validation passed for {profile.slug} at {root}")
    return 0

def main():
    parser = argparse.ArgumentParser(description="symphony-pilot project lifecycle")
    parser.add_argument("--profile", default=str(ROOT / "projects/cleanroom/profile.toml"))
    parser.add_argument("action", choices=("start", "status", "stop", "stop-now", "finish", "test"))
    args = parser.parse_args()
    profile = load_profile(pathlib.Path(args.profile))
    if args.action == "start": return start(profile)
    if args.action == "stop": return stop(profile)
    if args.action == "stop-now": return stop(profile, force=True)
    if args.action == "finish": return finish(profile)
    if args.action == "status": return status(profile)
    return test(profile)

if __name__ == "__main__":
    raise SystemExit(main())
