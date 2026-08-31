#!/usr/bin/env python3
"""Server-derived GitHub label-dispatch provenance."""
from __future__ import annotations

import datetime as dt
from typing import Callable


class DispatchProvenanceError(RuntimeError):
    pass


def _event_time(value: object) -> dt.datetime:
    if not isinstance(value, str):
        raise DispatchProvenanceError("GitHub dispatch event has no timestamp")
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DispatchProvenanceError("GitHub dispatch event timestamp is invalid") from exc


def prove_dispatch(issue: dict[str, object], events: list[dict[str, object]],
                   required_labels: tuple[str, ...], trusted_dispatchers: tuple[str, ...]) -> list[dict[str, object]]:
    """Prove each current dispatch label came from the latest trusted label event."""
    if issue.get("state") != "open":
        raise DispatchProvenanceError("issue is not open")
    current = {
        item.get("name") for item in (issue.get("labels") or [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    proven = []
    for label in required_labels:
        if label not in current:
            raise DispatchProvenanceError(f"required dispatch label is absent: {label}")
        applicable = [
            event for event in events
            if event.get("event") in {"labeled", "unlabeled"}
            and isinstance(event.get("label"), dict)
            and event["label"].get("name") == label
        ]
        if not applicable:
            raise DispatchProvenanceError(f"no server event proves dispatch label: {label}")
        def event_key(event):
            event_id = event.get("id")
            if not isinstance(event_id, int) or isinstance(event_id, bool) or event_id < 1:
                raise DispatchProvenanceError(f"dispatch event identity is invalid for label: {label}")
            return _event_time(event.get("created_at")), event_id
        latest = max(applicable, key=event_key)
        actor = (latest.get("actor") or {}).get("login") if isinstance(latest.get("actor"), dict) else None
        if latest.get("event") != "labeled" or not isinstance(actor, str) or actor not in trusted_dispatchers:
            raise DispatchProvenanceError(f"latest dispatch event is not trusted for label: {label}")
        event_id = latest.get("id")
        if not isinstance(event_id, int) or isinstance(event_id, bool) or event_id < 1:
            raise DispatchProvenanceError(f"dispatch event identity is invalid for label: {label}")
        created_at = latest.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            raise DispatchProvenanceError(f"dispatch event timestamp is invalid for label: {label}")
        proven.append({"label": label, "actor": actor, "event_id": event_id, "created_at": created_at})
    return proven


def fetch_all_events(fetch_page: Callable[[int], object]) -> list[dict[str, object]]:
    """Fetch every issue-event page; incomplete history is an admission block."""
    all_events: list[dict[str, object]] = []
    page = 1
    while True:
        value = fetch_page(page)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise DispatchProvenanceError("GitHub issue event history is malformed")
        all_events.extend(value)
        if len(value) < 100:
            return all_events
        page += 1
        if page > 100:
            raise DispatchProvenanceError("GitHub issue event history exceeded pagination bound")
