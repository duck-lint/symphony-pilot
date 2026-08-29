"""Small host integrations owned by the pilot, not by project adapters.

The WSL pilot invokes static PowerShell programs through the Windows interop
boundary. Dynamic issue text is passed as JSON on stdin, never interpolated
into executable PowerShell source.
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import time
from typing import Any

from process_identity import capture, matches, read
from prepare_workspace import Profile


AWAKE_STATE = "host-awake.json"
NOTIFICATION_STATE = "notifications.json"

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

_NOTIFY_SCRIPT = r"""
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction Stop
  $null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
  $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
  $escape = { param($value) [System.Security.SecurityElement]::Escape([string]$value) }
  $title = & $escape $payload.title
  $message = & $escape $payload.message
  $launch = if ($payload.url) { & $escape $payload.url } else { '' }
  $xml.LoadXml("<toast launch='$launch'><visual><binding template='ToastGeneric'><text>$title</text><text>$message</text></binding></visual></toast>")
  $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier([string]$payload.app).Show($toast)
} catch {
  Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
  Add-Type -AssemblyName System.Drawing -ErrorAction Stop
  $icon = New-Object System.Windows.Forms.NotifyIcon
  $icon.Icon = [System.Drawing.SystemIcons]::Information
  $icon.Visible = $true
  $icon.ShowBalloonTip(8000, [string]$payload.title, [string]$payload.message, [System.Windows.Forms.ToolTipIcon]::Info)
  Start-Sleep -Seconds 8
  $icon.Dispose()
}
""".strip()


def _state_path(profile: Profile, name: str) -> pathlib.Path:
    profile.state_root.mkdir(parents=True, exist_ok=True)
    return profile.state_root / name


def _identity_alive(identity: object) -> bool:
    return matches(identity)


def recover_awake_guard(profile: Profile) -> None:
    """Forget a dead helper after a crash/reboot; retain a live guard."""
    path = _state_path(profile, AWAKE_STATE)
    if not path.exists():
        return
    identity = read(path)
    if not identity or not _identity_alive(identity.get("identity")):
        path.unlink(missing_ok=True)
        return


def establish_awake_guard(profile: Profile) -> None:
    if not profile.prevent_host_sleep:
        return
    recover_awake_guard(profile)
    path = _state_path(profile, AWAKE_STATE)
    if path.exists():
        return
    try:
        helper = subprocess.Popen(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-WindowStyle", "Hidden", "-Command", _AWAKE_SCRIPT],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
        )
    except OSError as exc:
        raise RuntimeError(f"Windows host-awake backend unavailable: {exc}") from exc
    identity = capture(helper.pid)
    if identity is None:
        helper.terminate()
        helper.wait(timeout=5)
        raise RuntimeError("Windows host-awake helper identity could not be verified")
    path.write_text(json.dumps({"schema": "symphony-pilot-host-awake/v1",
                                "pid": helper.pid, "identity": identity,
                                "backend": "windows-execution-state"},
                               sort_keys=True) + "\n", encoding="utf-8")


def release_awake_guard(profile: Profile) -> None:
    path = _state_path(profile, AWAKE_STATE)
    if not path.exists():
        return
    state = read(path)
    if not state:
        path.unlink(missing_ok=True)
        return
    identity = state.get("identity")
    if not _identity_alive(identity):
        # A reused PID is not our helper and must never be terminated.
        path.unlink(missing_ok=True)
        return
    pid = int(identity["pid"])
    if _identity_alive(identity):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _identity_alive(identity):
            time.sleep(0.05)
    if not _identity_alive(identity):
        path.unlink(missing_ok=True)


def _safe_url(url: str | None, profile: Profile, issue: int) -> str:
    expected = f"https://github.com/{profile.repository}/issues/{issue}"
    return url if url == expected else expected


def _safe_summary(message: str) -> str:
    """Redact credential-shaped data without relying on English keywords."""
    import re

    value = str(message)
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                   "[REDACTED PRIVATE KEY]", value, flags=re.I | re.S)
    value = re.sub(r"(?:github_pat|gh[pousr]|xox[baprs])-?[A-Za-z0-9_\-]+",
                   "[REDACTED CREDENTIAL]", value, flags=re.I)
    value = re.sub(r"\b(?:sk|pk|api[_-]?key|access[_-]?token|client[_-]?secret)[-_:=][A-Za-z0-9_\-./]+",
                   "[REDACTED CREDENTIAL]", value, flags=re.I)
    value = re.sub(r"(https?://)([^/@\s]+):([^/@\s]+)@", r"\1[REDACTED]@", value, flags=re.I)
    value = re.sub(r"([?&](?:token|access_token|api_key|key|secret|password)=)[^&#\s]+",
                   r"\1[REDACTED]", value, flags=re.I)
    value = re.sub(r"\bBearer\s+[^\s]+", "Bearer [REDACTED]", value, flags=re.I)
    value = re.sub(r"\b(?:Authorization|X-Api-Key)\s*:\s*[^\r\n]*",
                   "[REDACTED AUTHORIZATION]", value, flags=re.I)
    return " ".join(value.split())[:240]


def _notification_key(event: str, issue: int) -> str:
    return f"{event}:GH-{issue}"


def _read_notification_state(path: pathlib.Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    import re
    return {key: value for key, value in raw.items()
            if isinstance(key, str) and re.fullmatch(r"(?:human|infrastructure|completed):GH-\d+", key)
            and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)}


def clear_notification(profile: Profile, event: str, issue: int) -> None:
    path = _state_path(profile, NOTIFICATION_STATE)
    state = _read_notification_state(path)
    state.pop(_notification_key(event, issue), None)
    if state:
        path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def notify(profile: Profile, event: str, issue: int, message: str,
           url: str | None = None, fingerprint: str | None = None) -> bool:
    """Best-effort, deduplicated notification; never changes project state."""
    if not profile.notifications_enabled or profile.notification_backend != "windows-toast":
        return False
    fingerprint = fingerprint or message
    path = _state_path(profile, NOTIFICATION_STATE)
    old = _read_notification_state(path)
    import hashlib
    safe_message = _safe_summary(message)
    safe_url = _safe_url(url, profile, issue)
    state_key = _notification_key(event, issue)
    active_fingerprint = hashlib.sha256(
        (event + "\0" + str(issue) + "\0" + fingerprint).encode()).hexdigest()
    if old.get(state_key) == active_fingerprint:
        return False
    titles = {
        "human": f"{profile.display_name} needs you",
        "infrastructure": f"{profile.display_name} infrastructure needs attention",
        "completed": f"{profile.display_name} issue completed",
    }
    payload: dict[str, Any] = {
        "app": profile.display_name or profile.slug,
        "title": titles.get(event, f"{profile.display_name} needs attention"),
        "message": safe_message,
        "url": safe_url,
    }
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-Command", _NOTIFY_SCRIPT],
            input=json.dumps(payload), text=True, capture_output=True, timeout=10,
            check=False,
        )
        if result.returncode:
            print(f"symphony-pilot notification warning: backend exit {result.returncode}")
            return False
        old[state_key] = active_fingerprint
        path.write_text(json.dumps(old, sort_keys=True) + "\n", encoding="utf-8")
        return True
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"symphony-pilot notification warning: {type(exc).__name__}")
        return False
