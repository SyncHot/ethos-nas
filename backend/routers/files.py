"""EthOS NAS — File Manager Router

REST:
- GET    /api/files?path=...      — list directory
- POST   /api/files/mkdir         — create directory
- POST   /api/files/touch         — create empty file
- DELETE /api/files               — delete file/directory
- PUT    /api/files/rename        — rename
- GET    /api/files/download?path — download file
- GET    /api/files/raw?path      — serve raw content (Range-capable)
- POST   /api/files/upload?path=  — upload one or more files to a directory
- GET    /api/files/search?q=&path= — recursive name search from a directory
- GET    /api/files/universal-search?q=[&roots=&type=&min_size_mb=] — global multi-root search
- GET    /api/files/search-roots  — sensible scopes for universal search
- POST   /api/files/trash         — move to trash
- GET    /api/files/trash         — list trash
- POST   /api/files/trash/restore — restore from trash
- DELETE /api/files/trash         — empty trash
- GET    /api/files/thumbnail?path&size        — image thumbnail (WebP, cached)
- GET    /api/files/video-thumbnail?path&size  — video frame (WebP, cached; 202 when queued)
- POST   /api/files/warm-thumbnails?paths=     — pre-generate thumbnails in background
- GET    /api/files/transcode-status?path      — transcode job status
- POST   /api/files/transcode?path             — start H.264 transcode for browser playback
- POST   /api/files/extract                     — extract an archive next to itself
"""

# ── Module map — grep a section name, then read only that slice ─────────────
# Thumbnail cache      sidecar meta (mtime+size), eviction hooks
# Video thumbnails     ffmpeg frame extraction + warm endpoints
# Custom covers        user-captured video covers
# Face detection       movie thumbnails prefer a face
# Transcode            H.264 remux for browser playback
# Routes               list, mkdir/touch, delete/rename, download/raw, upload
# Favorites            per-user starred locations
# Image/video thumbs   GET endpoints
# Search               recursive + universal multi-root search
# Trash                move/list/restore/empty

import asyncio
from functools import lru_cache
import contextlib
import hashlib
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from backend.config import settings
from backend.models import (
    FavoriteRequest, FileContentRequest, FileExtractRequest, FilePathRequest, FileRenameRequest,
    MkdirRequest, TrashRestoreRequest, VideoCoverRequest,
)
from backend.services import json_store
from backend.services import path_scope
from backend.services import search_service
from backend.services import media_cache
from backend.services import system_deps
from backend.services import task_registry
from backend.services import transcode
# Thumbnail engine + covers + face crop (services/thumbnails.py) and the
# transcode worker (services/transcode.py) — the endpoints below stay thin.
from backend.services.thumbnails import (  # noqa: F401 — re-exported for endpoints
    VIDEO_THUMB_WIDTH,
    _cover_is_fresh,
    _cover_master_file,
    _cover_slot_is_fresh,
    _grab_cover_frame_sync,
    _queue_video_thumb,
    _render_cover_to_cache,
    _render_image_thumb,
    _render_video_slot,
    _video_cache_base,
    _video_fallback_svg,
    _video_master_file,
    _video_master_is_fresh,
    _video_slot_file,
    _video_slot_is_fresh,
    _video_thumb_backoff,
    _VIDEO_EXTS,
    _VIDEO_THUMB_VERSION,
)
from backend.services import trash_service
from backend.services.files_service import (
    MissingExtractor, broadcast_mutation, extract_archive, is_extractable_name,
)

try:
    import grp
except ImportError:  # grp is Linux-only
    grp = None  # type: ignore[assignment]
try:
    import pwd
except ImportError:  # pwd is Linux-only
    pwd = None  # type: ignore[assignment]

logger = logging.getLogger("ethos-nas.routers.files")
# Identity scoping lives in services/path_scope.py so sync.py and file_ops.py
# share the exact same rules (TODO.md P0-06).
_scope_request = path_scope.scope_request


router = APIRouter(dependencies=[Depends(_scope_request)])

# Recycle bin storage lives in services/trash_service.py (per-user namespaces).
# Derived-media caches (thumbnails, covers, transcodes) live in
# services/media_cache.py — one sidecar-meta freshness rule for all of them.
THUMB_DIR = media_cache.THUMB_DIR
TRANSCODE_DIR = media_cache.TRANSCODE_DIR
FAVORITES_FILE = settings.DATA_DIR / "favorites.json"
_ensure_thumb_dir = media_cache.ensure_thumb_dir
_make_thumb_key = media_cache.make_thumb_key
_thumb_meta_file = media_cache.thumb_meta_file
_cache_is_fresh = media_cache.cache_is_fresh
_write_thumb_meta = media_cache.write_thumb_meta
_read_thumb_meta = media_cache.read_thumb_meta
_cleanup_stale_thumb_artifacts = media_cache.cleanup_stale_thumb_artifacts
_probe_duration = media_cache.probe_duration
_append_bytes = media_cache.append_bytes

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/svg+xml"}

# Supported Pillow image types (excludes SVG which needs separate handling)
_PIL_OPENABLE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _make_thumb_key(abs_path: str, size: int) -> str:
    h = hashlib.sha256(f"{abs_path}:{size}".encode()).hexdigest()[:16]
    return f"{h}_{size}"


def _sanitize(path: str) -> str:
    """Resolve to an absolute path and enforce the per-identity scope.

    Delegates to services/path_scope.py so sync.py and file_ops.py apply the
    identical rule: non-admins are limited to their own home, /mnt and /media.
    Write endpoints must keep using this function.
    """
    return path_scope.sanitize(path)


def _sanitize_read(path: str) -> str:
    """Scope check for read-only endpoints, including received shares."""
    return path_scope.sanitize_read(path)


def _permission_denied(exc: Exception, path: str) -> HTTPException:
    """Build a 403 that explains the service-user permission model.

    The server runs as a dedicated service user (see docs/service-user.md),
    not as the logged-in PAM user — so a PermissionError means the *service*
    user lacks access to this path, not the human. Spell out owner/mode and
    the fix instead of a bare errno.
    """
    owner = group = "?"
    mode = "?"
    me = "the server user"
    probe = path if os.path.exists(path) else os.path.dirname(path)
    try:
        st = os.stat(probe)
        if pwd is not None:
            owner = pwd.getpwuid(st.st_uid).pw_name
            me = pwd.getpwuid(os.getuid()).pw_name
        if grp is not None:
            group = grp.getgrgid(st.st_gid).gr_name
        mode = format(st.st_mode & 0o7777, "o").rjust(4, "0")
    except (OSError, KeyError):
        pass  # probe failure means 'not present'
    logger.error("Permission denied on %s: running as %s, %s is %s:%s mode %s",
                 path, me, probe, owner, group, mode)
    if pwd is None:
        hint = "Fix: re-run `sudo bash setup_scripts/setup_service.sh` (grants the service user an ACL on the admin home)."
    else:
        hint = (f"Fix: re-run `sudo bash setup_scripts/setup_service.sh` (grants the service user "
                f"an ACL on the admin home), or grant this path directly: "
                f"`sudo setfacl -R -m u:{me}:rwx -dm u:{me}:rwx '{probe}'`.")
    return HTTPException(
        status_code=403,
        detail=(f"Permission denied — the server runs as '{me}', not your login user, "
                f"and cannot access {path} ({probe} is owned by {owner}:{group}, mode {mode}). "
                + hint),
    )


@lru_cache(maxsize=1024)
def _user_name(uid: int) -> str:
    """Cached uid → name lookup (NSS hits dominated large directory listings)."""
    try:
        return pwd.getpwuid(uid).pw_name if pwd is not None else str(uid)
    except (KeyError, OSError):
        return str(uid)


@lru_cache(maxsize=1024)
def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name if grp is not None else str(gid)
    except (KeyError, OSError):
        return str(gid)


def _format_info(name: str, fpath: str, stat, is_dir: bool) -> dict:
    """Build the listing payload from an already-fetched stat."""
    ext = Path(name).suffix.lower()
    mtime = stat.st_mtime

    if is_dir:
        ftype = "directory"
    elif ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"):
        ftype = "image"
    elif ext in (".mp4", ".avi", ".mkv", ".mov", ".webm"):
        ftype = "video"
    elif ext in (".mp3", ".ogg", ".wav", ".flac", ".aac"):
        ftype = "audio"
    elif ext == ".pdf":
        ftype = "document"
    elif ext in (".txt", ".md", ".log", ".rst"):
        ftype = "text"
    elif is_extractable_name(name):
        ftype = "archive"
    elif ext in (".py", ".js", ".ts", ".html", ".css", ".json", ".yaml",
                 ".yml", ".sh", ".bash", ".xml", ".csv", ".toml"):
        ftype = "code"
    else:
        ftype = "other"

    return {
        "name": name,
        "path": fpath,
        "size": stat.st_size,
        "modified": mtime,
        "created": stat.st_ctime,
        "is_dir": is_dir,
        "file_type": ftype,
        "permissions": oct(stat.st_mode)[-3:],
        "owner": _user_name(stat.st_uid),
        "group": _group_name(stat.st_gid),
    }


def _file_info(fpath: str) -> dict | None:
    """Return metadata dict for a file or directory, None on error."""
    try:
        stat = os.stat(fpath)
        return _format_info(os.path.basename(fpath), fpath, stat, os.path.isdir(fpath))
    except PermissionError as exc:
        # An unreadable entry silently disappears from the listing — leave a
        # trace so "my folder is empty" reports can be diagnosed.
        logger.warning("Cannot stat %s: %s", fpath, exc)
        return None
    except OSError as exc:
        logger.warning("Cannot stat %s: %s", fpath, exc)
        return None


# Video thumbnail engine (master frame, per-size slots, covers, face crop)
# lives in services/thumbnails.py; transcodes in services/transcode.py.

# ── Transcode (H.264 remux for browser playback) ─────────────────────────────
# The ffmpeg worker and the job registry live in services/transcode.py.


@router.get("/transcode-status")
async def transcode_status(path: str = Query(...), height: int = Query(720, ge=360, le=1080)) -> dict:
    """Report status of the H.264 transcode for a video file."""
    abs_path = _sanitize_read(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")
    return transcode.status_payload(abs_path, height)


@router.get("/transcode-stream")
async def transcode_stream(path: str = Query(...), height: int = Query(720, ge=360, le=1080)):
    """Serve the H.264 transcoded file (Range-capable) for browser playback.

    The player should poll ``/transcode-status`` until ``done`` before
    requesting this — a partially written file is not Range-stable, so while
    the job runs we return 202 + Retry-After instead of streaming garbage.
    """
    abs_path = _sanitize_read(path)
    if transcode.output_is_fresh(abs_path, height):
        return FileResponse(str(transcode.output_file(abs_path, height)),
                            media_type="video/mp4")
    running = transcode.active(abs_path)
    if running is None:
        # Stale or missing output with no active job — nothing to stream.
        raise HTTPException(status_code=404, detail="No transcode available")
    return Response(
        status_code=202,
        headers={"Retry-After": "5", "X-Transcode-Status": running},
    )


@router.post("/transcode")
async def start_transcode(path: str = Query(...), height: int = Query(720, ge=360, le=1080)) -> dict:
    """Start (or report) an H.264 transcode so the browser can play the file.

    Browsers cannot decode HEVC/AV1 8K streams; transcoding to H.264 ≤1080p
    makes any video playable in the built-in player. One job at a time —
    concurrent requests for other files are queued by the semaphore and
    reported as ``queued``. Transcoding only reads the source and writes a
    cache file, so a received share may use it.
    """
    abs_path = _sanitize_read(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")

    ext = Path(abs_path).suffix.lower()
    if ext not in _VIDEO_EXTS:
        raise HTTPException(status_code=400, detail="Not a supported video format")
    return await transcode.start(abs_path, height)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("")
async def list_directory(
    path: str = Query("/"),
    limit: int = Query(0, ge=0, le=100_000),
    offset: int = Query(0, ge=0),
) -> dict:
    """List contents of a directory.

    Args:
        path: Absolute filesystem path to the directory
    """
    abs_path = _sanitize_read(path)
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="Path not found")
    if not os.path.isdir(abs_path):
        raise HTTPException(status_code=400, detail="Path is not a directory")

    def _list() -> tuple[int, list[dict]]:
        # Two-phase so a paged request never stats the whole directory:
        #   1. `scandir` gives name + type from the directory entry itself
        #      (d_type on ext4/xfs — no syscall), sorted dirs-first/name.
        #   2. only the requested slice is stat()'d and formatted.
        # uid/gid → name lookups are cached (`_user_name`/`_group_name`).
        entries: list[tuple[str, str, bool]] = []
        with os.scandir(abs_path) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir(follow_symlinks=True)
                except OSError:
                    is_dir = False
                entries.append((entry.name, entry.path, is_dir))
        entries.sort(key=lambda e: (0 if e[2] else 1, e[0].lower()))
        window = entries[offset:offset + limit] if limit else entries[offset:]
        page = [info for info in (_file_info(path) for _name, path, _dir in window) if info]
        return len(entries), page

    try:
        total, files = await asyncio.to_thread(_list)
    except PermissionError as exc:
        raise _permission_denied(exc, abs_path) from exc

    return {
        "path": abs_path,
        "files": files,
        "total": total,
        "limit": limit,
        "offset": offset,
        # Own scope only: a received share is always read-only.
        "can_write": path_scope.can_write(abs_path),
    }


@router.get("/stat")
async def stat_item(path: str = Query("/")) -> dict:
    """Return metadata for a single file or directory (for the Properties dialog)."""
    abs_path = _sanitize_read(path)
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="Path not found")
    info = _file_info(abs_path)
    if not info:
        raise HTTPException(status_code=403, detail="Permission denied")
    if info["is_dir"]:
        try:
            info["item_count"] = await asyncio.to_thread(lambda: len(os.listdir(abs_path)))
        except OSError:
            info["item_count"] = None
        info["size"] = 0
    info["can_write"] = path_scope.can_write(abs_path)
    return info


# ── Favorites (starred locations, per user) ──────────────────────────────────

_FAVORITES_STORE = json_store.get(FAVORITES_FILE)  # {username: [path, ...]}


def _load_favorites() -> dict:
    data = _FAVORITES_STORE.load()  # quarantines a corrupt file and logs it
    return data if isinstance(data, dict) else {}


def _save_favorites(data: dict):
    _FAVORITES_STORE.save(data)


def _current_username(request: Request) -> str:
    payload = getattr(request.state, "current_user", None) or {}
    return payload.get("username") or "default"


def _favorite_on_unmounted_media(path: str) -> bool:
    """True when a *missing* favorite sits on removable media that is offline.

    A folder under /mnt or /media is not a dead link while its volume is
    unplugged: the entry is kept and reported ``exists: false``. Once the
    volume is mounted again, a missing folder underneath it has really been
    deleted and gets pruned with the rest.
    """
    if not any(path == root or path.startswith(root + "/") for root in ("/mnt", "/media")):
        return False
    probe = os.path.dirname(path)
    while probe and probe != "/":
        if os.path.exists(probe):
            return not os.path.ismount(probe)
        probe = os.path.dirname(probe)
    return True


def _prune_missing_favorites(data: dict, user: str) -> list[str]:
    """Drop starred folders that no longer exist (offline volumes are kept)."""
    if not isinstance(data, dict):
        return []
    paths = [p for p in (data.get(user) or []) if isinstance(p, str)]
    kept = [p for p in paths if os.path.isdir(p) or _favorite_on_unmounted_media(p)]
    dropped = len(paths) - len(kept)
    if dropped:
        logger.info("Pruned %d missing favorite(s) for %s", dropped, user)
        data[user] = kept
    return kept


@router.get("/favorites")
async def list_favorites(request: Request) -> dict:
    """List starred folders for the current user (dead links are pruned)."""
    user = _current_username(request)
    paths = [p for p in (_load_favorites().get(user) or []) if isinstance(p, str)]
    # Stale entries accumulate when folders are deleted outside the UI (another
    # app, SSH, a removed share), so reconcile on read — a cheap isdir() per
    # star — instead of showing phantom folders in the sidebar forever.
    if any(not os.path.isdir(p) and not _favorite_on_unmounted_media(p) for p in paths):
        paths = _FAVORITES_STORE.update(lambda data: _prune_missing_favorites(data, user))
    favorites = [
        {"path": p, "name": os.path.basename(p.rstrip("/")) or p, "exists": os.path.isdir(p)}
        for p in paths
    ]
    return {"favorites": favorites, "total": len(favorites)}


@router.put("/favorites")
async def set_favorite(body: FavoriteRequest, request: Request) -> dict:
    """Star or unstar a folder."""
    abs_path = _sanitize(body.path)
    user = _current_username(request)

    def _mutate(data: dict) -> list[str]:
        # Atomic read-modify-write: two clicks in different windows must not
        # clobber each other's star (TODO.md P2-11).
        if not isinstance(data, dict):
            data.clear()
        paths = [p for p in (data.get(user) or []) if isinstance(p, str)]
        if body.starred and abs_path not in paths:
            paths.append(abs_path)
        elif not body.starred:
            paths = [p for p in paths if p != abs_path]
        data[user] = paths
        return paths

    paths = _FAVORITES_STORE.update(_mutate)
    return {"ok": True, "favorites": paths}


@router.post("/mkdir")
async def make_directory(body: MkdirRequest) -> dict:
    """Create a new directory."""
    abs_path = _sanitize(body.path)
    if os.path.exists(abs_path):
        raise HTTPException(status_code=409, detail="Path already exists")
    try:
        os.makedirs(abs_path)
    except PermissionError as exc:
        raise _permission_denied(exc, abs_path) from exc
    except OSError as exc:
        logger.error("mkdir failed for %s: %s", abs_path, exc)
        raise HTTPException(status_code=500, detail=f"Cannot create folder: {exc}") from exc
    broadcast_mutation(
        "mkdir", os.path.dirname(abs_path),
        [{"name": os.path.basename(abs_path)}],
    )
    return {"ok": True, "path": abs_path}


@router.post("/touch")
async def create_file(body: FilePathRequest) -> dict:
    """Create an empty file."""
    abs_path = _sanitize(body.path)
    if os.path.exists(abs_path):
        raise HTTPException(status_code=409, detail="File already exists")
    try:
        Path(abs_path).touch()
    except PermissionError as exc:
        raise _permission_denied(exc, abs_path) from exc
    except OSError as exc:
        logger.error("touch failed for %s: %s", abs_path, exc)
        raise HTTPException(status_code=500, detail=f"Cannot create file: {exc}") from exc
    broadcast_mutation(
        "touch", os.path.dirname(abs_path),
        [{"name": os.path.basename(abs_path)}],
    )
    return {"ok": True, "path": abs_path}


def _emit_extract_event(kind: str, title: str, message: str) -> None:
    """Emit a Notification Center event; never raises (notification failures must not break extraction)."""
    try:
        from backend.services.notifications_service import emit_event
        emit_event(kind, title, message=message, source="files")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to emit extract notification: %s", exc)


def _clean_extract_subfolder(value: str | None) -> str | None:
    """Validate the optional folder name of an extraction request."""
    if value is None:
        return None
    name = value.strip()
    if not name:
        return ""
    if name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise HTTPException(status_code=400,
                            detail="Folder name must not contain path separators")
    if len(name) > 200:
        raise HTTPException(status_code=400,
                            detail="Folder name is too long (max 200 characters)")
    return name


@router.post("/extract")
async def extract_archive_api(body: FileExtractRequest) -> dict:
    """Extract an archive — next to itself, or into a folder the user picks.

    Body: {path, dest_dir?, subfolder?, overwrite?, password?}.
    ``subfolder`` None → a folder named after the archive (collision-suffixed,
    never overwrites); "" → straight into ``dest_dir``; any other value → that
    folder. Split volumes (.part1.rar/.7z.001/.r00) resolve to their first
    volume. Runs in a worker thread; every path goes through the scope check.
    """
    abs_path = _sanitize(body.path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")
    if not is_extractable_name(abs_path):
        ext = Path(abs_path).suffix.lower()
        raise HTTPException(status_code=400,
                            detail=f"Not a supported archive type ({ext or 'no extension'})")

    dest = _sanitize(body.dest_dir) if body.dest_dir else os.path.dirname(abs_path)
    if not os.path.isdir(dest):
        raise HTTPException(status_code=400, detail=f"Destination is not a directory: {dest}")
    subfolder = _clean_extract_subfolder(body.subfolder)

    try:
        result = await asyncio.to_thread(
            extract_archive,
            abs_path,
            dest_dir=dest,
            subfolder=subfolder,
            overwrite=bool(body.overwrite),
            password=body.password or None,
        )
    except MissingExtractor as exc:
        # Needs a host extractor that is not installed. The UI offers a one-click
        # admin install for `missing_requirement` instead of a dead end.
        logger.warning("Extraction needs host tools for %s: %s", abs_path, exc)
        _emit_extract_event("error", "Extraction failed", str(exc))
        return JSONResponse(status_code=409, content={
            "detail": str(exc),
            "missing_requirement": exc.requirement,
            "hint": system_deps.install_hint(exc.requirement),
        })
    except ValueError as exc:  # unsupported/corrupt/unsafe archive — client error
        logger.error("Extract failed for %s: %s", abs_path, exc)
        _emit_extract_event("error", "Extraction failed", str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        _emit_extract_event("error", "Extraction failed", f"Permission denied: {dest}")
        raise _permission_denied(exc, dest) from exc
    except Exception as exc:
        logger.error("Extract error for %s: %s", abs_path, exc, exc_info=True)
        _emit_extract_event("error", "Extraction failed", f"{abs_path}: {exc}")
        raise HTTPException(status_code=500, detail=f"Extraction failed: {exc}") from exc

    dest_dir = result["dest_dir"]
    count = result["extracted_count"]
    skipped = result.get("skipped") or []
    skipped_existing = int(result.get("skipped_existing") or 0)
    notes = []
    if skipped:
        notes.append(f"{len(skipped)} special file(s) skipped")
    if skipped_existing:
        notes.append(f"{skipped_existing} existing file(s) kept")
    skip_note = (" (" + ", ".join(notes) + ")") if notes else ""
    logger.info("Extracted %s → %s (%d files%s, tool=%s)",
                abs_path, dest_dir, count, skip_note, result.get("tool") or "stdlib")
    if skipped:
        logger.warning("Skipped special files in %s: %s", abs_path, ", ".join(skipped[:20]))
    broadcast_mutation(
        "extract", dest_dir,
        [{"name": os.path.basename(dest_dir) or dest_dir, "status": "ok"}],
    )
    _emit_extract_event("success", "Archive extracted",
                        f'"{os.path.basename(abs_path)}" → {dest_dir} ({count} files{skip_note})')
    return {
        "ok": True,
        "dest_dir": dest_dir,
        "extracted_count": count,
        "skipped_count": len(skipped),
        "skipped": skipped[:20],  # cap the payload; full list is in the server log
        "skipped_existing": skipped_existing,
        "tool": result.get("tool") or "stdlib",
        "archive": result.get("archive") or abs_path,
    }


@router.delete("")
async def delete_item(body: FilePathRequest) -> dict:
    """Delete a file or directory (recursive). Runs in a worker thread so
    large recursive deletes never block the event loop."""
    abs_path = _sanitize(body.path)
    if not os.path.lexists(abs_path):
        raise HTTPException(status_code=404, detail="Path not found")

    def _remove() -> None:
        # isdir() follows symlinks while rmtree() refuses them, so deleting a
        # link to a folder used to raise 500 and the entry could never be
        # removed. A symlink is always unlinked, never followed.
        if os.path.islink(abs_path):
            os.unlink(abs_path)
        elif os.path.isdir(abs_path):
            shutil.rmtree(abs_path)
        else:
            os.remove(abs_path)

    try:
        await asyncio.to_thread(_remove)
    except PermissionError as exc:
        raise _permission_denied(exc, abs_path) from exc
    except OSError as exc:
        logger.error("delete failed for %s: %s", abs_path, exc)
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}") from exc
    broadcast_mutation(
        "delete_hard", os.path.dirname(abs_path),
        [{"name": os.path.basename(abs_path)}],
    )
    return {"ok": True, "path": abs_path}


@router.put("/rename")
async def rename_item(body: FileRenameRequest) -> dict:
    """Rename a file or directory.

    Refuses to overwrite an existing sibling: ``os.rename`` would silently
    replace the target file (data loss), so a collision is a 409 instead. The
    new name must stay inside the parent directory — ``/`` and ``\\`` are
    rejected, as are the names ``.`` and ``..`` (which would move the item out
    of the directory the UI thinks it is renaming in).
    """
    abs_path = _sanitize(body.path)
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="Path not found")
    new_name = (body.new_name or "").strip()
    if "/" in new_name or "\\" in new_name or not new_name or new_name in (".", ".."):
        logger.warning("Rename rejected for %s: invalid name %r", abs_path, body.new_name)
        raise HTTPException(status_code=400, detail="Invalid new name")
    new_path = os.path.join(os.path.dirname(abs_path), new_name)
    if os.path.abspath(new_path) == abs_path:
        return {"ok": True, "path": abs_path}  # unchanged — nothing to do
    if os.path.lexists(new_path):
        logger.warning("Rename rejected: %s already exists (target of %s)", new_path, abs_path)
        raise HTTPException(status_code=409, detail=f"'{new_name}' already exists")
    try:
        os.rename(abs_path, new_path)
    except PermissionError as exc:
        raise _permission_denied(exc, abs_path) from exc
    except OSError as exc:
        logger.error("Rename failed %s → %s: %s", abs_path, new_path, exc)
        raise HTTPException(status_code=500, detail=f"Rename failed: {exc}") from exc
    # Notify other Files windows so the parent directory refreshes.
    broadcast_mutation(
        "rename", os.path.dirname(abs_path),
        [{"name": new_name, "old_name": os.path.basename(abs_path)}],
    )
    return {"ok": True, "path": new_path}


@router.get("/download")
async def download_file(path: str = Query(...)) -> FileResponse:
    """Download a file as an attachment (own files or a received share)."""
    abs_path = _sanitize_read(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(abs_path, filename=os.path.basename(abs_path))


@router.get("/raw")
async def raw_file(path: str = Query(...)) -> FileResponse:
    """Serve a file inline (for preview / streaming).

    Starlette's ``FileResponse`` handles HTTP Range requests natively, so the
    browser can seek and stream large videos without downloading them first.

    The response is locked down: without ``nosniff`` an uploaded ``.html`` or
    ``.svg`` is served as an active document from the NAS origin, so anyone who
    can upload could run script with a victim's session. The sandbox CSP keeps
    such a document inert while images and video still render normally.
    """
    abs_path = _sanitize_read(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(abs_path, headers={
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:",
    })

@router.put("/content")
async def write_file_content(body: FileContentRequest) -> dict:
    """Write text content to an existing file (Text Editor save).

    The editor detects the source encoding on load (UTF-8, falling back to
    Latin-1) and sends it back here so round-trips preserve non-UTF-8 files.
    Binary editing is not supported — only plain-text content.
    """
    abs_path = _sanitize(body.path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")
    # Conflict detection: the editor sends the mtime it saw at load time; if the
    # file changed on disk in the meantime (git pull, another editor, cron),
    # refuse to clobber it silently — the UI then offers reload/overwrite.
    if body.mtime is not None:
        try:
            cur_mtime = os.stat(abs_path).st_mtime
        except OSError as exc:
            raise HTTPException(status_code=404, detail="File not found") from exc
        if abs(cur_mtime - body.mtime) > 0.001:
            logger.info("Write to %s rejected: modified on disk (mtime %.6f → %.6f)",
                        abs_path, body.mtime, cur_mtime)
            raise HTTPException(
                status_code=409,
                detail="File was modified on disk after it was opened",
            )
    enc = "latin-1" if body.encoding.lower() in ("latin-1", "latin1", "iso-8859-1") else "utf-8"

    def _atomic_write() -> None:
        """Write to a sibling temp file, fsync, then swap it in.

        Opening the real path with "w" truncated it immediately, so a crash, a
        UnicodeEncodeError or a full disk mid-write left the user with a
        zero-length source file. Runs in a thread — a 5 MB write on a busy mount
        used to block the event loop.
        """
        tmp = abs_path + ".ethos-tmp"
        try:
            with open(tmp, "w", encoding=enc) as f:
                f.write(body.content)
                f.flush()
                os.fsync(f.fileno())
            with contextlib.suppress(OSError):
                shutil.copymode(abs_path, tmp)
            os.replace(tmp, abs_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise

    try:
        await asyncio.to_thread(_atomic_write)
    except PermissionError as exc:
        raise _permission_denied(exc, abs_path) from exc
    except UnicodeEncodeError as e:
        # Client error (the text cannot be represented in the file's encoding).
        raise HTTPException(status_code=400, detail=f"Content is not valid {enc}: {e}") from e
    except OSError as e:
        logger.error("Write to %s failed: %s", abs_path, e)
        raise HTTPException(status_code=500, detail=f"Write failed: {e}") from e
    # Return the new mtime so the editor can keep its conflict-detection
    # baseline up to date without a second stat round-trip.
    try:
        new_mtime = os.stat(abs_path).st_mtime
    except OSError:
        new_mtime = None
    logger.info("Wrote %d bytes to %s", len(body.content.encode(enc)), abs_path)
    return {"ok": True, "path": abs_path, "bytes": len(body.content.encode(enc)),
            "mtime": new_mtime}


# ── Image Thumbnail ───────────────────────────────────────────────────────────

_THUMB_SEM = asyncio.Semaphore(4)          # CPU-bound PIL renders at once
_THUMB_INFLIGHT: dict[str, asyncio.Task] = {}


@router.get("/thumbnail")
async def thumbnail(
    request: Request,
    path: str = Query(...),
    size: int = Query(150, ge=32, le=400),
):
    """Generate a square thumbnail of an image file as WebP.

    Caches thumbnails on disk at ``backend/data/thumbnails/`` keyed by
    MD5 of (absolute_path:size).  Cache validity is tracked via a sidecar
    meta file storing the source mtime+size, so cached thumbs are reused
    across server restarts and only regenerated when the source changes.

    The render runs in a worker thread and is de-duplicated per cache key, so a
    grid scroll that asks for the same thumb twice only cooks it once. Clients
    that append ``&v=<source mtime>`` (the gallery does) get a one-year
    immutable cache; unversioned callers get a short private cache instead.
    """
    abs_path = _sanitize_read(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")

    ext = Path(abs_path).suffix.lower()
    if ext not in _PIL_OPENABLE:
        # For non-Pillow formats (SVG etc.) serve original as fallback
        return FileResponse(abs_path)

    cache_file = THUMB_DIR / f"{_make_thumb_key(abs_path, size)}.webp"
    headers = ({"Cache-Control": "public, max-age=31536000, immutable"}
               if request.query_params.get("v")
               else {"Cache-Control": "private, max-age=600"})

    # Return cached thumbnail if source hasn't changed
    if cache_file.exists() and _cache_is_fresh(cache_file, abs_path):
        return FileResponse(str(cache_file), media_type="image/webp", headers=headers)

    key = str(cache_file)

    async def _render() -> bool:
        async with _THUMB_SEM:
            return await asyncio.to_thread(_render_image_thumb, abs_path, size, cache_file)

    task = _THUMB_INFLIGHT.get(key)
    if task is None or task.done():
        task = task_registry.spawn(_render(), name="file-render")
        _THUMB_INFLIGHT[key] = task
        task.add_done_callback(lambda _t, k=key: _THUMB_INFLIGHT.pop(k, None))
    try:
        await task
    except Exception as exc:
        logger.warning("Thumbnail generation failed for %s: %s", abs_path, exc)
        # Fallback to serving original file
        return FileResponse(abs_path)

    return FileResponse(str(cache_file), media_type="image/webp", headers=headers)


# ── Video Thumbnail ───────────────────────────────────────────────────────────

@router.get("/video-thumbnail")
async def video_thumbnail(
    path: str = Query(...),
    size: int = Query(320, ge=64, le=640),
    face: int = Query(1, ge=0, le=1),
):
    """Serve a cached video frame; enqueue extraction on a miss.

    One master frame per file is extracted by a bounded, niced background
    worker and every size is a cheap PIL downscale of it. A miss answers
    **202 Accepted** with ``Retry-After`` so the client re-requests instead of
    holding the connection; repeated failures back off exponentially per file.

    Args:
        path: Absolute filesystem path to the video file.
        size: Thumbnail width in pixels (64-640, default 320).
        face: 1 (default) allows the optional face-aware crop used by Photos;
            File Manager sends 0 and gets a plain frame from its own cache slot.
    """
    abs_path = _sanitize_read(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")

    ext = Path(abs_path).suffix.lower()
    if ext not in _VIDEO_EXTS:
        raise HTTPException(status_code=400, detail="Not a supported video format")

    use_face = bool(face)
    _ensure_thumb_dir()
    width = min(int(size), VIDEO_THUMB_WIDTH)

    # no-cache so a freshly captured cover shows up on the next grid load;
    # unchanged entries still answer as cheap 304s via If-Modified-Since.
    def _serve(p: Path) -> FileResponse:
        return FileResponse(str(p), media_type="image/webp",
                            headers={"Cache-Control": "no-cache"})

    # Custom covers are authoritative and live in their own cache slot — the
    # automatic pipeline never writes there, so an in-flight or queued frame
    # extraction can't overwrite a user-picked cover.
    if _cover_is_fresh(abs_path):
        cover_file = (THUMB_DIR / f"{_video_cache_base(abs_path)}_"
                      f"{_VIDEO_THUMB_VERSION}_cover_{width}.webp")
        if not _cover_slot_is_fresh(cover_file, abs_path):
            await asyncio.to_thread(_render_cover_to_cache, abs_path, width, cover_file)
        if cover_file.exists():
            return _serve(cover_file)
        return _video_fallback_svg(width)

    master = _video_master_file(abs_path, use_face)
    if not (master.exists() and _cache_is_fresh(master, abs_path)):
        backoff = _video_thumb_backoff(abs_path, use_face)
        if backoff is not None:
            if backoff.get("permanent"):
                # Hopeless source — show the generic tile instead of re-queueing
                # an ffmpeg attempt on every grid refresh.
                return _video_fallback_svg(width, cache="private, max-age=3600")
            try:
                delay = int(float(backoff.get("next_try", 0)) - time.time())
            except (TypeError, ValueError):
                delay = 2
            return Response(status_code=202,
                            headers={"Retry-After": str(max(2, min(10, delay)))})
        if not _queue_video_thumb(abs_path, use_face):
            # Worker pool saturated — ask the client to poll again shortly.
            return Response(status_code=202, headers={"Retry-After": "5"})
        return Response(status_code=202, headers={"Retry-After": "2"})

    if width >= VIDEO_THUMB_WIDTH:
        return _serve(master)

    slot = _video_slot_file(abs_path, width, use_face)
    if not _video_slot_is_fresh(slot, master, abs_path):
        ok = await asyncio.to_thread(_render_video_slot, master, abs_path, width, slot)
        if not ok:
            # The master is a usable thumbnail at any size.
            return _serve(master)
    return _serve(slot)

@router.post("/video-cover")
async def set_video_cover(body: VideoCoverRequest) -> dict:
    """Set a custom cover for a video file.

    Grabs the frame at time ``t`` (seconds, default 0) with ffmpeg and stores
    it as the full-resolution cover master in the thumbnail cache. From then
    on, every /video-thumbnail request for this file renders from that frame
    instead of the automatic ~10% grab — i.e. the captured frame becomes the
    cover shown for the file in Files (all sizes).

    The cover is tied to the current source file (mtime+size): if the video
    itself changes, it falls back to automatic generation again.
    """
    abs_path = _sanitize(body.path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")

    ext = Path(abs_path).suffix.lower()
    if ext not in _VIDEO_EXTS:
        raise HTTPException(status_code=400, detail="Not a supported video format")

    try:
        t = max(0.0, float(body.t or 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid time") from exc

    duration = await asyncio.to_thread(_probe_duration, abs_path)
    if duration > 0 and t >= max(1.0, duration - 0.5):
        t = max(0.0, duration - 1.0)

    _ensure_thumb_dir()
    master = _cover_master_file(abs_path)
    tmp_file = Path(str(master) + ".tmp")
    try:
        await asyncio.to_thread(_grab_cover_frame_sync, abs_path, t, tmp_file, bool(body.left_half))
        if not (tmp_file.exists() and tmp_file.stat().st_size > 0):
            raise RuntimeError("empty frame")
        os.replace(tmp_file, master)
        _write_thumb_meta(master, abs_path)
    except subprocess.TimeoutExpired as exc:
        logger.warning("Cover capture timed out for %s", abs_path)
        tmp_file.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail="Could not capture cover (timeout)") from exc
    except Exception as exc:
        logger.warning("Cover capture failed for %s at %.2fs: %s", abs_path, t, exc)
        tmp_file.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail=f"Could not capture cover: {exc}") from exc

    return {"ok": True, "path": str(abs_path), "t": round(t, 3)}

@router.post("/warm-thumbnails")
async def warm_thumbnails(paths: str = Query(...)):
    """Pre-generate video thumbnails in the bounded background worker.

    Returns immediately with the number of jobs queued. Paths use the shared
    read scope (thumbs are derived cache, not writes); already-cached and
    backing-off files are skipped so a warm call never burns CPU twice.
    """
    accepted = 0
    for p in [p.strip() for p in paths.split(",") if p.strip()][:60]:
        try:
            abs_path = _sanitize_read(p)
            if not os.path.isfile(abs_path) or Path(abs_path).suffix.lower() not in _VIDEO_EXTS:
                continue
            if _video_master_is_fresh(abs_path):
                continue
            if _video_thumb_backoff(abs_path) is not None:
                continue
            if _queue_video_thumb(abs_path):
                accepted += 1
        except HTTPException:
            continue  # out of scope for this account — skip silently
        except Exception as exc:
            logger.warning("warm-thumbnails failed for %s: %s", p, exc)
    return {"ok": True, "accepted": accepted}


# ── Upload ───────────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload_file(
    path: str = Query(...),
    file: UploadFile | None = None,
):
    """Upload a file to the specified directory.

    Args:
        path: Target directory (must be absolute).
        file: The uploaded file(s). If missing or empty → 422/400.
    """
    abs_path = _sanitize(path)
    if not os.path.isdir(abs_path):
        raise HTTPException(status_code=400, detail="Target path is not a directory")

    if file is None or file.filename is None or file.filename == "":
        raise HTTPException(
            status_code=422,
            detail="No file provided",
        )

    # Multipart filenames are client-controlled — reduce to a single path
    # component so "../" sequences cannot escape the target directory.
    filename = os.path.basename(file.filename.replace("\\", "/").strip()) or "upload"
    # Never silently replace an existing file, and stream into a temp file so an
    # aborted transfer cannot leave a truncated file under the real name.
    from backend.services.files_service import unique_dest
    dest = await asyncio.to_thread(unique_dest, abs_path, filename)
    filename = os.path.basename(dest)
    tmp_dest = dest + ".part"

    # Stream in 1 MiB chunks — reading the whole upload into memory (await file.read())
    # risks OOM on large NAS files.
    size = 0
    try:
        if os.path.exists(tmp_dest):
            os.remove(tmp_dest)
        while chunk := await file.read(1024 * 1024):
            await asyncio.to_thread(_append_bytes, tmp_dest, chunk)
            size += len(chunk)
        await asyncio.to_thread(os.replace, tmp_dest, dest)
    except PermissionError as exc:
        with contextlib.suppress(OSError):
            os.remove(tmp_dest)
        raise _permission_denied(exc, dest) from exc
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.remove(tmp_dest)
        logger.error("Upload failed %s → %s: %s", filename, dest, exc)
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}") from exc

    logger.info("Uploaded %s → %s (%d bytes)", filename, dest, size)
    # Tell every other Files window about the new file (the chunked
    # endpoints do this too — keep both upload paths consistent).
    broadcast_mutation("upload", abs_path, [{"name": filename, "size": size}])
    return {"ok": True, "path": dest, "size": size, "filename": filename}


# ── Search ───────────────────────────────────────────────────────────────────
# The walk, the extension catalogue and the default scopes live in
# services/search_service.py (P2-03); the endpoints keep auth + path scope.


@router.get("/search")
async def search_files(
    q: str = Query(...),
    path: str = Query("/"),
    limit: int = Query(500, ge=1, le=2000),
):
    """Recursively search for items whose names contain q (case-insensitive)."""
    if path == "/":
        # The API default is the filesystem root, which only admins may use.
        # Non-admins search their own home instead of getting a bare 403.
        username, role = path_scope.current_identity()
        if role != "admin":
            path = path_scope.home_dir(username) or path
    root_path = _sanitize(path)
    if not os.path.isdir(root_path):
        raise HTTPException(status_code=400, detail="Search path is not a directory")
    term = q.strip().lower()
    if not term:
        return {"query": q, "root": root_path, "results": [], "truncated": False}
    results, truncated = await asyncio.to_thread(search_service.walk_search, root_path, term, limit)
    return {"query": q, "root": root_path, "results": results, "truncated": truncated}


# ── Universal Search (global, multi-root) ───────────────────────────────────


@router.get("/search-roots")
async def get_search_roots() -> dict:
    """Sensible search scopes for the caller (admin also gets `/`)."""
    roots = search_service.default_search_roots()
    _username, role = path_scope.current_identity()
    if role == "admin" and "/" in search_service.real_mountpoints():
        roots.append({"path": "/", "label": "Entire filesystem (/)"})
    return {"roots": roots}


@router.get("/universal-search")
async def universal_search(
    q: str = Query(..., min_length=1),
    roots: str | None = Query(None, description="CSV of root paths; default = auto scopes"),
    file_type: str = Query("all", alias="type", pattern="^(all|images|video|audio|docs|archives|code)$"),
    min_size_mb: float = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
) -> dict:
    """Search for files across multiple roots in parallel.

    ``q`` is a case-insensitive substring match on the item name. Optional
    filters: ``type`` (file-type category → extension set), ``min_size_mb``,
    and per-root result ``limit``. Invalid roots are reported in
    ``skipped_roots`` rather than failing the whole request.
    """
    term = q.strip().lower()
    if not term:
        return {"query": q, "type": file_type, "roots": [], "skipped_roots": [],
                "total": 0, "results": []}

    exts = search_service.type_extensions(file_type)
    min_size = int(min_size_mb * 1024 * 1024)

    if roots:
        raw_roots = [r.strip() for r in roots.split(",") if r.strip()]
    else:
        raw_roots = [r["path"] for r in search_service.default_search_roots()]

    valid: list[str] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for raw in raw_roots:
        try:
            p = _sanitize(raw)
        except HTTPException:
            skipped.append({"root": raw, "reason": "invalid path"})
            continue
        if p in seen:
            continue
        seen.add(p)
        if os.path.isdir(p):
            valid.append(p)
        else:
            skipped.append({"root": raw, "reason": "not a directory"})

    outcomes = await asyncio.gather(
        *[asyncio.to_thread(search_service.walk_search, r, term, limit, exts, min_size) for r in valid]
    ) if valid else []

    merged, root_stats = search_service.merge_results(zip(valid, outcomes, strict=True))

    return {
        "query": q,
        "type": file_type,
        "min_size_mb": min_size_mb,
        "roots": root_stats,
        "skipped_roots": skipped,
        "truncated_any": any(r["truncated"] for r in root_stats),
        "total": len(merged),
        "results": merged,
    }


# ── Trash (per-user recycle bin) ─────────────────────────────────────────────
# Storage, ownership and migration live in services/trash_service.py. The router
# only derives the caller from the JWT, enforces path scope and emits events.
# One account can never list/restore/purge another account's items; admins can
# ask for the cross-user view explicitly (`all=1` / `owner=<username>`).


def _trash_actor(request: Request) -> tuple[str, bool]:
    return trash_service.actor_from_claims(getattr(request.state, "current_user", None))


def _trash_target_owner(request: Request, owner: str | None) -> tuple[str, bool]:
    username, is_admin = _trash_actor(request)
    try:
        return trash_service.target_owner(username, is_admin, owner)
    except PermissionError as exc:
        logger.warning("Trash: %s tried to access another account's trash", username)
        raise HTTPException(status_code=403, detail=str(exc) or "Admin access required") from exc


def _trash_fallback_home(owner: str, base: str) -> str:
    """Where to restore when the original parent directory is gone."""
    home = f"/home/{owner}"
    if not os.path.isdir(home):
        home = str(Path.home())
    candidate = os.path.join(home, base)
    counter = 0
    while os.path.exists(candidate):
        counter += 1
        candidate = os.path.join(home, f"{base}_{counter}")
    return candidate


@router.post("/trash")
async def move_to_trash(body: FilePathRequest, request: Request) -> dict:
    """Move a file or directory into the caller's trash.

    Runs in a worker thread — cross-device moves (e.g. from /mnt/* onto the
    system disk holding the recycle bin) are full copies and can take minutes
    for large items; doing that on the event loop froze the server.
    """
    abs_path = _sanitize(body.path)
    # lexists: a dangling symlink is still an entry the user can delete.
    if not os.path.lexists(abs_path):
        raise HTTPException(status_code=404, detail="Path not found")
    # Refuse to trash the trash itself or any of its ancestors (moving a
    # directory into its own subtree fails with an opaque OS error).
    trash_abs = str(trash_service.root().resolve())
    if abs_path == trash_abs or trash_abs.startswith(abs_path + os.sep):
        raise HTTPException(status_code=400, detail="Cannot move the trash (or a parent of it) into itself")
    owner, _is_admin = _trash_actor(request)
    try:
        result = await asyncio.to_thread(trash_service.move_to_trash, abs_path, owner)
    except PermissionError as exc:
        raise _permission_denied(exc, abs_path) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as e:
        logger.error("Move to trash failed for %s: %s", abs_path, e)
        raise HTTPException(status_code=500, detail=f"Move to trash failed: {e}") from e
    # delete_soft → other windows optimistically remove the item from view.
    broadcast_mutation(
        "delete_soft", os.path.dirname(abs_path),
        [{"name": os.path.basename(abs_path)}],
    )
    return {"ok": True, "trashed_as": result["name"], "owner": result["owner"]}


@router.get("/trash")
async def list_trash(request: Request,
                     limit: int = Query(0, ge=0, le=5000),
                     offset: int = Query(0, ge=0),
                     all_users: bool = Query(False, alias="all")) -> dict:
    """The caller's trash (admins may pass `all=1` for every namespace)."""
    owner, is_admin = _trash_actor(request)
    if all_users and not is_admin:
        logger.warning("Trash: %s requested the all-users view", owner)
        raise HTTPException(status_code=403, detail="Admin access required")
    try:
        return await asyncio.to_thread(
            trash_service.list_items, owner, all_users, limit, offset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/trash/restore")
async def restore_from_trash(body: TrashRestoreRequest, request: Request,
                             owner: str | None = Query(None)) -> dict:
    """Restore an item owned by the caller (admin may pass `owner=`)."""
    target_owner, _is_admin = _trash_target_owner(request, owner)
    try:
        src, meta_file, meta = await asyncio.to_thread(
            trash_service.resolve_item, target_owner, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Trash item not found") from exc
    dest = meta.get("original_path", "")
    if not dest:
        raise HTTPException(status_code=400, detail="Original path unknown")
    # The stored path is user data written before this request — re-check the
    # caller's scope before writing anything back to disk.
    dest = _sanitize(dest)
    if os.path.exists(dest):
        raise HTTPException(status_code=409, detail="Destination already exists")

    final_dest = dest
    if not os.path.isdir(os.path.dirname(dest)):
        # Original parent directory is gone — restore into the owner's home
        # directory instead of failing with an opaque 500.
        final_dest = _trash_fallback_home(target_owner, os.path.basename(dest))
        logger.warning("Trash restore: original parent %s missing for %s — restoring to %s",
                       os.path.dirname(dest), body.name, final_dest)

    try:
        # Restoring can move gigabytes across filesystems.
        await asyncio.to_thread(shutil.move, str(src), final_dest)
    except OSError as exc:
        logger.error("Trash restore failed %s → %s: %s", src, final_dest, exc)
        raise HTTPException(status_code=500, detail=f"Restore failed: {exc}") from exc
    meta_file.unlink(missing_ok=True)
    broadcast_mutation(
        "restore", os.path.dirname(final_dest),
        [{"name": os.path.basename(final_dest)}],
    )
    # A restored photo/video (or an encrypted private item) must reappear in
    # Photos without a manual rescan — the gallery index dropped its row
    # when the item was trashed.  Fire-and-forget; the scan is incremental.
    try:
        lower = final_dest.lower()
        if lower.endswith(".ethos.enc") or os.path.splitext(lower)[1] in (IMAGE_EXTS | _VIDEO_EXTS):
            from backend.routers.gallery import _run_rescan
            asyncio.get_running_loop().create_task(_run_rescan())
    except Exception as exc:
        logger.warning("Trash restore: gallery rescan trigger failed: %s", exc)
    return {"ok": True, "restored_to": final_dest, "fallback_home": final_dest != dest,
            "owner": target_owner}


@router.delete("/trash")
async def empty_trash(request: Request,
                      all_users: bool = Query(False, alias="all")) -> dict:
    """Permanently delete the caller's trash items (admins: `all=1`)."""
    owner, is_admin = _trash_actor(request)
    if all_users and not is_admin:
        logger.warning("Trash: %s tried to empty every account's trash", owner)
        raise HTTPException(status_code=403, detail="Admin access required")
    count, failed = await asyncio.to_thread(
        trash_service.empty, owner, all_users)
    if failed:
        logger.error("Empty trash: %d item(s) could not be removed (%s)", failed,
                     "all users" if all_users else owner)
    from backend.services.notifications_service import emit_event
    emit_event("info", "Trash emptied",
               f"{owner}: {count} item(s){' (all users)' if all_users else ''}",
               source="files")
    return {"ok": True, "deleted_count": count, "failed_count": failed,
            "scope": "all" if all_users else owner}


@router.delete("/trash/item/{name}")
async def delete_trash_item(name: str, request: Request,
                            owner: str | None = Query(None)) -> dict:
    """Permanently delete one item owned by the caller (admin may pass `owner=`)."""
    target_owner, _is_admin = _trash_target_owner(request, owner)
    try:
        deleted = await asyncio.to_thread(trash_service.delete_item, target_owner, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Trash item not found") from exc
    except OSError as exc:
        logger.error("Trash permanent delete failed for %s: %s", name, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "deleted": deleted, "owner": target_owner}
