"""Small host integration for the optional awake guard.

This module has no task, lifecycle, or project-state semantics. It only
maintains the identity of one operator-requested Windows helper.
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import time
from typing import Any

from process_identity import capture, matches
from prepare_workspace import PreparationError, Profile, require_physical_namespace

AWAKE_STATE = "host-awake.json"
AWAKE_SCHEMA = "symphony-pilot-host-awake/v1"
AWAKE_BACKEND = "windows-execution-state"

_AWAKE_SCRIPT = r"""
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class SymphonyPower {
  [DllImport("kernel32.dll")]
  public static extern uint SetThreadExecutionState(uint flags);
}
'@
[void][SymphonyPower]::SetThreadExecutionState(0x80000001)
try { while ($true) { Start-Sleep -Seconds 30 } }
finally { [void][SymphonyPower]::SetThreadExecutionState(0x80000000) }
""".strip()


def _state_path(profile: Profile) -> pathlib.Path:
    state_root = require_physical_namespace(profile.state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    return state_root / AWAKE_STATE


def _identity_alive(identity: object) -> bool:
    return matches(identity)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_awake_state(path: pathlib.Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise PreparationError("awake_state", "host-awake state is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PreparationError("awake_state", "host-awake state is malformed") from exc
    identity = value.get("identity") if isinstance(value, dict) else None
    pid = value.get("pid") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) != {"schema", "pid", "identity", "backend"} or
            value["schema"] != AWAKE_SCHEMA or value["backend"] != AWAKE_BACKEND or
            not isinstance(pid, int) or isinstance(pid, bool) or pid < 1 or
            not isinstance(identity, dict) or set(identity) != {"pid", "boot_id", "start_time"} or
            identity.get("pid") != pid or not isinstance(identity.get("boot_id"), str) or
            not isinstance(identity.get("start_time"), str)):
        raise PreparationError("awake_state", "host-awake state identity is invalid")
    return value


def recover_awake_guard(profile: Profile) -> None:
    path = _state_path(profile)
    state = _read_awake_state(path)
    if state is None:
        return
    if _identity_alive(state["identity"]):
        return
    if _pid_alive(state["pid"]):
        raise PreparationError("awake_state", "host-awake helper identity is stale or reused; state was retained")
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise PreparationError("awake_state", "stale host-awake state could not be removed") from exc


def establish_awake_guard(profile: Profile) -> None:
    if not profile.prevent_host_sleep:
        return
    recover_awake_guard(profile)
    path = _state_path(profile)
    if path.exists():
        return
    try:
        helper = subprocess.Popen(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _AWAKE_SCRIPT],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except OSError as exc:
        raise RuntimeError(f"Windows host-awake backend unavailable: {exc}") from exc
    identity = capture(helper.pid)
    if identity is None:
        helper.terminate()
        helper.wait(timeout=5)
        raise RuntimeError("Windows host-awake helper identity could not be verified")
    path.write_text(json.dumps({"schema": AWAKE_SCHEMA, "pid": helper.pid, "identity": identity, "backend": AWAKE_BACKEND}, sort_keys=True) + "\n", encoding="utf-8")


def release_awake_guard_at(path: pathlib.Path) -> bool:
    try:
        state = _read_awake_state(path)
    except PreparationError:
        return False
    if state is None:
        return True
    identity, pid = state["identity"], state["pid"]
    if not _identity_alive(identity):
        if _pid_alive(pid):
            return False
        path.unlink(missing_ok=True)
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except (PermissionError, OSError):
        return False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _identity_alive(identity):
        time.sleep(0.05)
    if _identity_alive(identity) or _pid_alive(pid):
        return False
    path.unlink(missing_ok=True)
    return True


def release_awake_guard(profile: Profile) -> bool:
    return release_awake_guard_at(_state_path(profile))
