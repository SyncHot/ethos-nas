"""EthOS NAS — System Notification Store (global Notification Center)

One store for every user-visible system event (download completions/failures,
disk warnings, package installs, firewall changes, …). It backs the bell +
right-edge Notification Center sheet in `core/notification-center.js` and is the
only notification history in the product — apps must not build their own.

Store file: <DATA_DIR>/notifications.json   {"version": 2, "events": [...]}

Event shape (v2):
    id          "evt_<12 hex>"
    kind        "success" | "error" | "warning" | "info"
    title       str (<=200)
    message     str (<=1000)
    source      str (<=60, e.g. "downloads", "pkg:<package-id>")
    created_at  ISO-8601 UTC
    read        bool            — global flag, shared by every account
    read_by     list[str]       — reserved for per-user read state
    action      {"label", "appId"?, "arg"?, "href"?} | None — row/action button
    dedupe_key  str | None      — repeats inside DEDUPE_WINDOW_SECONDS collapse
    count       int             — occurrences the row represents

Threading: events are emitted from worker threads (download engine, health
monitor, motion detection), so every public function is thread-safe and
`emit_event` never raises — a notification failure must never break the
subsystem that reported it.

Push: `register_listener(cb)` lets the WebSocket router subscribe to
create/read/delete changes. Listeners run on the emitting thread and must not
raise; failures are logged and ignored.
"""

import logging
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from backend.config import settings
from backend.services import json_store

logger = logging.getLogger("ethos-nas.services.notifications_service")

DATA_FILE = Path(settings.DATA_DIR) / "notifications.json"
SCHEMA_VERSION = 2
MAX_EVENTS = 500                 # hard cap — oldest events are trimmed first
MAX_TITLE = 200
MAX_MESSAGE = 1000
MAX_SOURCE = 60
DEDUPE_WINDOW_SECONDS = 300      # repeats of one dedupe_key collapse into one row
VALID_KINDS = ("success", "error", "warning", "info")

_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,59}$")
_HREF_RE = re.compile(r"^/[^\s]{0,300}$")

_LISTENERS: list[Callable[[dict], None]] = []
_LISTENERS_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _default_store() -> dict:
    return {"version": SCHEMA_VERSION, "events": []}


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _migrate(from_version: int, to_version: int, data: Any) -> Any:
    """v1 → v2: additive fields only (action, dedupe_key, count, read_by).

    Malformed entries are dropped instead of failing the whole upgrade — losing
    one notification is better than an empty Notification Center.
    """
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("notifications store is not an object with an events list")
    clean: list[dict] = []
    for event in data["events"]:
        if not isinstance(event, dict) or not event.get("id"):
            continue
        kind = event.get("kind")
        event["kind"] = kind if kind in VALID_KINDS else "info"
        event["title"] = str(event.get("title") or "Notification")[:MAX_TITLE]
        event["message"] = str(event.get("message") or "")[:MAX_MESSAGE]
        event["source"] = str(event.get("source") or "system")[:MAX_SOURCE]
        event.setdefault("created_at", _now_iso())
        event["read"] = bool(event.get("read", False))
        event.setdefault("read_by", [])
        event.setdefault("action", None)
        event.setdefault("dedupe_key", None)
        event.setdefault("count", 1)
        clean.append(event)
    data["events"] = clean
    logger.info("notifications store migrated v%s→v%s (%d events kept)",
                from_version, to_version, len(clean))
    return data


_STORE = json_store.get(
    DATA_FILE,
    _default_store,
    version=SCHEMA_VERSION,
    migrate=_migrate,
)


# ── Listener registry (service → WebSocket bridge) ───────────────────────────

def register_listener(callback: Callable[[dict], None]) -> None:
    """Subscribe to change payloads: {type, event?, unread}.

    Called from the thread that performed the change. Failures are contained by
    `_notify`, so a subscriber can never break the emitting subsystem.
    """
    with _LISTENERS_LOCK:
        if callback not in _LISTENERS:
            _LISTENERS.append(callback)


def unregister_listener(callback: Callable[[dict], None]) -> None:
    with _LISTENERS_LOCK:
        if callback in _LISTENERS:
            _LISTENERS.remove(callback)


def _notify(payload: dict) -> None:
    with _LISTENERS_LOCK:
        listeners = list(_LISTENERS)
    for callback in listeners:
        try:
            callback(payload)
        except Exception:  # noqa: BLE001 — a bad listener must not break the emit
            logger.warning("notification listener failed", exc_info=True)


# ── Sanitising helpers ───────────────────────────────────────────────────────

def _clean_action(action: Any) -> dict | None:
    """Validate the optional action button attached to an event."""
    if not isinstance(action, dict):
        return None
    label = str(action.get("label") or "").strip()[:40]
    if not label:
        return None
    cleaned: dict[str, str] = {"label": label}
    app_id = str(action.get("appId") or "").strip()
    if app_id:
        if not _APP_ID_RE.match(app_id):
            return None
        cleaned["appId"] = app_id
    arg = str(action.get("arg") or "").strip()
    if arg:
        cleaned["arg"] = arg[:200]
    href = str(action.get("href") or "").strip()
    if href:
        # Same-origin absolute paths only — never an open redirect.
        if not _HREF_RE.match(href):
            return None
        cleaned["href"] = href
    if "appId" not in cleaned and "href" not in cleaned:
        return None
    return cleaned


def _clean_source(source: Any) -> str:
    return str(source or "").strip()[:MAX_SOURCE] or "system"


# ── Public API (all thread-safe; emit_event never raises) ────────────────────

def emit_event(
    kind: str,
    title: str,
    message: str = "",
    source: str = "system",
    *,
    action: dict | None = None,
    dedupe_key: str | None = None,
) -> dict | None:
    """Append a system event and persist it. Returns the event dict, or None on failure.

    Positional arguments are unchanged from v1, so every existing call site keeps
    working. Pass `dedupe_key` for repeating conditions (a hot disk, one failing
    mount) so the panel shows a single row with `count` instead of a wall of
    duplicates.
    """
    try:
        safe_kind = kind if kind in VALID_KINDS else "info"
        safe_title = str(title).strip()[:MAX_TITLE] or "Notification"
        safe_message = str(message).strip()[:MAX_MESSAGE]
        safe_source = _clean_source(source)
        safe_action = _clean_action(action)
        key = str(dedupe_key or "").strip()[:120] or None
        created = _now()
        created_iso = created.isoformat(timespec="seconds")

        def _mutate(store: dict) -> dict:
            events: list[dict] = store["events"]
            if key is not None:
                cutoff = created - timedelta(seconds=DEDUPE_WINDOW_SECONDS)
                for existing in reversed(events):
                    if existing.get("dedupe_key") != key:
                        continue
                    if existing.get("source") != safe_source:
                        continue
                    seen = _parse_ts(existing.get("created_at"))
                    if seen is None or seen < cutoff:
                        break
                    existing["count"] = int(existing.get("count") or 1) + 1
                    existing["created_at"] = created_iso
                    existing["title"] = safe_title
                    existing["message"] = safe_message
                    existing["kind"] = safe_kind
                    existing["read"] = False
                    existing["read_by"] = []
                    if safe_action:
                        existing["action"] = safe_action
                    return dict(existing)

            event = {
                "id": f"evt_{uuid.uuid4().hex[:12]}",
                "kind": safe_kind,
                "title": safe_title,
                "message": safe_message,
                "source": safe_source,
                "created_at": created_iso,
                "read": False,
                "read_by": [],
                "action": safe_action,
                "dedupe_key": key,
                "count": 1,
            }
            events.append(event)
            if len(events) > MAX_EVENTS:
                del events[: len(events) - MAX_EVENTS]
            return dict(event)

        event = _STORE.update(_mutate)
        _notify({"type": "event", "event": event, "unread": unread_count()})
        return event
    except Exception as exc:  # noqa: BLE001 — never break the reporting subsystem
        logger.warning("notifications emit failed: %s", exc, exc_info=True)
        return None


def list_events(limit: int = 100, kind: str | None = None) -> tuple[list[dict], int]:
    """Return (events newest-first, total unread count across the whole store).

    `kind` optionally filters to a single kind (the panel's "Errors" chip).
    """
    store = _STORE.load()
    events = store.get("events") or []
    # Reversed first: timestamps are second-granular, so this keeps
    # newest-inserted-first ordering for events emitted within the same second
    # (the sort is stable).
    ordered = sorted(reversed(events), key=lambda e: e.get("created_at", ""), reverse=True)
    if kind:
        ordered = [e for e in ordered if e.get("kind") == kind]
    unread = sum(1 for e in events if not e.get("read"))
    return [dict(e) for e in ordered[:limit]], unread


def unread_count() -> int:
    return sum(1 for e in (_STORE.load().get("events") or []) if not e.get("read"))


def mark_read(event_id: str, user: str = "") -> bool:
    """Mark one event read. Returns False when the id is unknown."""
    def _mutate(store: dict) -> bool:
        for event in store["events"]:
            if event.get("id") != event_id:
                continue
            event["read"] = True
            if user and user not in (event.get("read_by") or []):
                event.setdefault("read_by", []).append(user)
            return True
        return False

    if not _STORE.update(_mutate):
        return False
    _notify({"type": "read", "event": {"id": event_id}, "unread": unread_count()})
    return True


def mark_all_read(user: str = "") -> int:
    """Mark every stored event read. Returns how many were newly marked."""
    def _mutate(store: dict) -> int:
        count = 0
        for event in store["events"]:
            if not event.get("read"):
                event["read"] = True
                count += 1
            if user and user not in (event.get("read_by") or []):
                event.setdefault("read_by", []).append(user)
        return count

    marked = _STORE.update(_mutate)
    if marked:
        _notify({"type": "read-all", "unread": 0})
    return marked


def delete_event(event_id: str) -> bool:
    """Delete one event. Returns False when the id is unknown."""
    def _mutate(store: dict) -> bool:
        before = len(store["events"])
        store["events"] = [e for e in store["events"] if e.get("id") != event_id]
        return len(store["events"]) != before

    removed = _STORE.update(_mutate)
    if removed:
        _notify({"type": "delete", "event": {"id": event_id}, "unread": unread_count()})
    return removed


def clear_all() -> int:
    """Delete every stored event. Returns how many were removed."""
    def _mutate(store: dict) -> int:
        cleared = len(store["events"])
        store["events"] = []
        return cleared

    cleared = _STORE.update(_mutate)
    if cleared:
        _notify({"type": "cleared", "unread": 0})
    return cleared
