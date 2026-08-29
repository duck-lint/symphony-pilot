"""PID identity checks for pilot-owned processes on Linux/WSL."""
from __future__ import annotations

import json
import pathlib
from typing import Any


def _boot_id() -> str | None:
    try:
        return pathlib.Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None


def _start_time(pid: int) -> str | None:
    try:
        text = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        closing = text.rfind(")")
        if closing < 0:
            return None
        fields = text[closing + 2:].split()
        return fields[19] if len(fields) > 19 else None
    except (OSError, UnicodeError, ValueError):
        return None


def capture(pid: int) -> dict[str, Any] | None:
    boot = _boot_id()
    start = _start_time(pid)
    if not boot or not start:
        return None
    # The official Burrito launcher replaces itself with beam.smp. Executable
    # and cmdline are therefore not stable across the initial exec boundary;
    # boot ID plus kernel process start time are the managed identity.
    return {"pid": pid, "boot_id": boot, "start_time": start}


def matches(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    try:
        pid = int(record["pid"])
    except (KeyError, TypeError, ValueError):
        return False
    current = capture(pid)
    if not current:
        return False
    return (current["boot_id"] == record.get("boot_id") and
            current["start_time"] == record.get("start_time") and
            (not record.get("executable") or current.get("executable") == record.get("executable")) and
            (not record.get("cmdline_sha256") or
             current.get("cmdline_sha256") == record.get("cmdline_sha256")))


def read(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
