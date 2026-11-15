"""EthOS NAS — Sync API for device clients (e.g. EthOS Photos Android app)

Purpose-built, minimal file-sync endpoints so mobile devices can sync media to
the NAS over plain HTTP — no SSH/SFTP or SMB access required on the server.

Auth: every endpoint requires a valid JWT (enforced by the global auth
middleware in main.py; /api/sync is NOT in public_paths). Tokens are obtained
via POST /api/auth/login with Linux credentials.

REST:
- POST   /api/sync/mkdir    — create directory tree recursively (idempotent)
- GET    /api/sync/stat     — metadata for one path ({exists, is_dir, size})
- GET    /api/sync/list     — entry names directly inside a directory
- POST   /api/sync/upload   — stream raw request body to a path; supports resume
                               via ?offset=N (existing file must be exactly N bytes)
- POST   /api/sync/rename   — atomic rename {from, to}
- DELETE /api/sync/delete   — best-effort delete (missing paths are not an error)

Paths are absolute filesystem paths. They are resolved through
`services/path_scope.py`, so non-admin accounts are limited to /home, /mnt and
/media exactly like the File Manager.
"""

import asyncio
import logging
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.services import path_scope

logger = logging.getLogger("ethos-nas.routers.sync")
router = APIRouter(dependencies=[Depends(path_scope.scope_request)])


def _abs(path: str) -> str:
    """Resolve to an absolute path and enforce the per-role scope."""
    return path_scope.sanitize(path)


class MkdirRequest(BaseModel):
    path: str


class RenameRequest(BaseModel):
    src: str = Field(alias="from")
    dst: str = Field(alias="to")

    model_config = {"populate_by_name": True}


@router.post("/mkdir")
async def api_mkdir(body: MkdirRequest) -> dict:
    """Create a directory tree recursively. Idempotent — existing dirs are ok."""
    abs_path = _abs(body.path)
    if os.path.isdir(abs_path):
        return {"ok": True, "path": abs_path, "created": False}
    try:
        os.makedirs(abs_path, exist_ok=True)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"Permission denied creating {abs_path}") from exc
    except OSError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not os.access(abs_path, os.W_OK):
        raise HTTPException(status_code=403, detail="Directory is not writable")
    return {"ok": True, "path": abs_path, "created": True}


@router.get("/stat")
async def api_stat(path: str = Query(...)) -> dict:
    """Metadata for a single file or directory. 404 when missing."""
    abs_path = _abs(path)
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="Path not found")
    try:
        st = os.stat(abs_path)
    except OSError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    is_dir = os.path.isdir(abs_path)
    return {"exists": True, "is_dir": is_dir, "size": 0 if is_dir else st.st_size}


@router.get("/list")
async def api_list(dir: str = Query(...)) -> dict:
    """Names of entries directly inside a directory. 404 when missing."""
    abs_dir = _abs(dir)
    if not os.path.isdir(abs_dir):
        raise HTTPException(status_code=404, detail="Directory not found")
    try:
        names = sorted(await asyncio.to_thread(os.listdir, abs_dir))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Permission denied") from exc
    return {"dir": abs_dir, "names": names}


@router.post("/upload")
async def api_upload(request: Request, path: str = Query(...), offset: int = Query(0, ge=0)) -> dict:
    """Stream the raw request body to [path].

    - offset == 0: file is created/truncated and written from the start.
    - offset > 0: resume — an existing file of exactly `offset` bytes is
      required; the body (the remaining suffix) is appended at that position.
    Returns {ok, path, size} with the final on-disk size.
    """
    abs_path = _abs(path)
    parent = os.path.dirname(abs_path)
    if not os.path.isdir(parent):
        raise HTTPException(status_code=409, detail="Destination directory does not exist")

    try:
        if offset > 0:
            if not os.path.isfile(abs_path) or os.path.getsize(abs_path) != offset:
                raise HTTPException(
                    status_code=409,
                    detail=f"Offset {offset} does not match existing file size",
                )
            fh = open(abs_path, "r+b")
        else:
            fh = open(abs_path, "wb")
    except OSError as exc:
        raise HTTPException(status_code=403, detail=f"Cannot write to {abs_path}: {exc}") from exc

    written = offset
    try:
        with fh:
            if offset > 0:
                fh.seek(offset)
            async for chunk in request.stream():
                if not chunk:
                    continue
                fh.write(chunk)
                written += len(chunk)
    except OSError as exc:
        msg = str(exc)
        code = 507 if "No space left" in msg else 403
        raise HTTPException(status_code=code, detail=msg) from exc

    logger.info("Sync upload complete: %s (%d bytes)", abs_path, written)
    return {"ok": True, "path": abs_path, "size": written}


@router.post("/rename")
async def api_rename(body: RenameRequest) -> dict:
    """Atomic rename within the same filesystem. 404 when source is missing."""
    src = _abs(body.src)
    dst = _abs(body.dst)
    if not os.path.exists(src):
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        os.rename(src, dst)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "from": src, "to": dst}


@router.delete("/delete")
async def api_delete(path: str = Query(...)) -> dict:
    """Best-effort delete of a file or directory tree. Missing paths are ok."""
    abs_path = _abs(path)
    if not os.path.exists(abs_path):
        return {"ok": True, "deleted": False}
    try:
        def _delete() -> None:
            if os.path.isdir(abs_path):
                shutil.rmtree(abs_path)
            else:
                os.remove(abs_path)

        # Device-initiated deletes can remove whole trees (voice memos, photo
        # albums) — never on the event loop.
        await asyncio.to_thread(_delete)
    except OSError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"ok": True, "deleted": True}
