#!/usr/bin/env python3
"""Loopback-only HTTP boundary for the trusted Pilot control plane.

The browser supplies only registered slugs and existing task UUIDs.  Profile,
database, deployment, process, and runtime identities are resolved from
host-owned state on every request; none can be overridden by request JSON.
"""
from __future__ import annotations

import http.server
import ipaddress
import json
import pathlib
import re
import socket
import urllib.parse
from dataclasses import dataclass
from typing import Any

from control_db import ControlPlaneDatabase, ControlPlaneError
from prepare_workspace import deployment_path, project_namespaces
from process_identity import matches
from project_registry import resolve_project, validate_registry
from runtime_lock import RuntimeLockError, validate_lock


TASK_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.I | re.S),
    re.compile(r"(?:github_pat|gh[pousr]|sk)-?[A-Za-z0-9_\-]{12,}", re.I),
    re.compile(r"\bBearer\s+[^\s]+", re.I),
)
STATIC_ROOT = pathlib.Path(__file__).resolve().parents[1] / "web"


def redact(value: object) -> object:
    """Redact credential-shaped text even if it was accidentally persisted."""
    if isinstance(value, str):
        for pattern in SECRET_PATTERNS:
            value = pattern.sub("[REDACTED CREDENTIAL]", value)
        return value
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def validate_loopback_bind(host: str) -> str:
    """Accept literal loopback addresses only; hostnames invite DNS rebinding."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("control API bind must be a literal loopback address") from exc
    if not address.is_loopback:
        raise ValueError("control API bind must be loopback-only")
    return address.compressed


def _read_json_file(path: pathlib.Path) -> object | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


@dataclass
class HostControlApplication:
    registry_root: pathlib.Path
    database_path: pathlib.Path

    def projects(self) -> list[dict[str, Any]]:
        projects = []
        for profile in validate_registry(self.registry_root):
            with self._read_database() as database:
                tasks = database.list_tasks(project_slug=profile.slug)
            projects.append({
                "slug": profile.slug,
                "display_name": profile.display_name,
                "repository": profile.repository,
                "dashboard_port": profile.dashboard_port,
                "task_count": len(tasks),
                "execution": {"enabled": False, "reason": "activation architecture remains blocked"},
                "deployment": self._deployment_summary(profile),
                "process": self._process_summary(profile),
                "runtime": self._runtime_summary(profile),
            })
        return projects

    def tasks(self, slug: str) -> list[dict[str, object]]:
        self._profile(slug)
        with self._read_database() as database:
            return redact(database.list_tasks(project_slug=slug))  # type: ignore[return-value]

    def task(self, slug: str, task_id: str) -> dict[str, object]:
        self._profile(slug)
        self._validate_task_id(task_id)
        with self._read_database() as database:
            projection = database.read_projection(task_id)
        if projection["task"]["project_slug"] != slug:  # type: ignore[index]
            raise ApiError(404, "unknown_task", "task is not registered to this project")
        return redact(projection)  # type: ignore[return-value]

    def _read_database(self) -> ControlPlaneDatabase:
        return ControlPlaneDatabase.open_readonly(self.database_path)

    def _profile(self, slug: str):
        try:
            return resolve_project(slug, self.registry_root)
        except (ValueError, RuntimeError) as exc:
            raise ApiError(404, "unknown_project", "project is not registered") from exc

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not TASK_ID_RE.fullmatch(task_id):
            raise ApiError(400, "invalid_task_id", "task id must be a canonical lowercase UUID")

    def _deployment_summary(self, profile) -> dict[str, object]:
        root = pathlib.Path(deployment_path(profile))
        value = _read_json_file(root / "DEPLOYMENT.json")
        if not isinstance(value, dict):
            return {"state": "not_deployed", "path": str(root)}
        return {
            "state": "present", "path": str(root),
            "identity": value.get("deployment_identity"),
            "source_commit": value.get("source_commit"),
            "deployed_utc": value.get("deployed_utc"),
        }

    def _process_summary(self, profile) -> dict[str, object]:
        path = pathlib.Path(project_namespaces(profile)["process_state"])
        value = _read_json_file(path)
        identity = value.get("identity") if isinstance(value, dict) else None
        return {
            "state": "running" if matches(identity) else "stopped",
            "pid": identity.get("pid") if isinstance(identity, dict) else None,
            "record_path": str(path),
        }

    def _runtime_summary(self, profile) -> dict[str, object]:
        path = pathlib.Path(profile.state_root) / "runtime-lock.json"
        value = _read_json_file(path)
        try:
            lock = validate_lock(value)
        except RuntimeLockError:
            return {"state": "missing_or_invalid", "lock_path": str(path),
                    "live_verification": "not_performed"}
        summary = {
            "state": "recorded", "lock_valid": True, "lock_path": str(path),
            "live_verification": "not_performed",
        }
        for name in ("symphony", "codex", "containment"):
            summary[name] = {
                key: lock[name][key] for key in ("executable", "version", "sha256")
            }
        return summary


class HostControlServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, application: HostControlApplication):
        super().__init__(address, HostControlHandler)
        self.application = application
        host, port = self.server_address[:2]
        bracketed = f"[{host}]" if ":" in host else host
        self.trusted_host = f"{bracketed}:{port}"


class HostControlHandler(http.server.BaseHTTPRequestHandler):
    server: HostControlServer

    def parse_request(self) -> bool:
        """Reject DNS-rebinding Host values before dispatching any method."""
        if not super().parse_request():
            return False
        if self.headers.get("Host") != self.server.trusted_host:
            self.send_error(421, "HTTP Host does not name this loopback listener")
            return False
        return True

    def do_GET(self) -> None:
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/":
                body = (STATIC_ROOT / "index.html").read_bytes()
                self._send(200, body, "text/html; charset=utf-8")
            elif path in {"/app.js", "/style.css"}:
                content_type = "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
                self._send(200, (STATIC_ROOT / path[1:]).read_bytes(), content_type)
            elif path == "/api/v1/projects":
                self._json(200, {"projects": self.server.application.projects()})
            else:
                match = re.fullmatch(r"/api/v1/projects/([^/]+)/tasks(?:/([^/]+))?", path)
                if not match:
                    raise ApiError(404, "not_found", "route does not exist")
                slug, task_id = match.groups()
                result = (self.server.application.task(slug, task_id) if task_id
                          else {"tasks": self.server.application.tasks(slug)})
                self._json(200, result)
        except ApiError as exc:
            self._json(exc.status, {"error": exc.code, "message": str(exc)})
        except (ControlPlaneError, ValueError) as exc:
            self._json(400, {"error": "invalid_request", "message": str(exc)})

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value, sort_keys=True).encode(), "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int, application: HostControlApplication) -> HostControlServer:
    address = validate_loopback_bind(host)
    if ipaddress.ip_address(address).version == 6:
        class IPv6HostControlServer(HostControlServer):
            address_family = socket.AF_INET6
        return IPv6HostControlServer((address, port), application)
    return HostControlServer((address, port), application)
