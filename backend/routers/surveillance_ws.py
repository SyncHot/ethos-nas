"""Cameras WebSocket — /ws/surveillance.

Pushes live camera status (online, bitrate, recording) and new motion events so
the package UI does not poll. Frames (JSON):

    {"type":"status","cameras":[{camera_id, online, bitrate_kbps, …}]}
    {"type":"event","event":{…}}          a new motion event the caller can see
    {"type":"sync","cameras":[…]}`        first frame after connect

Auth: `?token=<jwt>` (a WebSocket cannot set headers). Missing/invalid token →
close **4001**, which tells `core/ws.js` to stop reconnecting. The camera list is
filtered by the caller's `viewers` grants, exactly like the REST endpoints.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.services import camera_service as cs

logger = logging.getLogger("ethos-nas.routers.surveillance_ws")
router = APIRouter()

TICK_S = 2.0


def _status_frames(user: dict) -> list[dict]:
    frames = []
    for cam in cs.list_cameras(user):
        status = dict(cam.get("status") or {})
        status["camera_id"] = cam["id"]
        status["name"] = cs.mask_text(cam.get("name") or "")
        frames.append(status)
    return frames


@router.websocket("/ws/surveillance")
async def websocket_surveillance(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token") or websocket.cookies.get("token") or ""
    from backend.routers.auth import _verify_token

    payload = _verify_token(token)
    if not payload:
        await websocket.close(code=4001, reason="Authentication required")
        return

    user = {"username": str(payload.get("username") or ""),
            "role": str(payload.get("role") or "user")}
    await websocket.accept()
    logger.info("Cameras WS client connected (%s)", user["username"] or "?")
    last_status = ""
    last_event_id = None
    try:
        while True:
            cameras = _status_frames(user)
            text = json.dumps({"type": "status", "cameras": cameras}, ensure_ascii=False)
            if text != last_status:
                await websocket.send_text(text)
                last_status = text
            events = cs.list_events(limit=1, user=user)
            if events:
                newest = events[0]
                if newest.get("id") != last_event_id:
                    is_first = last_event_id is None
                    last_event_id = newest.get("id")
                    if not is_first:
                        await websocket.send_text(json.dumps(
                            {"type": "event", "event": newest}, ensure_ascii=False))
            # A short receive timeout doubles as the send tick and as the
            # disconnect detector (the client sends {"type":"ping"}).
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=TICK_S)
            except asyncio.TimeoutError:  # expected — the 2 s wait is the frame tick
                pass
    except WebSocketDisconnect:
        logger.info("Cameras WS connection closed")
    except Exception:  # noqa: BLE001 — log and drop one client, never the app
        logger.error("WS /ws/surveillance failed", exc_info=True)
        try:
            await websocket.close(code=1011)
        except Exception:  # noqa: BLE001 — socket already gone
            logger.debug("Could not close cameras WS", exc_info=True)
