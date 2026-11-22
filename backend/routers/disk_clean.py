"""EthOS NAS — Disk Cleaner Router

REST (admin-only; every path is scope-checked):
- GET  /api/disk-clean/scan             — legacy folder-size tree (synchronous)
- GET  /api/disk-clean/top-files        — legacy biggest files (synchronous)
- POST /api/disk-clean/scans            — start a background scan job
- GET  /api/disk-clean/scans/{job_id}   — job state, live progress and result
- POST /api/disk-clean/scans/{job_id}/cancel — cooperative cancel
- GET  /api/disk-clean/suggestions      — cleanup candidates (cached, ?refresh=1)
- POST /api/disk-clean/delete           — move to trash or delete permanently
- POST /api/disk-clean/caches/clean     — reclaim EthOS-managed caches
- POST /api/disk-clean/vacuum-journal   — journalctl --vacuum-time
- GET  /api/disk-clean/history          — recent reclaim actions

The walking, suggestion and deletion logic lives in
``services/disk_clean_service.py``; this router validates the request, resolves
the caller's trash ownership and maps failures onto HTTP status codes.
"""

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from backend.routers.auth import require_admin
from backend.services import disk_clean_service as svc
from backend.services import path_scope, trash_service

logger = logging.getLogger("ethos-nas.routers.disk_clean")
# Cleaner deletes whatever the scans find — admin-only, and every path must be
# mapped onto the caller's scope before it is touched.
router = APIRouter(dependencies=[Depends(require_admin), Depends(path_scope.scope_request)])


def _username(request: Request) -> str:
    """Account whose trash receives "move to trash" actions."""
    username, _is_admin = trash_service.actor_from_claims(
        getattr(request.state, "current_user", None))
    return username


class ScanRequest(BaseModel):
    """Body for POST /scans."""

    path: str = Field(default="/home", min_length=1)
    depth: int = Field(default=2, ge=1, le=5)
    mode: Literal["tree", "files"] = "tree"
    limit: int = Field(default=25, ge=1, le=100)

    model_config = {"extra": "forbid"}


class DeleteRequest(BaseModel):
    """Body for POST /delete.

    ``mode="trash"`` moves user content into the caller's recycle bin
    (recoverable in Files); ``permanent`` deletes it. ``async`` (JSON key)
    returns an ``op_id`` that is polled through ``/api/files/ops/{op_id}``.
    """

    paths: list[str] = Field(..., min_length=1, max_length=svc.MAX_DELETE_PATHS)
    mode: Literal["permanent", "trash"] = "permanent"
    run_async: bool = Field(default=False, alias="async")

    model_config = {"extra": "forbid", "populate_by_name": True}


class VacuumRequest(BaseModel):
    """Body for POST /vacuum-journal."""

    days: int = Field(default=7, ge=1, le=3650)

    model_config = {"extra": "forbid"}


class CacheCleanRequest(BaseModel):
    """Body for POST /caches/clean.

    ``areas`` is a subset of services.disk_clean_service.CLEANABLE_CACHES:
    temp_chunks, transcodes, trash, thumbnails, video_thumbs.
    """

    areas: list[str] = Field(..., min_length=1, max_length=10)

    model_config = {"extra": "forbid"}


# ── Legacy synchronous endpoints (older app versions) ────────────────────────

@router.get("/scan")
async def scan(
    path: str = Query(default="/home", description="Root directory to analyze"),
    depth: int = Query(default=2, ge=1, le=5),
):
    """Directory size tree for `path`, expanded `depth` levels."""
    return await svc.scan_sync(path, depth)


@router.get("/top-files")
async def top_files(
    path: str = Query(default="/home", description="Root directory to search"),
    limit: int = Query(default=25, ge=1, le=100),
):
    """Largest individual files under `path`."""
    return await svc.top_files_sync(path, limit)


# ── Background scan jobs ─────────────────────────────────────────────────────

@router.post("/scans")
async def start_scan(req: ScanRequest):
    """Start a scan job; poll GET /scans/{job_id} for progress and the result."""
    return svc.start_scan(req.path, req.depth, req.mode, req.limit)


@router.get("/scans/{job_id}")
async def get_scan(job_id: str):
    """Progress snapshot of one scan job (state, entries, current dir, result)."""
    return svc.get_scan(job_id)


@router.post("/scans/{job_id}/cancel")
async def cancel_scan(job_id: str):
    """Ask a running scan job to stop; the worker checks the flag as it walks."""
    return svc.cancel_scan(job_id)


# ── Suggestions ──────────────────────────────────────────────────────────────

@router.get("/suggestions")
async def suggestions(refresh: bool = Query(default=False)):
    """Cleanup candidates across the system, cached for two minutes."""
    return await asyncio.to_thread(svc.get_suggestions, refresh)


# ── Delete ───────────────────────────────────────────────────────────────────

@router.post("/delete")
async def delete_paths(req: DeleteRequest, request: Request):
    """Delete paths — recoverable by default when `mode="trash"` is requested."""
    return await svc.delete_paths(
        req.paths, mode=req.mode, run_async=req.run_async, username=_username(request))


# ── Managed caches ───────────────────────────────────────────────────────────

@router.post("/caches/clean")
async def clean_caches(req: CacheCleanRequest):
    """Reclaim EthOS-managed caches (thumbnails, transcodes, uploads, trash)."""
    return await svc.clean_caches(req.areas)


# ── Journal vacuum ───────────────────────────────────────────────────────────

@router.post("/vacuum-journal")
async def vacuum_journal(req: VacuumRequest):
    """Remove systemd journal entries older than `days` days."""
    return await svc.vacuum_journal(req.days)


# ── History ──────────────────────────────────────────────────────────────────

@router.get("/history")
async def history(limit: int = Query(default=50, ge=1, le=svc.HISTORY_LIMIT)):
    """Recent reclaim actions (newest first)."""
    return await asyncio.to_thread(svc.history, limit)
