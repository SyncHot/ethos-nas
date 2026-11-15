"""EthOS NAS — Notification WebSocket Router

WS /ws/notifications — push notification changes to every open client so the
bell badge and the Notification Center sheet update without polling.

Message envelope (JSON objects, `{"type": ...}` per docs/api-conventions.md):

    {"type": "event",    "event": {...},  "unread": 3}   new or deduped event
    {"type": "read",     "event": {"id"}, "unread": 2}   one event marked read
    {"type": "read-all",                  "unread": 0}   everything marked read
    {"type": "delete",   "event": {"id"}, "unread": 1}   one event deleted
    {"type": "cleared",                   "unread": 0}   history emptied
    {"type": "sync",                      "unread": 0}   sent once on connect

Auth: `?token=<jwt>` (the browser cannot set headers on a WebSocket). Missing or
invalid token closes with **4001**; clients stop reconnecting on that code.
This stream is available to every authenticated user — the notification store is
global, and only admins may publish to it (enforced in the REST router).
"""

import asyncio
import json as _json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.services import notifications_service, task_registry

logger = logging.getLogger("ethos-nas.routers.notifications_ws")
router = APIRouter()


class _NotificationsHub:
    """Fan-out to every connected notification client."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.append(websocket)
        logger.info("Notifications WS client connected (total: %d)", len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self._connections:
            self._connections.remove(websocket)
        logger.info("Notifications WS client disconnected (remaining: %d)", len(self._connections))

    async def broadcast(self, payload: dict) -> None:
        """Send one payload to every client; drop dead connections."""
        message = _json.dumps(payload, ensure_ascii=False)
        dead: list[WebSocket] = []
        for websocket in list(self._connections):
            try:
                await websocket.send_text(message)
            except Exception as exc:  # noqa: BLE001 — a dead client is not an error
                logger.debug("Failed to send notification to WS client: %s", exc)
                dead.append(websocket)
        for websocket in dead:
            self.disconnect(websocket)

    @property
    def count(self) -> int:
        return len(self._connections)


_hub = _NotificationsHub()


@router.websocket("/ws/notifications")
async def websocket_notifications(websocket: WebSocket) -> None:
    """Stream notification changes to the bell + sheet."""
    token = websocket.query_params.get("token") or websocket.cookies.get("token") or ""
    from backend.routers.auth import _verify_token

    payload = _verify_token(token)
    if not payload:
        # Close before accept: the client sees 4001 and stops retrying.
        await websocket.close(code=4001, reason="Authentication required")
        return

    await _hub.connect(websocket)
    try:
        # Initial snapshot so a freshly opened tab shows the right badge even if
        # it connected between two broadcasts.
        try:
            await websocket.send_text(_json.dumps({
                "type": "sync",
                "unread": notifications_service.unread_count(),
            }))
        except Exception:
            logger.debug("Could not send notification sync frame", exc_info=True)

        while True:
            # Clients may send {"type":"ping"}; anything else is ignored.
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info("Notifications WS connection closed")
    except Exception:
        logger.error("WS /ws/notifications failed", exc_info=True)
        try:
            await websocket.close(code=1011)
        except Exception:
            logger.debug("Could not close notification WS", exc_info=True)
    finally:
        _hub.disconnect(websocket)


# ── Service → WebSocket bridge (registered from main.py lifespan) ────────────

def setup_broadcaster() -> None:
    """Subscribe the hub to notifications_service changes.

    `emit_event` runs on worker threads (download engine, health monitor, motion
    detection), so the callback must hop onto the app loop with
    `task_registry.spawn_on_loop` — a bare `asyncio.create_task` from a thread
    raises "no running event loop".
    """
    loop = asyncio.get_running_loop()

    def _on_change(payload: dict) -> None:
        task_registry.spawn_on_loop(
            loop, lambda: _hub.broadcast(payload), name="notification-broadcast"
        )

    notifications_service.register_listener(_on_change)
    logger.info("Notification Center broadcaster registered")
