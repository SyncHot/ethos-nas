"""EthOS NAS — Notification Center Router

REST (mounted at /api/notifications):
- GET    /                        — list events (newest first, optional kind filter) + unread
- GET    /unread-count            — {unread} for the bell badge (WebSocket fallback)
- POST   /                        — create an event (packages: package_id, admins: any source)
- POST   /read-all                — mark every event read
- POST   /{event_id}/read         — mark one event read
- DELETE /{event_id}              — delete one event
- DELETE /                         — clear all events

Backend subsystems publish through backend.services.notifications_service.emit_event().
Package UIs (native modules) publish through `POST /api/notifications` with their
`package_id`; the server derives `source="pkg:<id>"` after verifying the package is
installed, so a package cannot impersonate another source. Clients receive live
updates on `/ws/notifications` (see notifications_ws.py).
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.services import packages_store
from backend.services.notifications_service import (
    clear_all,
    delete_event,
    emit_event,
    list_events,
    mark_all_read,
    mark_read,
    unread_count,
)

logger = logging.getLogger("ethos-nas.routers.notifications")
router = APIRouter()


class NotificationIn(BaseModel):
    """Body for `POST /api/notifications`.

    `source` is ignored when `package_id` is given (derived server-side).
    """

    kind: Literal["success", "error", "warning", "info"] = "info"
    title: str = Field(min_length=1, max_length=200)
    message: str = Field(default="", max_length=1000)
    source: str = Field(default="", max_length=60)
    package_id: Optional[str] = Field(default=None, max_length=60)
    action: Optional[dict] = None
    dedupe_key: Optional[str] = Field(default=None, max_length=120)


def _caller(request: Request) -> dict:
    return getattr(request.state, "current_user", None) or {}


@router.get("")
def get_notifications(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    kind: Optional[str] = Query(default=None, pattern="^(success|error|warning|info)$"),
) -> dict:
    """List events visible to the caller (newest first) plus their unread count."""
    username = str(_caller(request).get("username") or "")
    events, unread = list_events(limit, kind, username)
    return {"events": events, "unread": unread}


@router.post("")
def create_notification(payload: NotificationIn, request: Request) -> dict:
    """Create a notification from a package UI or (admins) from anywhere."""
    if payload.package_id:
        if not packages_store.get_package(payload.package_id):
            raise HTTPException(status_code=404,
                                detail=f"Package {payload.package_id} is not installed")
        source = f"pkg:{payload.package_id}"
    else:
        if str(_caller(request).get("role") or "user") != "admin":
            logger.warning("Non-admin notification publish denied for %s",
                           _caller(request).get("username", "?"))
            raise HTTPException(status_code=403, detail="Admin access required")
        source = payload.source or "system"

    event = emit_event(
        payload.kind,
        payload.title,
        payload.message,
        source=source,
        action=payload.action,
        dedupe_key=payload.dedupe_key,
    )
    if event is None:
        raise HTTPException(status_code=500, detail="Could not store the notification")
    return {"ok": True, "event": event}


@router.get("/unread-count")
def get_unread_count(request: Request) -> dict:
    """Lightweight poll target for the bell badge (fallback when the WS is down)."""
    username = str(_caller(request).get("username") or "")
    return {"unread": unread_count(username)}


@router.post("/read-all")
def read_all(request: Request) -> dict:
    marked = mark_all_read(str(_caller(request).get("username") or ""))
    return {"ok": True, "marked": marked}


@router.delete("")
def clear_notifications() -> dict:
    cleared = clear_all()
    logger.info("notifications cleared (%d events)", cleared)
    return {"ok": True, "cleared": cleared}


@router.post("/{event_id}/read")
def read_event(event_id: str, request: Request) -> dict:
    if not mark_read(event_id, str(_caller(request).get("username") or "")):
        raise HTTPException(status_code=404, detail=f"Notification {event_id} not found")
    return {"ok": True}


@router.delete("/{event_id}")
def delete_notification(event_id: str) -> dict:
    if not delete_event(event_id):
        raise HTTPException(status_code=404, detail=f"Notification {event_id} not found")
    return {"ok": True}
