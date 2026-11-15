"""EthOS NAS — File Events WebSocket Router

WS /ws/files/events — Stream file mutation events to connected clients.
Clients subscribe and receive broadcasted mutations for directory-level reactive sync.
"""

import asyncio
import json as _json
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.services import task_registry

logger = logging.getLogger("ethos-nas.routers.file_events_ws")
router = APIRouter()


class _WsConnectionManager:
    """Manages active WebSocket connections for file event streaming.

    Each connected client receives every mutation event broadcast by the FileOperationsService.
    Dead connections are cleaned up automatically after failed sends.
    """

    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        """Accept a new client connection and add it to the manager."""
        await websocket.accept()
        self._connections.append(websocket)
        logger.info("File events WS client connected (total: %d)", len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a client from the manager when it disconnects or fails."""
        if websocket in self._connections:
            self._connections.remove(websocket)
        logger.info("File events WS client disconnected (remaining: %d)", len(self._connections))

    async def broadcast(self, event: dict) -> None:
        """Send mutation event to all connected clients.

        Dead connections (failed sends) are removed automatically.
        Uses create_task so the broadcast doesn't block if a send is slow.
        """
        msg = _json.dumps(event)
        dead: list[WebSocket] = []

        for ws in self._connections:
            try:
                await ws.send_text(msg)
            except Exception as exc:
                logger.debug("Failed to send to WS client: %s", exc)
                dead.append(ws)

        # Clean up dead connections
        for ws in dead:
            self.disconnect(ws)

    @property
    def count(self) -> int:
        """Return the number of currently connected clients."""
        return len(self._connections)


_ws_manager = _WsConnectionManager()


@router.websocket("/ws/files/events")
async def websocket_file_events(websocket: WebSocket):
    """WebSocket endpoint for file mutation event streaming.

    Clients connect and receive real-time broadcasts of file operations
    (copy, move, delete, zip, upload) so the File Manager can perform
    silent background refetches when affected directories change.

    The connection stays open until the client disconnects or the server shuts down.
    """
    # Validate JWT from query params before accepting connection (same as /ws/terminal)
    token = websocket.url.query.split("token=")[-1] if "token=" in websocket.url.query else ""
    from backend.routers.auth import _verify_token
    payload = _verify_token(token)
    if not payload:
        await websocket.close(code=4001, reason="Authentication required")
        return
    await _ws_manager.connect(websocket)
    try:
        # Keep the connection alive — accept incoming messages (ping/pong style)
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info("File events WS connection closed")
    finally:
        _ws_manager.disconnect(websocket)


def broadcast_file_event(event: dict) -> None:
    """Called after successful file mutations to push events to connected clients.

    This is the bridge between files_service.broadcast_mutation and WebSocket transport.
    Called from the router layer after service methods return successfully.
    Uses create_task so the broadcast doesn't block the response handler.
    """
    task_registry.spawn(_ws_manager.broadcast(event), name="file-event-broadcast")


# ── Service → WS bridge (registered at app startup in main.py lifespan) ─────

from backend.services.files_service import on_broadcast as _register_broadcaster_callback


def _setup_event_broadcaster() -> None:
    """Connect files_service events to WebSocket transport.

    Registers a callback that pushes each mutation event to all connected
    WebSocket clients via the WS manager. Called once during app startup in
    main.py's lifespan function (so the running loop can be captured).

    broadcast_mutation() runs in worker threads (asyncio.to_thread), so the
    callback must hop onto the app loop with call_soon_threadsafe — a bare
    asyncio.create_task() from a thread raises "no running event loop".
    The event dict is passed as-is: _ws_manager.broadcast() does the JSON
    encoding itself (pre-dumping it here double-encoded every message).
    """
    loop = asyncio.get_running_loop()

    def _ws_callback(event: dict) -> None:
        try:
            task_registry.spawn_on_loop(
                loop, lambda: _ws_manager.broadcast(event), name="file-event-broadcast"
            )
        except RuntimeError:
            pass  # loop closed during shutdown — nothing to deliver

    # Register the callback so every broadcast_mutation() push reaches WS clients
    try:
        _register_broadcaster_callback(_ws_callback)
    except ImportError:
        pass  # on_broadcast not yet available — will be added in files_service.py


__all__ = ["broadcast_file_event", "_ws_manager", "_setup_event_broadcaster"]
