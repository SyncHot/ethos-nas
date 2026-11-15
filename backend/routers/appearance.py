"""EthOS NAS — Appearance Router (per-user desktop wallpaper)

REST API endpoints:
- GET    /api/appearance/wallpaper        — current user's wallpaper choice (404 if none saved yet)
- POST   /api/appearance/wallpaper        — save a built-in wallpaper id for the caller
- DELETE /api/appearance/wallpaper        — reset to default (removes record + custom image)
- POST   /api/appearance/wallpaper/upload — upload a custom image (jpg/png/webp, ≤ 8 MB)
- GET    /api/appearance/wallpaper/image  — serve the caller's own custom image

Storage:
- backend/data/wallpapers.json → {username: {"wallpaper": "<id>", "custom_file": "<filename>"}}
- backend/data/wallpapers/<username><ext> — per-user uploaded images

Built-in wallpaper ids are pure-CSS gradients defined in frontend/index.html
(.wallpaper-* classes) — no image assets, so nothing to license. The id list
here must stay in sync with WALLPAPER_IDS in frontend/static/core/wallpaper.js.
"""

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.config import settings

logger = logging.getLogger("ethos-nas.routers.appearance")
router = APIRouter()

WALLPAPER_FILE = settings.DATA_DIR / "wallpapers.json"

from backend.services import json_store

# Shape is {username: record} — a top-level version key would look like a user.
_STORE = json_store.get(WALLPAPER_FILE)
CUSTOM_DIR = settings.DATA_DIR / "wallpapers"
_LOCK = threading.Lock()

# Must match WALLPAPER_IDS in frontend/static/core/wallpaper.js (order = UI order).
BUILTIN_WALLPAPERS = ["aurora", "dusk", "meadow", "tide", "graphite"]
CUSTOM_ID = "custom"
VALID_IDS = set(BUILTIN_WALLPAPERS) | {CUSTOM_ID}

MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB
ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _current_username(request: Request) -> Optional[str]:
    """Authenticated username from middleware state (header/cookie/query token)."""
    user = getattr(request.state, "current_user", None) or {}
    username = str(user.get("username") or "").strip()
    return re.sub(r"[^a-z0-9._-]", "", username.lower())[:64] or None


def _load_store() -> dict:
    """Load {username: record}. A corrupt file is quarantined by json_store."""
    data = _STORE.load()
    return data if isinstance(data, dict) else {}


def _save_store(data: dict) -> None:
    """Persist atomically via services/json_store.py."""
    _STORE.save(data)


def _custom_path(username: str, filename: Optional[str]) -> Optional[Path]:
    """Resolve a stored custom-image filename to an existing file inside CUSTOM_DIR."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    p = (CUSTOM_DIR / filename).resolve()
    try:
        p.relative_to(CUSTOM_DIR.resolve())
    except ValueError:
        return None
    return p if p.is_file() else None


class WallpaperRequest(BaseModel):
    wallpaper: str


@router.get("/wallpaper")
async def get_wallpaper(request: Request) -> dict:
    """Return the caller's wallpaper choice.

    404 when this user has no saved record yet — the client then keeps its local
    state and uploads it via POST, which seeds the per-user record (same pattern
    as /api/packages/desktop/layout).
    """
    username = _current_username(request)
    if not username:
        raise HTTPException(401, "Authentication required")
    with _LOCK:
        rec = _load_store().get(username)
    if not isinstance(rec, dict):
        raise HTTPException(404, "No saved wallpaper for this user")
    out = {"username": username, "wallpaper": rec.get("wallpaper", BUILTIN_WALLPAPERS[0])}
    p = _custom_path(username, rec.get("custom_file"))
    if out["wallpaper"] == CUSTOM_ID:
        if p is None:
            # Record points at a missing file — fall back to default.
            out["wallpaper"] = BUILTIN_WALLPAPERS[0]
        else:
            out["custom_url"] = "/api/appearance/wallpaper/image"
    out["has_custom"] = p is not None
    return out


@router.post("/wallpaper")
async def save_wallpaper(req: WallpaperRequest, request: Request) -> dict:
    """Save a built-in wallpaper id (or "custom" when an image was uploaded)."""
    username = _current_username(request)
    if not username:
        raise HTTPException(401, "Authentication required")
    wid = str(req.wallpaper or "").strip().lower()
    if wid not in VALID_IDS:
        raise HTTPException(400, f"Unknown wallpaper id: {wid!r}")

    with _LOCK:
        data = _load_store()
        rec = data.get(username) or {}
        # Keep any previously uploaded file so "custom" can be re-selected later.
        if wid == CUSTOM_ID and not _custom_path(username, rec.get("custom_file")):
            raise HTTPException(400, "No custom image uploaded yet")
        rec["wallpaper"] = wid
        data[username] = rec
        _save_store(data)

    logger.info("Saved wallpaper %s for %s", wid, username)
    return {"status": "ok", "username": username, "wallpaper": wid}


@router.delete("/wallpaper")
async def reset_wallpaper(request: Request) -> dict:
    """Reset to the default wallpaper and remove the user's custom image."""
    username = _current_username(request)
    if not username:
        raise HTTPException(401, "Authentication required")
    with _LOCK:
        data = _load_store()
        rec = data.pop(username, None)
        _save_store(data)
    removed_file = False
    if isinstance(rec, dict):
        p = _custom_path(username, rec.get("custom_file"))
        if p is not None:
            try:
                p.unlink()
                removed_file = True
            except OSError as e:
                logger.warning("Could not remove custom wallpaper file for %s: %s", username, e)
    return {"status": "ok", "username": username, "removed_custom_file": removed_file}


@router.post("/wallpaper/upload")
async def upload_wallpaper(request: Request, file: UploadFile = File(...)) -> dict:
    """Upload a custom wallpaper image for the caller (replaces any previous one)."""
    username = _current_username(request)
    if not username:
        raise HTTPException(401, "Authentication required")

    ctype = (file.content_type or "").split(";")[0].strip().lower()
    ext = ALLOWED_TYPES.get(ctype)
    if ext is None:
        # Fall back to sniffing the extension when the client omitted content-type.
        name_ext = Path(file.filename or "").suffix.lower().lstrip(".")
        for t, e in ALLOWED_TYPES.items():
            if name_ext == e.lstrip("."):
                ext = e
                break
    if ext is None:
        raise HTTPException(400, "Only JPEG, PNG or WebP images are supported")

    body = await file.read()
    if not body:
        raise HTTPException(400, "Empty file")
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Image too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)")

    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{username}{ext}"
    # Atomic-ish write: tmp in same dir + replace.
    fd, tmp = tempfile.mkstemp(dir=str(CUSTOM_DIR), prefix=f".{username}-", suffix=ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        os.replace(tmp, CUSTOM_DIR / filename)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    with _LOCK:
        data = _load_store()
        rec = data.get(username) or {}
        rec["wallpaper"] = CUSTOM_ID
        rec["custom_file"] = filename
        data[username] = rec
        _save_store(data)

    logger.info("Uploaded custom wallpaper for %s (%d bytes)", username, len(body))
    return {"status": "ok", "username": username, "wallpaper": CUSTOM_ID,
            "custom_url": "/api/appearance/wallpaper/image"}


@router.get("/wallpaper/image")
async def get_wallpaper_image(request: Request) -> FileResponse:
    """Serve the caller's own custom wallpaper image (auth via header/cookie/?token=)."""
    username = _current_username(request)
    if not username:
        raise HTTPException(401, "Authentication required")
    with _LOCK:
        rec = _load_store().get(username) or {}
    p = _custom_path(username, rec.get("custom_file"))
    if p is None:
        raise HTTPException(404, "No custom wallpaper image for this user")
    media = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(p.suffix.lstrip("."), "application/octet-stream")
    return FileResponse(str(p), media_type=media)
