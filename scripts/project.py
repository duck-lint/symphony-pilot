#!/usr/bin/env python3
"""Operator-facing lifecycle controls for one Symphony project."""
from __future__ import annotations
import argparse
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
    except (ProcessLookupError, PermissionError):
        return False

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
        try:
            old = int(pid_path.read_text().strip())
        except ValueError:
            old = 0
        if old and pid_alive(old):
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
        print(f"Cannot start Symphony: {exc}. Check the official binary and deployment.")
        return 1
    finally:
        if not log.closed:
            log.close()
    pid_path.write_text(str(child.pid) + "\n", encoding="ascii")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if dashboard(profile) is not None:
            print("Symphony is running; dashboard is reachable.")
            return 0
        if child.poll() is not None:
            print("Symphony exited during startup. Review the project log.")
            pid_path.unlink(missing_ok=True)
            return 1
        time.sleep(0.5)
    print("Symphony started but its dashboard did not become reachable within 30 seconds.")
    return 1

def stop(profile, force=False):
    pid_path, _ = state_paths(profile)
    if not pid_path.exists():
        print("Symphony is stopped — SAFE TO SHUT DOWN")
        return 0
    try:
        pid = int(pid_path.read_text().strip())
    except ValueError:
        pid = 0
    if not pid or not pid_alive(pid):
        pid_path.unlink(missing_ok=True)
        print("Symphony is stopped — SAFE TO SHUT DOWN")
        return 0
    if not force:
        view = runtime_state(profile)
        if view is None:
            print("Cannot verify Symphony activity; the process was not stopped.")
            return 1
        if view.get("running") or view.get("retrying"):
            print("Finish/drain must be performed before stopping active work.")
            return 2
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.5)
    if pid_alive(pid):
        print("Stop requested, but Symphony has not exited; no force kill was attempted.")
        return 1
    pid_path.unlink(missing_ok=True)
    print("Symphony stopped — SAFE TO SHUT DOWN")
    return 0

def status(profile):
    pid_path, _ = state_paths(profile)
    running = False
    if pid_path.exists():
        try:
            running = pid_alive(int(pid_path.read_text().strip()))
        except ValueError:
            pass
    if not running:
        print("STOPPED — SAFE TO SHUT DOWN")
        return 0
    view = runtime_state(profile) or dashboard(profile)
    if isinstance(view, dict):
        state = str(view.get("state") or view.get("status") or "").lower()
        issue = view.get("issue") or view.get("issue_number") or "?"
        if "human" in state or "blocked" in state:
            print(f"NEEDS YOU #{issue} — WORK SAFELY PAUSED")
        elif "finish" in state or "drain" in state:
            print(f"FINISHING #{issue} — DO NOT SHUT DOWN YET")
        elif "work" in state or "run" in state:
            print(f"WORKING ON #{issue} — DO NOT SHUT DOWN")
        else:
            print("IDLE — SAFE TO STOP")
    else:
        print("WORKING — DO NOT SHUT DOWN")
    return 0

def test(profile):
    subprocess.run([sys.executable, str(ROOT / "scripts/validate_profile.py"),
                    str(ROOT / "projects" / profile.slug / "profile.toml")], check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts/deploy.py"),
                    "--profile", str(ROOT / "projects" / profile.slug / "profile.toml"), "--dry-run"], check=True)
    return 0

def main():
    parser = argparse.ArgumentParser(description="symphony-pilot project lifecycle")
    parser.add_argument("--profile", default=str(ROOT / "projects/cleanroom/profile.toml"))
    parser.add_argument("action", choices=("start", "stop", "stop-now", "status", "test"))
    args = parser.parse_args()
    profile = load_profile(pathlib.Path(args.profile))
    if args.action == "start": return start(profile)
    if args.action == "stop": return stop(profile)
    if args.action == "stop-now": return stop(profile, force=True)
    if args.action == "status": return status(profile)
    return test(profile)

if __name__ == "__main__":
    raise SystemExit(main())
