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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def recover_awake_guard(profile: Profile) -> None:
    """Forget a dead helper after a crash/reboot; retain a live guard."""
    path = _state_path(profile, AWAKE_STATE)
    if not path.exists():
        return
    try:
        pid = int(json.loads(path.read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return
    if not _pid_alive(pid):
        path.unlink(missing_ok=True)


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
    path.write_text(json.dumps({"schema": "symphony-pilot-host-awake/v1",
                                "pid": helper.pid, "backend": "windows-execution-state"},
                               sort_keys=True) + "\n", encoding="utf-8")


def release_awake_guard(profile: Profile) -> None:
    path = _state_path(profile, AWAKE_STATE)
    if not path.exists():
        return
    try:
        pid = int(json.loads(path.read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.05)
    if not _pid_alive(pid):
        path.unlink(missing_ok=True)


def _safe_summary(message: str) -> str:
    lowered = message.lower()
    if any(secret in lowered for secret in ("token", "authorization", "bearer", "password")):
        return "The pilot stopped because an infrastructure service needs attention."
    return " ".join(message.split())[:240]


def notify(profile: Profile, event: str, issue: int, message: str,
           url: str | None = None, fingerprint: str | None = None) -> bool:
    """Best-effort, deduplicated notification; never changes project state."""
    if not profile.notifications_enabled or profile.notification_backend != "windows-toast":
        return False
    fingerprint = fingerprint or f"{event}:GH-{issue}:{message}"
    path = _state_path(profile, NOTIFICATION_STATE)
    try:
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError, TypeError):
        old = {}
    if old.get(event) == fingerprint:
        return False
    titles = {
        "human": f"{profile.display_name} needs you",
        "infrastructure": f"{profile.display_name} infrastructure needs attention",
        "completed": f"{profile.display_name} issue completed",
    }
    payload: dict[str, Any] = {
        "app": profile.display_name or profile.slug,
        "title": titles.get(event, f"{profile.display_name} needs attention"),
        "message": _safe_summary(message),
        "url": url or f"https://github.com/{profile.repository}/issues/{issue}",
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
        old[event] = fingerprint
        path.write_text(json.dumps(old, sort_keys=True) + "\n", encoding="utf-8")
        return True
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"symphony-pilot notification warning: {type(exc).__name__}")
        return False
