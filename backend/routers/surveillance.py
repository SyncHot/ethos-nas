"""Cameras API — /api/surveillance.

Camera CRUD + connection test, live HLS (one pipeline per camera), snapshot,
recordings/playback/export and motion events.

Auth: every caller needs a JWT. Camera management, settings, storage and
exports are admin-only; live view, snapshots, playback and events are filtered
per camera (`viewers`). Media URLs carry `?token=<jwt>` because <img>/<video>
cannot send headers (the global auth middleware accepts it).
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from backend.routers.auth import require_admin
from backend.services import camera_discovery as cd
from backend.services import camera_recordings, camera_service as cs, camera_settings, camera_storage
from backend.services import shutdown_state
from backend.services.camera_service import CameraError

logger = logging.getLogger("ethos-nas.routers.surveillance")

router = APIRouter()

_ADMIN = [Depends(require_admin)]


def _user(request: Request) -> dict:
    user = getattr(request.state, "current_user", None) or {}
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def _parse_ts(value: Optional[str]) -> datetime:
    if not value:
        raise ValueError("missing timestamp")
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class MotionCfg(BaseModel):
    enabled: Optional[bool] = None
    interval_s: Optional[float] = Field(default=None, ge=1, le=3600)   # legacy alias for cooldown_s
    sensitivity: Optional[float] = Field(default=None, ge=0.02, le=0.95)
    cooldown_s: Optional[float] = Field(default=None, ge=1, le=3600)
    min_cells: Optional[int] = Field(default=None, ge=1, le=500)
    zones: Optional[dict] = None
    schedule: Optional[dict] = None


class RecordCfg(BaseModel):
    mode: Optional[str] = None
    schedule: Optional[dict] = None
    retention_days: Optional[float] = None


class CameraIn(BaseModel):
    name: str = ""
    url: str = ""
    sub_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    streams: Optional[dict] = None


class CameraUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    sub_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    streams: Optional[dict] = None
    enabled: Optional[bool] = None
    motion: Optional[MotionCfg] = None
    record: Optional[RecordCfg] = None
    viewers: Optional[list[str]] = None
    onvif: Optional[dict] = None


class DiscoverIn(BaseModel):
    timeout_s: float = Field(default=3.0, ge=1, le=15)   # ONVIF probe window
    scan_subnets: bool = True                             # + TCP port scan of the LAN


class SettingsIn(BaseModel):
    storage_dir: Optional[str] = None
    quota_pct: Optional[int] = None
    default_retention_days: Optional[int] = None
    segment_s: Optional[int] = None
    pre_s: Optional[int] = None
    post_s: Optional[int] = None
    notify_motion: Optional[bool] = None
    notify_offline: Optional[bool] = None


class ExportIn(BaseModel):
    camera_id: str
    from_iso: str = Field(alias="from")
    to_iso: str = Field(alias="to")


def _cam_or_404(cam_id: str, user=None) -> dict:
    try:
        return cs.get_camera(cam_id, user)
    except CameraError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ── Cameras ─────────────────────────────────────────────────────────────────

@router.get("/cameras")
async def list_cameras(request: Request) -> dict:
    user = _user(request)
    return {"cameras": [cs.public_camera(c) for c in cs.list_cameras(user)]}


@router.post("/cameras", status_code=201, dependencies=_ADMIN)
async def add_camera(body: CameraIn) -> dict:
    streams = body.streams or {}
    main = body.url or streams.get("main") or ""
    sub = body.sub_url or streams.get("sub")
    try:
        cam = cs.add_camera(body.name, main, sub_url=sub,
                            username=body.username or "", password=body.password or "")
    except CameraError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"camera": cs.public_camera(cs.get_camera(cam["id"]))}


@router.put("/cameras/{cam_id}", dependencies=_ADMIN)
async def update_camera(cam_id: str, body: CameraUpdate) -> dict:
    fields = body.model_dump(exclude_none=True)
    try:
        cam = cs.update_camera(cam_id, fields)
    except CameraError as exc:
        status = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return {"camera": cs.public_camera(cs.get_camera(cam_id)) if cam else None}


@router.delete("/cameras/{cam_id}", dependencies=_ADMIN)
async def delete_camera(cam_id: str) -> dict:
    try:
        cs.delete_camera(cam_id)
    except CameraError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/cameras/{cam_id}/test", dependencies=_ADMIN)
async def test_camera(cam_id: str) -> dict:
    cam = _cam_or_404(cam_id)
    # Probe only — never raises on an unreachable camera (≤8 s).
    return cs.test_connection(cs.stream_urls(cam, "main"))


@router.post("/test", dependencies=_ADMIN)
async def test_unsaved(body: CameraIn) -> dict:
    """Probe a URL before the camera exists (Add-camera wizard)."""
    streams = body.streams or {}
    main = body.url or streams.get("main") or ""
    return cs.test_connection(main, body.username or "", body.password or "")


@router.post("/discover", dependencies=_ADMIN)
async def discover_cameras(body: DiscoverIn) -> dict:
    """Search the local network for cameras (ONVIF WS-Discovery + RTSP/HTTP scan).

    Runs in a worker thread — never raises; an empty result set is a valid answer.
    """
    return await asyncio.to_thread(cd.discover, body.timeout_s, body.scan_subnets)


# ── Snapshot / live ─────────────────────────────────────────────────────────

@router.get("/cameras/{cam_id}/snapshot")
async def snapshot(request: Request, cam_id: str,
                   w: int = Query(default=480, ge=64, le=1920),
                   refresh: bool = False, at: Optional[str] = None) -> Response:
    user = _user(request)
    _cam_or_404(cam_id, user)
    if at:
        try:
            moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid timestamp") from exc
        try:
            path = camera_recordings.snapshot_at(cam_id, moment)
        except CameraError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    else:
        try:
            path = cs.get_snapshot(cam_id, width=w, refresh=refresh, user=user)
        except CameraError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(content=path.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@router.get("/cameras/{cam_id}/live")
async def live(request: Request, cam_id: str) -> StreamingResponse:
    user = _user(request)
    _cam_or_404(cam_id, user)
    try:
        stream = cs.live_stream(cam_id, user)  # legacy MJPEG generator
    except CameraError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    def _frames():
        # A never-ending MJPEG body would otherwise keep uvicorn draining past
        # the unit's stop deadline (services/shutdown_state.py).
        for part in stream:
            if shutdown_state.is_set():
                break
            yield part

    return StreamingResponse(_frames(), media_type="multipart/x-mixed-replace; boundary=frame")


@router.get("/cameras/{cam_id}/hls/{name}")
async def live_hls(request: Request, cam_id: str, name: str) -> Response:
    """Live HLS window written by camera_runner (playlist + fMP4 segments)."""
    user = _user(request)
    _cam_or_404(cam_id, user)
    if name == "index.m3u8":
        path = camera_storage.camera_live_dir(cam_id) / "index.m3u8"
        media = "application/vnd.apple.mpegurl"
    elif name == "init.mp4":
        path = camera_storage.camera_live_dir(cam_id) / "init.mp4"
        media = "video/mp4"
    elif camera_recordings.SEGMENT_RE.match(name):
        path = camera_storage.camera_live_dir(cam_id) / name
        media = "video/iso.segment"
    else:
        raise HTTPException(status_code=404, detail="Unknown stream file")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Stream not ready yet")
    return Response(content=path.read_bytes(), media_type=media,
                    headers={"Cache-Control": "no-store"})


# ── Recordings / playback / export ─────────────────────────────────────────

@router.get("/recordings/{cam_id}/days")
async def recording_days(request: Request, cam_id: str) -> dict:
    user = _user(request)
    _cam_or_404(cam_id, user)
    return {"days": camera_recordings.available_days(cam_id)}


@router.get("/cameras/{cam_id}/recordings")
async def recordings_for_day(request: Request, cam_id: str,
                             date: Optional[str] = None) -> dict:
    user = _user(request)
    _cam_or_404(cam_id, user)
    day = date or datetime.now(timezone.utc).date().isoformat()
    try:
        items = camera_recordings.ranges(cam_id, day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date") from exc
    return {"date": day, "ranges": items, "total_s": sum(r["duration_s"] for r in items)}


@router.get("/playback/{cam_id}/index.m3u8")
async def playback_playlist(request: Request, cam_id: str,
                            start: Optional[str] = Query(default=None, alias="from"),
                            end: Optional[str] = Query(default=None, alias="to")) -> PlainTextResponse:
    user = _user(request)
    _cam_or_404(cam_id, user)
    try:
        begin = _parse_ts(start)
        finish = _parse_ts(end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="from/to must be ISO timestamps") from exc
    try:
        body = camera_recordings.playlist(cam_id, begin, finish)
    except CameraError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PlainTextResponse(body, media_type="application/vnd.apple.mpegurl",
                             headers={"Cache-Control": "no-store"})


@router.get("/playback/{cam_id}/seg")
async def playback_segment(request: Request, cam_id: str, p: str) -> FileResponse:
    user = _user(request)
    _cam_or_404(cam_id, user)
    try:
        path = camera_recordings.segment_path(cam_id, p)
    except (CameraError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="video/iso.segment")


@router.post("/exports", status_code=201, dependencies=_ADMIN)
async def create_export(body: ExportIn) -> dict:
    try:
        op = await asyncio.to_thread(camera_recordings.start_export, body.camera_id,
                                     body.from_iso, body.to_iso)
    except (CameraError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"op_id": op["op_id"], "export": op}


@router.get("/exports/{op_id}")
async def export_status(request: Request, op_id: str) -> dict:
    _user(request)
    try:
        return {"export": camera_recordings.get_export(op_id)}
    except CameraError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/exports/{op_id}/download")
async def download_export(request: Request, op_id: str) -> FileResponse:
    _user(request)
    try:
        path = camera_recordings.export_path(op_id)
    except CameraError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="video/mp4", filename=f"{op_id}.mp4")


# ── Settings / storage (admin) ──────────────────────────────────────────────

@router.get("/settings", dependencies=_ADMIN)
async def get_settings() -> dict:
    return {"settings": camera_settings.load()}


@router.put("/settings", dependencies=_ADMIN)
async def put_settings(body: SettingsIn) -> dict:
    try:
        return {"settings": camera_settings.save(body.model_dump(exclude_none=True))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/storage", dependencies=_ADMIN)
async def get_storage() -> dict:
    return {"storage": await asyncio.to_thread(camera_storage.usage)}


@router.post("/storage/purge", dependencies=_ADMIN)
async def purge_storage() -> dict:
    result = await asyncio.to_thread(camera_storage.purge)
    return {"ok": True, **result}


# ── Recording (MP4 clips) ────────────────────────────────────────────────

@router.post("/cameras/{cam_id}/record", status_code=201, dependencies=_ADMIN)
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


@router.delete("/cameras/{cam_id}/record", dependencies=_ADMIN)
async def stop_recording(cam_id: str) -> dict:
    try:
        return cs.stop_recording(cam_id)
    except CameraError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/cameras/{cam_id}/record")
async def recording_status(request: Request, cam_id: str) -> dict:
    _cam_or_404(cam_id, _user(request))
    return cs.recording_status(cam_id)


# ── Clips ────────────────────────────────────────────────────────────────

@router.get("/clips", dependencies=_ADMIN)
async def list_clips(camera_id: Optional[str] = None) -> dict:
    return {"clips": cs.list_clips(camera_id=camera_id)}


@router.get("/clips/{clip_id}/download", dependencies=_ADMIN)
async def download_clip(clip_id: str) -> FileResponse:
    try:
        path = cs.get_clip_path(clip_id)
    except CameraError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="video/mp4", filename=clip_id)


@router.delete("/clips/{clip_id}", dependencies=_ADMIN)
async def delete_clip(clip_id: str) -> dict:
    try:
        cs.delete_clip(clip_id)
    except CameraError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


# ── Events ──────────────────────────────────────────────────────────────────

@router.get("/events")
async def list_events(request: Request, limit: int = Query(default=50, ge=1, le=2000),
                      camera_id: Optional[str] = None, date: Optional[str] = None,
                      reviewed: Optional[bool] = None) -> dict:
    return {"events": cs.list_events(limit=limit, camera_id=camera_id, user=_user(request),
                                     day=date, reviewed=reviewed)}


@router.post("/events/{event_id}/reviewed")
async def review_event(request: Request, event_id: str) -> dict:
    user = _user(request)
    cs.get_event(event_id, user)                    # 404 for a hidden camera
    return {"event": cs.set_event_reviewed(event_id, True)}


@router.delete("/events/{event_id}", dependencies=_ADMIN)
async def delete_event(event_id: str) -> dict:
    try:
        cs.delete_event(event_id)
    except CameraError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/events", dependencies=_ADMIN)
async def clear_events() -> dict:
    return {"ok": True, "removed": cs.clear_events()}


@router.get("/events/{event_id}/image")
async def event_image(request: Request, event_id: str) -> Response:
    try:
        path = cs.event_image_path(event_id, _user(request))
    except CameraError as exc:
        status = 410 if "no longer" in str(exc) else 404
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return Response(content=path.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})
