"""EthOS NAS — one Logs WebSocket for every source (docs/logs.md).

    /ws/logs?source=app
    /ws/logs?source=container:<name>&tail=200
    /ws/logs?source=file:/var/log/syslog&tail=200
    /ws/logs?source=journal&unit=ethos-nas&tail=200

JSON frames only (`docs/api-conventions.md`): ``entry`` | ``line`` | ``end`` |
``truncated`` | ``error``. Legacy paths ``/ws/logs/system`` and
``/ws/logs/{container}`` are served by this module too, so every log socket now
has the same auth and frame contract.

Auth: ``?token=<jwt>``. A missing/invalid token closes with **4001**; a valid
non-admin token closes with **4003**. Host files, containers and the journal are
diagnostic data (auth.log, env values) and stay admin-only, matching the HTTP
router. ``core/ws.js`` stops reconnecting on both codes.
"""

import asyncio
import json
import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.services import log_service, task_registry

logger = logging.getLogger("ethos-nas.routers.logs_ws")
router = APIRouter()

CLOSE_UNAUTHORISED = 4001
CLOSE_FORBIDDEN = 4003
CLOSE_NOT_FOUND = 4004
CLOSE_ERROR = 1011

_MAX_LINES = 5000          # per connection, then `truncated` and a clean stop
_MAX_TAIL = 5000
_ALLOWED_LOG_DIRS = ("/var/log", "/tmp")
_CONTAINER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]{0,62}$")
_UNIT_RE = re.compile(r"^[A-Za-z0-9@._\-]{1,64}$")

_BROADCASTERS: list = []


# ── helpers ──────────────────────────────────────────────────────────────────

async def _send(websocket: WebSocket, payload: dict) -> None:
    try:
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — a dead client is not an error
        logger.debug("log ws: send failed", exc_info=True)


async def _authenticate(websocket: WebSocket) -> Optional[dict]:
    """Verify the query token and the admin role. Closes on failure."""
    from backend.routers.auth import _verify_token

    token = (websocket.query_params.get("token") or "").strip()
    payload = _verify_token(token)
    if not payload:
        await websocket.close(code=CLOSE_UNAUTHORISED, reason="Authentication required")
        return None
    if str(payload.get("role") or "user") != "admin":
        await websocket.close(code=CLOSE_FORBIDDEN, reason="Admin access required")
        return None
    return payload


def _tail_param(websocket: WebSocket) -> int:
    try:
        value = int(websocket.query_params.get("tail", "200"))
    except (TypeError, ValueError):
        value = 200
    return max(1, min(value, _MAX_TAIL))


# ── hubs ────────────────────────────────────────────────────────────────────

class _LogHub:
    """Fan-out for live app events (`source=app`)."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.append(websocket)
        logger.info("Logs WS client connected (total: %d)", len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self._connections:
            self._connections.remove(websocket)
        logger.info("Logs WS client disconnected (remaining: %d)", len(self._connections))

    async def broadcast(self, payload: dict) -> None:
        message = json.dumps(payload, ensure_ascii=False)
        dead = []
        for websocket in list(self._connections):
            try:
                await websocket.send_text(message)
            except Exception as exc:  # noqa: BLE001 — dead client
                logger.debug("log ws broadcast failed: %s", exc)
                dead.append(websocket)
        for websocket in dead:
            self.disconnect(websocket)

    @property
    def count(self) -> int:
        return len(self._connections)


_hub = _LogHub()


def setup_broadcaster() -> None:
    """Subscribe the hub to unified-store entries (called from main lifespan)."""
    loop = asyncio.get_running_loop()

    def _on_entry(payload: dict) -> None:
        task_registry.spawn_on_loop(
            loop, lambda: _hub.broadcast(payload), name="log-broadcast"
        )

    log_service.get_service().register_listener(_on_entry)
    _BROADCASTERS.append(_on_entry)
    logger.info("Unified log broadcaster registered")


def teardown_broadcaster() -> None:
    service = log_service.get_service()
    while _BROADCASTERS:
        service.unregister_listener(_BROADCASTERS.pop())
    logger.info("Unified log broadcaster removed")


# ── app events (live) ───────────────────────────────────────────────────────

async def _stream_app(websocket: WebSocket) -> None:
    await _hub.connect(websocket)
    try:
        # Clients may send {"type":"ping"}; anything else is ignored.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info("Logs WS (app) connection closed")
    except Exception:
        logger.error("WS /ws/logs?source=app failed", exc_info=True)
        try:
            await websocket.close(code=CLOSE_ERROR)
        except Exception:
            logger.debug("Could not close logs WS", exc_info=True)
    finally:
        _hub.disconnect(websocket)


# ── file (tail -f) ──────────────────────────────────────────────────────────

async def _stream_file(websocket: WebSocket, path: str) -> None:
    await websocket.accept()
    real = os.path.realpath(path or "")
    if not any(real == allowed or real.startswith(allowed + os.sep) for allowed in _ALLOWED_LOG_DIRS):
        await _send(websocket, {"type": "error", "detail": "Path outside allowed log directories"})
        await websocket.close(code=CLOSE_FORBIDDEN)
        return
    if not os.path.isfile(real):
        await _send(websocket, {"type": "error", "detail": f"{real} not found"})
        await websocket.close(code=CLOSE_NOT_FOUND)
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            "tail", "-f", "-n", "50", real,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Cannot start tail for %s", real, exc_info=True)
        await _send(websocket, {"type": "error", "detail": str(exc)})
        await websocket.close(code=CLOSE_ERROR)
        return

    sent = 0
    try:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            await _send(websocket, {"type": "line", "source": f"file:{real}", "line": line})
            sent += 1
            if sent >= _MAX_LINES:
                await _send(websocket, {"type": "truncated", "sent": sent})
                break
    except WebSocketDisconnect:
        logger.info("File log stream disconnected: %s", real)
    except Exception as exc:  # noqa: BLE001
        logger.error("File log stream failed for %s", real, exc_info=True)
        await _send(websocket, {"type": "error", "detail": str(exc)})
    finally:
        proc.kill()
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001 — best-effort reap
            logger.debug("tail process reap failed", exc_info=True)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 — already closed
            logger.debug("log ws close failed", exc_info=True)


# ── journal (journalctl -f) ─────────────────────────────────────────────────

async def _stream_journal(websocket: WebSocket, unit: str) -> None:
    await websocket.accept()
    if unit and not _UNIT_RE.fullmatch(unit):
        await _send(websocket, {"type": "error", "detail": "Invalid unit name"})
        await websocket.close(code=400)
        return

    cmd = ["journalctl", "-q", "-f", "-n", "50", "--no-pager", "-o", "short-iso"]
    if unit:
        cmd += ["-u", unit]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Cannot start journalctl", exc_info=True)
        await _send(websocket, {"type": "error", "detail": str(exc)})
        await websocket.close(code=CLOSE_ERROR)
        return

    sent = 0
    try:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            await _send(websocket, {"type": "line", "source": f"journal:{unit or 'all'}", "line": line})
            sent += 1
            if sent >= _MAX_LINES:
                await _send(websocket, {"type": "truncated", "sent": sent})
                break
    except WebSocketDisconnect:
        logger.info("Journal stream disconnected")
    except Exception as exc:  # noqa: BLE001
        logger.error("Journal stream failed", exc_info=True)
        await _send(websocket, {"type": "error", "detail": str(exc)})
    finally:
        proc.kill()
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001 — best-effort reap
            logger.debug("journalctl process reap failed", exc_info=True)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 — already closed
            logger.debug("log ws close failed", exc_info=True)


# ── container (docker logs) ─────────────────────────────────────────────────

async def _stream_container(websocket: WebSocket, name: str) -> None:
    await websocket.accept()
    if not _CONTAINER_RE.match(name or ""):
        await _send(websocket, {"type": "error", "detail": f"Invalid container name '{name}'"})
        await websocket.close(code=CLOSE_NOT_FOUND)
        return

    tail = _tail_param(websocket)
    try:
        import docker

        def _get_container():
            client = docker.from_env()
            return client.containers.get(name)

        container = await asyncio.to_thread(_get_container)
    except Exception as exc:  # noqa: BLE001 — NotFound or Docker unavailable
        message = f"Container '{name}' not found" if "NotFound" in type(exc).__name__ else str(exc)
        logger.warning("Container log stream rejected for %s: %s", name, message)
        await _send(websocket, {"type": "error", "detail": message})
        await websocket.close(code=CLOSE_NOT_FOUND)
        return

    try:
        stream = await asyncio.to_thread(
            lambda: container.logs(stream=True, follow=True, tail=tail)
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Cannot read logs for container %s", name, exc_info=True)
        await _send(websocket, {"type": "error", "detail": str(exc)})
        await websocket.close(code=CLOSE_ERROR)
        return

    sent = 0
    try:
        while True:
            # One chunk per thread hop: iterating inline would block the loop.
            chunk: Optional[bytes] = await asyncio.to_thread(next, stream, None)
            if chunk is None:
                await _send(websocket, {"type": "end", "detail": "container is no longer running"})
                break
            for line in chunk.decode("utf-8", "replace").rstrip("\n").splitlines():
                await _send(websocket, {"type": "line", "source": f"container:{name}", "line": line})
                sent += 1
                if sent >= _MAX_LINES:
                    await _send(websocket, {"type": "truncated", "sent": sent})
                    return
    except WebSocketDisconnect:
        logger.info("Container log stream disconnected: %s", name)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Container log stream failed for %s", name, exc_info=True)
        await _send(websocket, {"type": "error", "detail": str(exc)})
    finally:
        try:
            await asyncio.to_thread(stream.close)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.debug("docker log stream close failed", exc_info=True)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 — already closed
            logger.debug("log ws close failed", exc_info=True)


# ── routes ──────────────────────────────────────────────────────────────────

@router.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket, source: str = "app") -> None:
    """Stream app events or a host/container source as JSON frames."""
    if not await _authenticate(websocket):
        return
    if source == "app":
        await _stream_app(websocket)
        return
    if source.startswith("container:"):
        await _stream_container(websocket, source.split(":", 1)[1])
        return
    if source.startswith("file:"):
        await _stream_file(websocket, source.split(":", 1)[1])
        return
    if source == "journal":
        await _stream_journal(websocket, (websocket.query_params.get("unit") or "").strip())
        return
    # Accept first so the browser sees the reason instead of a failed handshake.
    await websocket.accept()
    await _send(websocket, {"type": "error", "detail": f"Unknown source '{source}'"})
    await websocket.close(code=CLOSE_NOT_FOUND)


# Legacy routes — same auth and JSON frames, kept for cached clients.

@router.websocket("/ws/logs/system")
async def ws_log_stream(websocket: WebSocket, path: str = "/var/log/syslog") -> None:
    """Legacy: system file stream. Prefer `/ws/logs?source=file:<path>`."""
    if not await _authenticate(websocket):
        return
    await _stream_file(websocket, path)


@router.websocket("/ws/logs/{container_name}")
async def stream_container_logs(websocket: WebSocket, container_name: str) -> None:
    """Legacy: container stream. Prefer `/ws/logs?source=container:<name>`."""
    if not await _authenticate(websocket):
        return
    await _stream_container(websocket, container_name)
