"""Cameras API — /api/surveillance.

Camera CRUD + connection test, snapshot (disk-cached JPEG), live MJPEG stream
(ffmpeg re-mux), and motion-detection events fed into the Notification Center.

Auth note: <img>/live URLs can't send headers — clients append ?token=<jwt>
(handled by the global auth middleware). These paths are NOT public.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from backend.services import camera_discovery as cd
from backend.services import camera_service as cs
from backend.services import shutdown_state
from backend.services.camera_service import CameraError

logger = logging.getLogger("ethos-nas.routers.surveillance")

router = APIRouter()


class MotionCfg(BaseModel):
    enabled: Optional[bool] = None
    interval_s: Optional[float] = Field(default=None, ge=1, le=3600)
    sensitivity: Optional[float] = Field(default=None, ge=0.05, le=0.9)


class CameraIn(BaseModel):
    name: str
    url: str


class CameraUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    enabled: Optional[bool] = None
    motion: Optional[MotionCfg] = None


def _cam_or_404(cam_id: str) -> dict:
    for c in cs.list_cameras():
        if c["id"] == cam_id:
            return c
    raise HTTPException(status_code=404, detail="Camera not found")


# ── Cameras ─────────────────────────────────────────────────────────────────

@router.get("/cameras")
async def list_cameras() -> dict:
    return {"cameras": cs.list_cameras()}


@router.post("/cameras", status_code=201)
async def add_camera(body: CameraIn) -> dict:
    try:
        cam = cs.add_camera(body.name, body.url)
    except CameraError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("Camera added: %s (%s)", cam["name"], cam["id"])
    return {"camera": cam}


@router.put("/cameras/{cam_id}")
async def update_camera(cam_id: str, body: CameraUpdate) -> dict:
    try:
        cam = cs.update_camera(cam_id, body.model_dump(exclude_none=True))
    except CameraError as exc:
        status = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return {"camera": cam}


@router.delete("/cameras/{cam_id}")
async def delete_camera(cam_id: str) -> dict:
    try:
        cs.delete_camera(cam_id)
    except CameraError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/cameras/{cam_id}/test")
async def test_camera(cam_id: str) -> dict:
    cam = _cam_or_404(cam_id)
    # Probe only — never raises on an unreachable camera (≤8 s).
    return cs.test_connection(cam["url"])


class DiscoverIn(BaseModel):
    timeout_s: float = Field(default=3.0, ge=1, le=15)   # ONVIF probe window
    scan_subnets: bool = True                             # + TCP port scan of the LAN


@router.post("/discover")
async def discover_cameras(body: DiscoverIn) -> dict:
    """Search the local network for cameras (ONVIF WS-Discovery + RTSP/HTTP scan).

    Runs in a worker thread — never raises; an empty result set is a valid answer.
    """
    return await asyncio.to_thread(cd.discover, body.timeout_s, body.scan_subnets)


# ── Snapshot / live ─────────────────────────────────────────────────────────

@router.get("/cameras/{cam_id}/snapshot")
async def snapshot(cam_id: str, w: int = Query(default=480, ge=64, le=1920), refresh: bool = False) -> Response:
    _cam_or_404(cam_id)
    try:
        path = cs.get_snapshot(cam_id, width=w, refresh=refresh)
    except CameraError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(
        content=path.read_bytes(),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/cameras/{cam_id}/live")
async def live(cam_id: str) -> StreamingResponse:
    _cam_or_404(cam_id)
    try:
        stream = cs.live_stream(cam_id)  # generator of multipart parts (bytes)
    except CameraError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    def _frames():
        # A never-ending MJPEG body would otherwise keep uvicorn draining past
        # the unit's stop deadline (services/shutdown_state.py).
        for part in stream:
            if shutdown_state.is_set():
                break
            yield part

    return StreamingResponse(
        _frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# ── Recording (MP4 clips) ────────────────────────────────────────────────

@router.post("/cameras/{cam_id}/record", status_code=201)
async def start_recording(cam_id: str) -> dict:
    _cam_or_404(cam_id)
    try:
        return cs.start_recording(cam_id)
    except CameraError as exc:
        detail = str(exc)
        if "already recording" in detail:
            status = 409
        elif "Clip storage full" in detail:
            status = 507
        else:
            status = 400
        raise HTTPException(status_code=status, detail=detail) from exc


@router.delete("/cameras/{cam_id}/record")
async def stop_recording(cam_id: str) -> dict:
    try:
        return cs.stop_recording(cam_id)
    except CameraError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/cameras/{cam_id}/record")
async def recording_status(cam_id: str) -> dict:
    _cam_or_404(cam_id)
    return cs.recording_status(cam_id)


# ── Clips ────────────────────────────────────────────────────────────────

@router.get("/clips")
async def list_clips(camera_id: Optional[str] = None) -> dict:
    return {"clips": cs.list_clips(camera_id=camera_id)}


@router.get("/clips/{clip_id}/download")
async def download_clip(clip_id: str) -> FileResponse:
    try:
        path = cs.get_clip_path(clip_id)
    except CameraError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="video/mp4", filename=clip_id)


@router.delete("/clips/{clip_id}")
async def delete_clip(clip_id: str) -> dict:
    try:
        cs.delete_clip(clip_id)
    except CameraError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


# ── Events ──────────────────────────────────────────────────────────────────

@router.get("/events")
async def list_events(limit: int = Query(default=50, ge=1, le=200), camera_id: Optional[str] = None) -> dict:
    return {"events": cs.list_events(limit=limit, camera_id=camera_id)}


@router.delete("/events/{event_id}")
async def delete_event(event_id: str) -> dict:
    try:
        cs.delete_event(event_id)
    except CameraError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/events")
async def clear_events() -> dict:
    return {"ok": True, "removed": cs.clear_events()}


@router.get("/events/{event_id}/image")
async def event_image(event_id: str) -> Response:
    for e in cs.list_events(limit=200):
        if e["id"] == event_id and e.get("image"):
            path = cs.EVENTS_IMG_DIR / e["image"]
            if not path.exists():
                raise HTTPException(status_code=410, detail="Image no longer on disk")
            return Response(content=path.read_bytes(), media_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})
    raise HTTPException(status_code=404, detail="Event not found")
