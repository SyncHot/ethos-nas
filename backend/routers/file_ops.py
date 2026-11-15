"""EthOS NAS — File Operations Router (HTTP endpoints)

REST:
- POST   /api/files/copy     — start a background copy (returns op_id)
- POST   /api/files/move     — start a background move (returns op_id)
- GET    /api/files/ops/{op_id} — progress snapshot of a file operation
- DELETE /api/files/delete    — start a background hard delete (returns op_id)
- GET    /api/files/zip       — create and download zip archive of selected items
- POST   /api/files/chunked/init        — initialize chunked upload session
- POST   /api/files/chunked/upload      — append a chunk to an in-progress upload
- POST   /api/files/chunked/complete    — finalize chunked upload

EventBroadcaster: Every successful mutation broadcasts to connected WebSocket clients.
"""

import asyncio
import json as _json
import logging
import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, Form, File, Request
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from backend.services import path_scope
from backend.services import trash_service
from backend.services.files_service import (
    start_copy_operation, start_move_operation, get_file_op,
    start_hard_delete_operation, create_zip_archive,
    get_events_for_dir, clear_events,
    create_chunked_upload, append_chunk, complete_chunked_upload,
    start_trash_operation, start_purge_operation,
)

logger = logging.getLogger("ethos-nas.routers.file_ops")
# Same per-role path scope as the File Manager — without this dependency a
# non-admin could copy/move/delete anywhere the service user can reach.
router = APIRouter(dependencies=[Depends(path_scope.scope_request)])


class CopyMoveRequest(BaseModel):
    """Body for POST /copy and /move."""
    model_config = {"extra": "forbid"}

    sources: list[str] = Field(min_length=1)
    dest_dir: str = Field(min_length=1)


class HardDeleteRequest(BaseModel):
    """Body for DELETE /delete."""
    model_config = {"extra": "forbid"}

    paths: list[str] = Field(min_length=1)


class TrashBatchRequest(BaseModel):
    """Body for POST /trash/batch (move many items to the caller's trash)."""
    model_config = {"extra": "forbid"}

    paths: list[str] = Field(min_length=1)


class TrashPurgeItem(BaseModel):
    """One trash entry to delete permanently."""
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1)
    owner: str | None = None


class TrashPurgeRequest(BaseModel):
    """Body for DELETE /trash/items."""
    model_config = {"extra": "forbid"}

    items: list[TrashPurgeItem] = Field(min_length=1)


def _sanitize_many(paths: object) -> list[str]:
    """Scope-check every path in a request list (non-list input passes through)."""
    if not isinstance(paths, list):
        return []
    return [path_scope.sanitize(str(p)) for p in paths]


@router.post("/copy")
async def api_copy(body: CopyMoveRequest):
    """Start a background copy of files/directories.

    Body: {sources: [path1, path2, ...], dest_dir: "/absolute/path/to/dest"}
    Returns immediately with {op_id} — poll GET /api/files/ops/{op_id} for
    live progress (the blocking copy runs in a worker thread).
    """
    sources = _sanitize_many(body.sources)
    dest_dir = path_scope.sanitize(body.dest_dir)

    try:
        op_id = await start_copy_operation(sources, dest_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"op_id": op_id}


@router.get("/ops/{op_id}")
async def api_get_op(op_id: str):
    """Progress snapshot of a copy/move operation.

    Returns {op_id, action, status(running|done), total, index, current,
             percent, bytes_done, bytes_total, succeeded, failed} or 404.
    """
    op = get_file_op(op_id)
    if not op:
        raise HTTPException(status_code=404, detail="Unknown operation")
    return op


@router.post("/move")
async def api_move(body: CopyMoveRequest):
    """Start a background move/cut of files/directories.

    Body: {sources: [path1, path2, ...], dest_dir: "/absolute/path/to/dest"}
    Returns immediately with {op_id} — poll GET /api/files/ops/{op_id} for
    live progress (the blocking move runs in a worker thread).
    """
    sources = _sanitize_many(body.sources)
    dest_dir = path_scope.sanitize(body.dest_dir)

    try:
        op_id = await start_move_operation(sources, dest_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"op_id": op_id}


@router.delete("/delete")
async def api_hard_delete(body: HardDeleteRequest):
    """Permanently delete files/directories from disk (background op).

    Body: {paths: [path1, path2, ...]}
    Returns immediately with {op_id} — poll GET /api/files/ops/{op_id} for
    progress. The blocking deletion runs in a worker thread so large deletes
    never stall the event loop.
    """
    paths = _sanitize_many(body.paths)

    try:
        op_id = await start_hard_delete_operation(paths)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"op_id": op_id}


@router.post("/trash/batch")
async def api_trash_batch(body: TrashBatchRequest, request: Request):
    """Move several files/directories into the caller's trash (background op).

    Body: {paths: [path1, path2, ...]}
    Returns immediately with {op_id} — poll GET /api/files/ops/{op_id}.
    Cross-device moves copy with byte progress; failures carry a reason.
    """
    paths = _sanitize_many(body.paths)
    username, _is_admin = trash_service.actor_from_claims(
        getattr(request.state, "current_user", None))
    try:
        op_id = await start_trash_operation(paths, username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"op_id": op_id}


@router.delete("/trash/items")
async def api_trash_purge(body: TrashPurgeRequest, request: Request):
    """Permanently delete selected trash entries (background op).

    Body: {items: [{name, owner?}, ...]} — a non-admin may only name their own
    namespace; admins may pass `owner` for the all-users view.
    """
    username, is_admin = trash_service.actor_from_claims(
        getattr(request.state, "current_user", None))
    resolved: list[dict] = []
    for item in body.items:
        try:
            owner, _ = trash_service.target_owner(username, is_admin, item.owner)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        try:
            trash_service.valid_item_name(item.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        resolved.append({"name": item.name, "owner": owner})
    try:
        op_id = await start_purge_operation(resolved)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"op_id": op_id}


@router.delete("/trash/all")
async def api_trash_empty(request: Request,
                         all_users: bool = Query(False, alias="all")):
    """Permanently delete every trash item (background op).

    Admins may pass `?all=1` to purge every account's namespace. Returns
    {op_id, total}; `op_id` is null when the trash was already empty.
    """
    username, is_admin = trash_service.actor_from_claims(
        getattr(request.state, "current_user", None))
    if all_users and not is_admin:
        logger.warning("Trash: %s tried to empty every account's trash", username)
        raise HTTPException(status_code=403, detail="Admin access required")
    listing = await asyncio.to_thread(
        trash_service.list_items, username, all_users, 0, 0)
    items = [{"name": it["name"], "owner": it.get("owner") or username}
             for it in listing.get("items", [])]
    if not items:
        return {"op_id": None, "total": 0}
    try:
        op_id = await start_purge_operation(items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"op_id": op_id, "total": len(items)}


def _cleanup_zip(path: str) -> None:
    """Remove a served zip archive (runs after the response completes)."""
    try:
        os.remove(path)
    except OSError:
        pass  # best-effort cleanup — nothing to report

@router.get("/zip")
async def api_zip(path: str = Query(...)):
    """Create a zip archive from selected items.

    Path format: comma-separated or JSON-encoded list of absolute paths.
    Returns streaming ZIP download with Content-Disposition: attachment.
    The temporary archive is deleted as soon as the response finishes —
    previously it accumulated in backend/data/temp forever.
    """
    try:
        # Parse path parameter — could be a single path, comma-separated list,
        # or JSON array. (Paths containing commas must use the JSON form.)
        if isinstance(path, str) and path.startswith('['):
            sources = _json.loads(path)
        else:
            sources = [p.strip() for p in path.split(",") if p.strip()]

        if not sources or not isinstance(sources, list):
            raise HTTPException(status_code=400, detail="No items selected for zip")

        sources = _sanitize_many(sources)

        result = await create_zip_archive(sources)
        return FileResponse(
            result["path"],
            media_type="application/zip",
            filename=result["filename"],
            headers={"Content-Disposition": f'attachment; filename="{result["filename"]}"'},
            background=BackgroundTask(_cleanup_zip, result["path"]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/chunked/init")
async def api_chunk_init(body: dict):
    """Initialize a chunked upload session.

    Body: {file_id: "unique-id", filename: "name.ext"}
    Returns: {upload_id, dest_dir, filename}
    """
    file_id = body.get("file_id") or str(uuid.uuid4())
    # basename() prevents path traversal via the filename field
    filename = os.path.basename(body.get("filename") or "uploaded")
    dest_dir = path_scope.sanitize(body.get("dest_dir") or "/tmp")
    # Exact byte count — lets complete() reject a truncated transfer instead of
    # moving it over the destination and reporting success.
    try:
        total_size = int(body.get("total_size") or 0)
    except (TypeError, ValueError):
        total_size = 0

    try:
        return await asyncio.to_thread(create_chunked_upload, file_id, dest_dir,
                                       filename, total_size)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/chunked/upload")
async def api_chunk_upload(upload_id: str = Form(...), chunk_index: int = Form(...), data: bytes = File(...)):
    """Append a chunk to an in-progress upload.

    Parameters via multipart/form-data:
      - upload_id: The session ID from /chunked/init
      - chunk_index: Zero-based index of this chunk
      - data: Raw binary chunk data (handled by FastAPI form parser)

    Returns progress status including percentage complete.
    """
    try:
        return await asyncio.to_thread(append_chunk, upload_id, chunk_index, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/chunked/complete")
async def api_chunk_complete(upload_id: str = Form(...)):
    """Finalize a chunked upload — move temp file to destination.

    Returns the final upload result with path and size.
    """
    try:
        return await asyncio.to_thread(complete_chunked_upload, upload_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/events")
async def api_get_events(dir_path: str = Query("/")):
    """Get recent file mutation events for a directory.

    Used by the reactive sync system to detect changes that need refetching.
    Returns list of {action, path, items, timestamp}.
    """
    try:
        return {"events": get_events_for_dir(dir_path)}
    except Exception as exc:
        logger.error("Failed to get events for %s: %s", dir_path, exc)
        return {"events": []}


@router.delete("/events")
async def api_clear_events():
    """Clear all stored mutation events."""
    clear_events()
    return {"ok": True}
