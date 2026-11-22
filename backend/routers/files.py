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
import json
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
from backend.services import package_client
from backend.services import path_scope
from backend.services import system_deps
from backend.services import task_registry
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
THUMB_DIR = settings.DATA_DIR / "thumbnails"
TRANSCODE_DIR = settings.DATA_DIR / "transcodes"
FAVORITES_FILE = settings.DATA_DIR / "favorites.json"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def _ensure_thumb_dir():
    THUMB_DIR.mkdir(parents=True, exist_ok=True)


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


# ── Thumbnail cache helpers (sidecar meta: mtime + size) ─────────────────────

def _thumb_meta_file(cache_file: Path) -> Path:
    """Sidecar file storing source mtime+size the thumbnail was built from."""
    return cache_file.with_name(cache_file.name + ".meta.json")


def _cache_is_fresh(cache_file: Path, abs_path: str) -> bool:
    """True if a cached artifact matches the current source (mtime AND size)."""
    meta = _thumb_meta_file(cache_file)
    try:
        st = os.stat(abs_path)
        data = json.loads(meta.read_text())
        return (abs(st.st_mtime - float(data.get("src_mtime", 0))) < 1.0
                and int(data.get("src_size", -1)) == st.st_size)
    except Exception:
        # No meta / unreadable → treat as stale; regenerate once to rebuild it.
        return False


def _write_thumb_meta(cache_file: Path, abs_path: str, extra: dict | None = None):
    """Record the source mtime+size a cached artifact was built from.

    ``extra`` merges derived artifacts' own lineage (e.g. the mtime of the
    master frame a per-size slot was rendered from).
    """
    try:
        st = os.stat(abs_path)
        data = {"src_mtime": st.st_mtime, "src_size": st.st_size}
        if extra:
            data.update(extra)
        _thumb_meta_file(cache_file).write_text(json.dumps(data))
    except OSError:
        pass  # probe failure means 'not present'


def _cleanup_stale_thumb_artifacts():
    """Remove orphaned thumbnail artifacts (no sidecar meta) — one-time sweep."""
    try:
        for f in THUMB_DIR.iterdir():
            if not f.is_file():
                continue
            if "_vid_v2" in f.name:
                # Previous video pipeline (one ffmpeg run per size) — superseded
                # by the master-frame cache; drop it so it stops eating the cap.
                with contextlib.suppress(OSError):
                    f.unlink()
                continue
            if f.suffix != ".webp":
                continue
            if _thumb_meta_file(f).exists():
                continue
            # Orphaned (legacy cache without meta) — safe to drop.
            with contextlib.suppress(OSError):
                f.unlink()
    except OSError:
        pass  # best-effort cleanup — nothing to report


# ── Video thumbnail generation (shared by GET + warm endpoints) ─────────────
#
# One ffmpeg run per file: a master frame at VIDEO_THUMB_WIDTH is extracted
# once and every requested size is a cheap PIL downscale of that master — so
# compact (320) and cinematic (640) grids share the same decode and toggling
# the preview mode never re-runs ffmpeg for the whole folder.
#
# Extraction always happens in a bounded background worker: a cache miss
# enqueues a job and the request answers 202 + Retry-After immediately. Files
# that keep failing back off exponentially instead of being retried on every
# grid refresh (an 8K VR clip used to queue four 30 s ffmpeg attempts per
# scroll).

_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".wmv", ".ts", ".mts"}
VIDEO_THUMB_WIDTH = 640              # master frame width; larger requests clamp to it
_VIDEO_THUMB_VERSION = "vid_v3"      # bump to invalidate every cached video frame
_VIDEO_THUMB_FAIL_MAX_ATTEMPTS = 4
_VIDEO_THUMB_BACKOFF = (60, 300, 900, 3600)  # seconds, indexed by attempt-1
_VIDEO_THUMB_PERMANENT_BACKOFF = 86400      # a hopeless file costs 1 try per day
_VIDEO_THUMB_QUEUE_MAX = 48          # pending jobs; beyond that clients poll again
_VIDEO_THUMB_TOTAL_BUDGET = 75       # wall-clock cap for one file, seconds
_VIDEO_THUMB_MAX_BYTES = 50 * 1024 ** 3

# Max concurrent ffmpeg processes (CPU/RAM guard). Jobs beyond this wait in
# the asyncio queue; each ffmpeg itself is capped to 2 decode threads.
_video_thumb_semaphore = asyncio.Semaphore(max(1, settings.VIDEO_THUMB_CONCURRENCY))
_VIDEO_THUMB_INFLIGHT: dict[str, asyncio.Task] = {}


@lru_cache(maxsize=1)
def _ffmpeg_thumb_prefix() -> tuple[str, ...]:
    """Lowest-priority ffmpeg wrapper for background frame grabs.

    ``nice -n 19`` keeps the CPU for the UI/API; ``ionice -c 3`` keeps a
    thumbnail burst from starving the disks (media streams stay smooth).
    """
    prefix: list[str] = ["nice", "-n", "19"]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c", "3"]
    return tuple(prefix + ["ffmpeg"])


def _video_cache_base(abs_path: str) -> str:
    return hashlib.sha256(abs_path.encode()).hexdigest()[:16]


def _video_master_file(abs_path: str) -> Path:
    """Full-width frame extracted from the video (the only ffmpeg artifact)."""
    return THUMB_DIR / f"{_video_cache_base(abs_path)}_{_VIDEO_THUMB_VERSION}.webp"


def _video_slot_file(abs_path: str, size: int) -> Path:
    """Per-size WebP rendered from the master frame."""
    return THUMB_DIR / f"{_video_cache_base(abs_path)}_{_VIDEO_THUMB_VERSION}_{size}.webp"


def _video_fail_file(abs_path: str) -> Path:
    return THUMB_DIR / f"{_video_cache_base(abs_path)}_{_VIDEO_THUMB_VERSION}.fail.json"


def _video_master_is_fresh(abs_path: str) -> bool:
    master = _video_master_file(abs_path)
    return master.exists() and _cache_is_fresh(master, abs_path)


def _video_slot_is_fresh(slot: Path, master: Path, abs_path: str) -> bool:
    """Slot matches the source file AND the master frame it was rendered from."""
    if not (slot.exists() and _cache_is_fresh(slot, abs_path)):
        return False
    try:
        data = json.loads(_thumb_meta_file(slot).read_text())
        return float(data.get("master_mtime", -1.0)) >= master.stat().st_mtime - 1.0
    except (OSError, ValueError, TypeError):
        return False


def _read_video_thumb_failure(abs_path: str) -> dict:
    """Failure marker for the current source; {} when absent or stale.

    The marker stores the source mtime+size, so replacing/re-encoding a file
    that used to fail starts a fresh set of attempts.
    """
    try:
        data = json.loads(_video_fail_file(abs_path).read_text())
        if not isinstance(data, dict):
            return {}
        st = os.stat(abs_path)
        if (abs(st.st_mtime - float(data.get("src_mtime", 0))) >= 1.0
                or int(data.get("src_size", -1)) != st.st_size):
            return {}
        return data
    except (OSError, ValueError, TypeError):
        return {}


def _record_video_thumb_failure(abs_path: str, reason: str, permanent: bool = False) -> dict:
    """Bump the attempt counter and schedule the next try (exponential)."""
    try:
        st = os.stat(abs_path)
    except OSError:
        return {}
    attempts = int(_read_video_thumb_failure(abs_path).get("attempts", 0)) + 1
    permanent = permanent or attempts >= _VIDEO_THUMB_FAIL_MAX_ATTEMPTS
    delay = (_VIDEO_THUMB_PERMANENT_BACKOFF if permanent
             else _VIDEO_THUMB_BACKOFF[min(attempts, len(_VIDEO_THUMB_BACKOFF)) - 1])
    data = {
        "attempts": attempts,
        "next_try": time.time() + delay,
        "permanent": permanent,
        "reason": reason[:200],
        "src_mtime": st.st_mtime,
        "src_size": st.st_size,
    }
    try:
        _video_fail_file(abs_path).write_text(json.dumps(data))
    except OSError:
        pass  # bookkeeping only — a lost marker costs one extra attempt
    return data


def _clear_video_thumb_failure(abs_path: str):
    with contextlib.suppress(OSError):
        _video_fail_file(abs_path).unlink()


def _video_thumb_backoff(abs_path: str) -> dict | None:
    """Active backoff marker, or None when a new attempt is allowed now."""
    data = _read_video_thumb_failure(abs_path)
    if not data:
        return None
    try:
        next_try = float(data.get("next_try", 0))
    except (TypeError, ValueError):
        return None
    return data if time.time() < next_try else None


def _render_video_slot(master: Path, abs_path: str, size: int, slot: Path) -> bool:
    """Downscale the master frame into a per-size WebP cache slot (PIL)."""
    tmp_file = slot.with_suffix(".tmp.webp")
    try:
        from PIL import Image as PILImage
        with PILImage.open(master) as img:
            w, h = img.size
            if w > size:
                img = img.resize((size, max(2, int(h * size / w))), PILImage.Resampling.LANCZOS)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(str(tmp_file), "WEBP", quality=82)
        tmp_file.replace(slot)
        _write_thumb_meta(slot, abs_path, {"master_mtime": master.stat().st_mtime})
        return True
    except Exception as exc:
        logger.warning("Video thumb slot render failed for %s: %s", abs_path, exc)
        with contextlib.suppress(OSError):
            tmp_file.unlink()
        return False


async def _generate_video_master(abs_path: str) -> bool:
    """Background worker: one niced ffmpeg frame grab into the master slot."""
    if _cover_is_fresh(abs_path):
        # A user cover supersedes the automatic frame — not a failure.
        return False
    try:
        if os.path.getsize(abs_path) > _VIDEO_THUMB_MAX_BYTES:
            logger.warning("Video too large for thumbnail: %s", abs_path)
            _record_video_thumb_failure(abs_path, "too_large", permanent=True)
            return False
    except OSError:
        return False
    master = _video_master_file(abs_path)
    ok = False
    async with _video_thumb_semaphore:
        try:
            ok = await asyncio.to_thread(
                _extract_video_frame, abs_path, VIDEO_THUMB_WIDTH, master)
        except Exception as exc:
            logger.warning("Video thumbnail generation failed for %s: %s",
                           abs_path, exc, exc_info=True)
    if ok and master.exists():
        _clear_video_thumb_failure(abs_path)
        return True
    _record_video_thumb_failure(abs_path, "no_frame")
    return False


def _queue_video_thumb(abs_path: str) -> bool:
    """Deduplicated enqueue of a master-frame job. True when a job is pending."""
    task = _VIDEO_THUMB_INFLIGHT.get(abs_path)
    if task is not None and not task.done():
        return True
    if len(_VIDEO_THUMB_INFLIGHT) >= _VIDEO_THUMB_QUEUE_MAX:
        return False
    task = task_registry.spawn(_generate_video_master(abs_path), name="video-thumb")
    if task is None:
        return False
    _VIDEO_THUMB_INFLIGHT[abs_path] = task
    task.add_done_callback(lambda _t, k=abs_path: _VIDEO_THUMB_INFLIGHT.pop(k, None))
    return True

# ── Custom video covers (user-captured frame shown as the file's thumbnail) ──

def _cover_master_file(abs_path: str) -> Path:
    """Full-resolution JPEG master of a user-set cover, keyed by path only."""
    h = hashlib.sha256(f"cover:{abs_path}".encode()).hexdigest()[:16]
    return THUMB_DIR / f"{h}_cover.jpg"

def _cover_is_fresh(abs_path: str) -> bool:
    """True when a custom cover exists and matches the current source file."""
    master = _cover_master_file(abs_path)
    try:
        return master.stat().st_size > 0 and _cache_is_fresh(master, abs_path)
    except OSError:
        return False

def _cover_slot_is_fresh(cover_file: Path, abs_path: str) -> bool:
    """True when a rendered cover slot matches both the source file and the
    current cover master (re-capturing the cover invalidates old renders)."""
    if not (cover_file.exists() and _cache_is_fresh(cover_file, abs_path)):
        return False
    try:
        data = json.loads(_thumb_meta_file(cover_file).read_text())
        master_mtime = float(data.get("master_mtime", -1.0))
        return master_mtime >= _cover_master_file(abs_path).stat().st_mtime - 1.0
    except (OSError, ValueError):
        return False

def _render_cover_to_cache(abs_path: str, size: int, cache_file: Path) -> bool:
    """Scale the custom cover master into a per-size WebP cache slot.

    The user picked this exact frame — no face-crop or re-selection is applied.
    Returns True on success (cache file + meta written).
    """
    master = _cover_master_file(abs_path)
    tmp_file = cache_file.with_suffix(".tmp.webp")
    try:
        from PIL import Image as PILImage
        with PILImage.open(master) as img:
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            w, h = img.size
            if w != size and w >= 32:
                img = img.resize((size, max(2, int(h * size / w))), PILImage.Resampling.LANCZOS)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(str(tmp_file), "WEBP", quality=85)
        tmp_file.replace(cache_file)
        _write_thumb_meta(cache_file, abs_path, {"master_mtime": master.stat().st_mtime})
        return True
    except Exception as exc:
        logger.warning("Cover render failed for %s: %s", abs_path, exc)
        tmp_file.unlink(missing_ok=True)
        return False

def _grab_cover_frame_sync(abs_path: str, t: float, out_file: Path, left_half: bool = False) -> None:
    """Grab one full-resolution frame at time t with ffmpeg (input-side seek).

    With ``left_half`` the frame is cropped to its left half — for side-by-side
    (VR goggles) videos this yields a single-eye image instead of the doubled
    stereo pair.
    """
    cmd = list(_ffmpeg_thumb_prefix()) + [
        "-nostdin", "-hide_banner", "-loglevel", settings.FFMPEG_LOGLEVEL,
        "-threads", "2", "-ss", f"{t:.3f}", "-i", abs_path]
    if left_half:
        cmd += ["-vf", "crop=iw/2:ih:0:0"]
    cmd += ["-vframes", "1", "-q:v", "2", "-f", "image2", "-y", str(out_file)]
    subprocess.run(cmd, capture_output=True, timeout=60)


def _probe_duration(abs_path: str) -> float:
    """Probe video duration with ffprobe. Returns 0 on failure."""
    try:
        probe = subprocess.run(
            ["nice", "-n", "19", "ffprobe", "-v", "error",
             "-probesize", "1000000", "-analyzeduration", "1000000",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", abs_path],
            capture_output=True, text=True, timeout=6,
        )
        return float(probe.stdout.strip()) if probe.stdout.strip() else 0.0
    except Exception:
        return 0.0


def _append_bytes(path: str, data: bytes) -> None:
    """Append one upload chunk (runs in a worker thread)."""
    with open(path, "ab") as fh:
        fh.write(data)


# ── Face detection (movie thumbnails prefer a face over a random frame) ─────
# Detection is an optional capability: it runs inside the `ethos-media-enhance`
# package (OpenCV lives in that package's venv). Without it, thumbnails fall
# back to the first valid frame — a plain centre-visible shot.

MEDIA_ENHANCE = "ethos-media-enhance"


def _detect_face_in_frame(frame_path: str):
    """Return (x, y, w, h) of the best face in a frame image, or None.

    The package downscales internally and returns original-pixel coordinates,
    so the caller's PIL crop sees the same box it used to get from cv2.
    """
    try:
        data = Path(frame_path).read_bytes()
    except OSError as exc:
        logger.debug("Could not read frame %s for face detection: %s", frame_path, exc)
        return None
    try:
        payload = package_client.post_bytes(
            MEDIA_ENHANCE, "/face", data, "application/octet-stream", timeout=10,
        )
    except package_client.PackageUnavailable:
        logger.debug("Face detection skipped: package %s is not installed", MEDIA_ENHANCE)
        return None
    except package_client.PackageCallError as exc:
        logger.warning("Face detection failed for %s: %s", frame_path, exc)
        return None
    box = payload.get("face") if isinstance(payload, dict) else None
    if not isinstance(box, list) or len(box) != 4:
        return None
    try:
        x, y, w, h = (int(value) for value in box)
    except (TypeError, ValueError):
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


def _crop_face_from_frame(frame_path: str, face_box):
    """Crop a face-centered region (face + ~60% headroom/shoulders).

    Returns True if the file was replaced with the crop.
    """
    try:
        from PIL import Image as PILImage
        x, y, w, h = face_box
        with PILImage.open(frame_path) as img:
            W, H = img.size
            cx, cy = x + w / 2, y + h / 2
            side = max(w * 1.9, h * 2.4)
            left = int(cx - side / 2)
            top = int(cy - side * 0.58)  # shift up: more headroom than chin
            box = (max(0, min(left, W - 8)), max(0, min(top, H - 8)),
                   min(W, left + int(side)), min(H, top + int(side)))
            if box[2] - box[0] < 32 or box[3] - box[1] < 32:
                return False
            crop = img.crop(box)
            tmp_file = Path(frame_path).with_suffix(".crop.webp")
            if crop.mode != "RGB":
                crop = crop.convert("RGB")
            crop.save(str(tmp_file), "WEBP", quality=85)
            tmp_file.replace(frame_path)
        return True
    except Exception as exc:
        logger.debug("Face crop failed for %s: %s", frame_path, exc)
        return False


def _extract_video_frame(abs_path: str, size: int, cache_file: Path, face_crop: bool = True) -> bool:
    """Extract one frame from a video into ``cache_file`` (WebP).

    Uses INPUT-side seek first (``-ss`` before ``-i``): ffmpeg seeks in the
    container and decodes only to the target — ~2 s on large HEVC files,
    versus minutes for output-side decode.  Falls back to output-side seek
    if input-side fails or yields nothing (rare container quirks).

    When ``face_crop`` is set, candidate frames are scanned with a Haar face
    detector; the first frame containing a face wins and is cropped around
    it — movie thumbnails end up showing a person instead of an empty shot.
    Returns True on success.
    """
    try:
        fsize = os.path.getsize(abs_path)
        if fsize > 50 * 1024 ** 3:
            logger.warning("Video too large for thumbnail: %s (%.1f GB)", abs_path, fsize / (1024**3))
            return False
    except OSError:
        return False

    # A user-set cover takes precedence over the automatic frame grab.
    # Files with a fresh custom cover are served from the dedicated cover slot
    # by the endpoints — skip auto-generation so it can never clobber the
    # user-picked frame.
    if _cover_is_fresh(abs_path):
        return False

    # Face detection needs the media-enhance package; without it every extra
    # candidate would just re-decode the same video to find nothing, so the
    # first valid frame wins and the scan stops immediately.
    if face_crop and not package_client.is_available(MEDIA_ENHANCE):
        face_crop = False

    duration = _probe_duration(abs_path)
    # Candidate seek points: ~10% of duration first (clamped 5–60 s), then a
    # few spread-out samples so face detection has more chances to hit.
    # Every point is clamped inside the file — short clips (<5 s) would
    # otherwise seek past EOF and yield no frame at all.
    if duration > 0:
        # Margin must exceed one frame period, otherwise low-fps sources
        # (e.g. 10 fps) have no frame left after the seek point → 0-byte output.
        margin = min(0.5, duration / 2)

        def _clamp(t: float) -> float:
            return min(t, max(0.0, duration - margin))
        candidates = [_clamp(min(max(duration * 0.1, 5), 60))]
        for frac in (0.3, 0.5, 0.7):
            t = _clamp(min(max(duration * frac, 2), max(60, duration - 2)))
            if all(abs(t - c) > 1 for c in candidates):
                candidates.append(t)
    else:
        candidates = [10]

    tmp_file = cache_file.with_suffix(".tmp.webp")
    # Three strategies, cheapest first:
    #  1. input-side ``-ss`` — the demuxer jumps to the previous keyframe and
    #     decodes only up to the target; the normal case lands in well under a
    #     second (the old code had four 30 s attempts for this alone);
    #  2. keyframe-only scan from the start (``-skip_frame nokey`` + output
    #     ``-ss``) — skips every P/B frame, which rescues long-GOP sources where
    #     (1) would decode minutes of 8K video;
    #  3. a full decode ~1 s in — last resort for containers where the
    #     keyframe-only scan yields nothing at all.
    base_input = ["-nostdin", "-hide_banner", "-loglevel", settings.FFMPEG_LOGLEVEL,
                  "-threads", "2"]
    rescue_t = 0.0 if duration <= 0 else min(1.0, duration / 2)
    attempts: list[tuple[list[str], int]] = []
    for t in candidates:
        attempts.append((base_input + ["-ss", str(t), "-i", abs_path], 20))
    attempts.append((base_input + ["-skip_frame", "nokey", "-i", abs_path,
                                   "-ss", str(candidates[0])], 20))
    attempts.append((base_input + ["-i", abs_path, "-ss", str(rescue_t)], 30))

    best_frame = None      # (frame_path, face_box) — a frame with a detected face
    fallback_file = None   # first valid frame without a face (kept as backup)
    fallback_path = cache_file.with_suffix(".fb.webp")
    deadline = time.monotonic() + _VIDEO_THUMB_TOTAL_BUDGET
    for prefix_args, timeout in attempts:
        remaining = deadline - time.monotonic()
        if remaining < 5:
            logger.warning("ffmpeg thumbnail budget exhausted for %s", abs_path)
            break
        try:
            if tmp_file.exists():
                tmp_file.unlink()
            proc = subprocess.run(
                list(_ffmpeg_thumb_prefix()) + prefix_args + [
                    "-map", "0:v:0", "-an", "-sn", "-dn",
                    "-vframes", "1",
                    "-vf", f"scale={size}:-2",
                    # explicit static encoder: ffmpeg 8's webp muxer defaults to
                    # libwebp_anim, which fails on single-frame output.
                    "-c:v", "libwebp",
                    "-f", "webp",
                    "-y", str(tmp_file),
                ],
                capture_output=True, timeout=max(5, int(min(timeout, remaining))),
            )
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg thumbnail timeout for %s (attempt)", abs_path)
            continue
        except Exception as exc:
            logger.warning("ffmpeg thumbnail failed for %s: %s", abs_path, exc)
            break
        if not (proc.returncode == 0 and tmp_file.exists() and tmp_file.stat().st_size > 0):
            continue
        if not face_crop:
            # First valid frame is the answer — no detector to please, and the
            # remaining candidates would only decode the source again.
            tmp_file.replace(cache_file)
            _write_thumb_meta(cache_file, abs_path)
            return True
        # Got a valid frame — try face detection before accepting it.
        box = _detect_face_in_frame(str(tmp_file))
        if box is not None:
            best_frame = (tmp_file, box)
            break
        # No face in this frame — keep the first one as fallback and move it
        # aside so the next attempt's unlink doesn't destroy it.
        if fallback_file is None:
            tmp_file.replace(fallback_path)
            fallback_file = fallback_path

    try:
        if best_frame is not None:
            frame_path, face_box = best_frame
            _crop_face_from_frame(str(frame_path), face_box)
            frame_path.rename(cache_file)
            _write_thumb_meta(cache_file, abs_path)
            return True

        # No face found in any candidate — fall back to the first valid frame.
        if fallback_file is not None and fallback_file.exists() and fallback_file.stat().st_size > 0:
            fallback_file.rename(cache_file)
            _write_thumb_meta(cache_file, abs_path)
            return True
    finally:
        # Clean up leftovers (tmp + unused fallback).
        for leftover in (tmp_file, fallback_file):
            try:
                if leftover is not None and Path(leftover).exists():
                    Path(leftover).unlink()
            except OSError:
                pass  # best-effort cleanup — nothing to report
    return False


def render_video_thumb(abs_path: str, size: int, out_file: Path) -> bool:
    """Render the effective video thumbnail into ``out_file`` (WebP).

    Uses the user's custom cover when one is set and still matches the source;
    otherwise extracts an automatic frame. Returns True on success.
    """
    if _cover_is_fresh(abs_path):
        return _render_cover_to_cache(abs_path, size, out_file)
    try:
        return _extract_video_frame(abs_path, size, out_file)
    except Exception as exc:
        logger.warning("Video thumbnail render failed for %s: %s", abs_path, exc)
        return False


def _video_fallback_svg(size: int, cache: str | None = None) -> Response:
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{int(size*0.75)}" viewBox="0 0 120 90">
        <defs>
            <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stop-color="#334155"/>
                <stop offset="1" stop-color="#1e293b"/>
            </linearGradient>
        </defs>
        <rect width="120" height="90" rx="8" fill="url(#g)"/>
        <circle cx="60" cy="45" r="17" fill="rgba(255,255,255,0.14)"/>
        <polygon points="55,37 55,53 69,45" fill="rgba(255,255,255,0.85)"/>
    </svg>'''
    headers = {"Cache-Control": cache} if cache else None
    return Response(content=svg, media_type="image/svg+xml", headers=headers)


# ── Transcode (H.264 remux for browser playback) ─────────────────────────────

_TRANSCODE_SEM = asyncio.Semaphore(1)  # one transcode at a time on this CPU
_transcode_jobs: dict[str, dict] = {}   # abs_path → {status, progress, error}


def _transcode_key(abs_path: str, height: int) -> str:
    return hashlib.sha256(f"tc:{abs_path}:{height}".encode()).hexdigest()[:16]


def _transcoded_file(abs_path: str, height: int = 720) -> Path:
    stem = os.path.splitext(os.path.basename(abs_path))[0][:80]
    return TRANSCODE_DIR / f"{_transcode_key(abs_path, height)}_{stem}.mp4"


def _transcode_is_fresh(out_file: Path, abs_path: str) -> bool:
    """True if the transcoded file matches the current source (mtime+size)."""
    return out_file.exists() and out_file.stat().st_size > 0 \
        and _cache_is_fresh(out_file, abs_path)


def _probe_codec(abs_path: str) -> str | None:
    """Return the first video codec name, or None."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", abs_path],
            capture_output=True, text=True, timeout=8,
        )
        line = (probe.stdout or "").strip().splitlines()
        return line[0].strip() if line else None
    except Exception:
        return None


def _probe_video_size(abs_path: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream; (0, 0) on failure."""
    with contextlib.suppress(Exception):
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "default=noprint_wrappers=1:nokey=1", abs_path],
            capture_output=True, text=True, timeout=8,
        )
        for line in (probe.stdout or "").strip().splitlines():
            if "," in line:
                w_s, h_s = line.split(",")
                return int(w_s), int(h_s)
    return 0, 0


def _transcode_worker(abs_path: str, height: int):
    """Run ffmpeg transcode in a worker thread. Updates job dict."""
    job = _transcode_jobs[abs_path]
    out_file = _transcoded_file(abs_path, height)
    tmp_file = out_file.with_suffix(".part.mp4")
    TRANSCODE_DIR.mkdir(parents=True, exist_ok=True)

    # Duration for progress reporting (best-effort).
    duration = _probe_duration(abs_path) or 0.0

    # Compute output dimensions in Python (no ffmpeg expression escaping):
    # cap at `height` px tall and 1920 px wide, never upscale, keep even dims.
    iw, ih = _probe_video_size(abs_path)
    vf: str | None = None
    if iw > 0 and ih > 0:
        tw, th = iw, ih
        if ih > height:
            th = height
            tw = max(2, (iw * height // ih) & ~1)
        elif iw > 1920:
            tw = 1920
            th = max(2, (ih * 1920 // iw) & ~1)
        if (tw, th) != (iw, ih):
            vf = f"scale={tw}:{th}"

    cmd = [
        "nice", "-n", "19",
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info",
        "-i", abs_path,
        "-map", "0:v:0", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "23", "-maxrate", "8M", "-bufsize", "16M",
    ]
    if vf:
        cmd += ["-vf", vf]
    cmd += [
        "-map", "0:a?", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-y", str(tmp_file),
    ]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if "time=" in line and duration > 0:
                # Best effort — malformed ffmpeg output lines must not kill the job.
                with contextlib.suppress(Exception):
                    t = line.split("time=", 1)[1].split()[0]
                    h, m, s = (x.split(":") for x in [t])
                    cur = int(h[0]) * 3600 + int(m) * 60 + float(s)
                    job["progress"] = min(99.0, round(cur / duration * 100, 1))
        proc.wait()
        if proc.returncode == 0 and tmp_file.exists() and tmp_file.stat().st_size > 0:
            tmp_file.rename(out_file)
            _write_thumb_meta(out_file, abs_path)
            job["status"] = "done"
            job["progress"] = 100.0
            logger.info("Transcode complete: %s → %s", abs_path, out_file.name)
        else:
            job["status"] = "error"
            job["error"] = f"ffmpeg exited {proc.returncode}"
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
    finally:
        if tmp_file.exists():
            with contextlib.suppress(OSError):
                tmp_file.unlink()


@router.get("/transcode-status")
async def transcode_status(path: str = Query(...), height: int = Query(720, ge=360, le=1080)) -> dict:
    """Report status of the H.264 transcode for a video file."""
    abs_path = _sanitize_read(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")

    out_file = _transcoded_file(abs_path, height)
    job = _transcode_jobs.get(abs_path)

    # Completed on disk (survives restarts) and still matches the current source?
    if _transcode_is_fresh(out_file, abs_path):
        return {"status": "done", "progress": 100.0, "path": abs_path}

    if job is None or job.get("status") == "done":
        # No active job. A completed job with a stale output means the source
        # changed after transcoding — report idle so clients re-request.
        return {"status": "idle", "progress": 0.0, "path": abs_path}

    return {
        "status": job.get("status", "running"),
        "progress": job.get("progress", 0.0),
        "error": job.get("error"),
        "path": abs_path,
    }


@router.get("/transcode-stream")
async def transcode_stream(path: str = Query(...), height: int = Query(720, ge=360, le=1080)):
    """Serve the H.264 transcoded file (Range-capable) for browser playback.

    The player should poll ``/transcode-status`` until ``done`` before
    requesting this — a partially written file is not Range-stable, so while
    the job runs we return 202 + Retry-After instead of streaming garbage.
    """
    abs_path = _sanitize_read(path)
    out_file = _transcoded_file(abs_path, height)
    if not _transcode_is_fresh(out_file, abs_path):
        job = _transcode_jobs.get(abs_path)
        if job is None or job.get("status") not in ("running", "queued"):
            # Stale or missing output with no active job — nothing to stream.
            raise HTTPException(status_code=404, detail="No transcode available")
        return Response(
            status_code=202,
            headers={"Retry-After": "5", "X-Transcode-Status": job.get("status", "running")},
        )
    return FileResponse(str(out_file), media_type="video/mp4")


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

    out_file = _transcoded_file(abs_path, height)
    if _transcode_is_fresh(out_file, abs_path):
        return {"status": "done", "progress": 100.0, "path": abs_path}

    job = _transcode_jobs.get(abs_path)
    # A running/queued job for this file already exists → just report it.
    if job is not None and job.get("status") in ("running", "queued"):
        return {"status": job["status"],
                "progress": job.get("progress", 0.0), "path": abs_path}

    # (Re)start the worker; semaphore serializes actual ffmpeg runs, so a
    # second file's job waits as "queued" until the first finishes.
    _transcode_jobs[abs_path] = {"status": "running", "progress": 0.0}

    async def _run():
        try:
            async with _TRANSCODE_SEM:
                await asyncio.to_thread(_transcode_worker, abs_path, height)
        except Exception as exc:
            j = _transcode_jobs.get(abs_path)
            if j is not None and j.get("status") != "done":
                j["status"] = "error"
                j["error"] = str(exc)

    task_registry.spawn(_run(), name="file-transcode")
    return {"status": "running", "progress": 0.0, "path": abs_path}


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


@router.get("/favorites")
async def list_favorites(request: Request) -> dict:
    """List starred folders for the current user."""
    user = _current_username(request)
    paths = _load_favorites().get(user, [])
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


def _render_image_thumb(abs_path: str, size: int, cache_file: Path) -> bool:
    """Blocking PIL render — always called from a worker thread.

    Decoding a 20 MP JPEG and running LANCZOS takes 100-300 ms; doing that on
    the event loop froze every other request (the gallery grid asks for dozens
    of thumbs at once). Writes through a temp file so a half-written thumb is
    never served.
    """
    _ensure_thumb_dir()
    from PIL import Image as PILImage
    with PILImage.open(abs_path) as img:
        # JPEG draft mode decodes at ~2x the target size instead of full
        # resolution — 3-10x faster on 12-48 MP photos with no visible
        # difference at thumbnail size. Must run before any load/resize.
        try:
            img.draft("RGB", (size * 2, size * 2))
        except Exception:
            pass  # intentional: a failure here must not break the caller
        if img.mode not in ("RGB", "RGBA", "P", "L"):
            img = img.convert("RGBA")
        img.thumbnail((size, size), PILImage.Resampling.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        tmp_file = cache_file.with_suffix(".tmp.webp")
        img.save(str(tmp_file), "WEBP", quality=85)
        tmp_file.replace(cache_file)
    _write_thumb_meta(cache_file, abs_path)
    return True


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
):
    """Serve a cached video frame; enqueue extraction on a miss.

    One master frame per file is extracted by a bounded, niced background
    worker and every size is a cheap PIL downscale of it — the compact (320)
    and cinematic (640) grids share a single ffmpeg run. A miss answers
    **202 Accepted** with ``Retry-After`` so the client re-requests instead of
    holding the connection; repeated failures back off exponentially per file.

    Args:
        path: Absolute filesystem path to the video file.
        size: Thumbnail width in pixels (64-640, default 320).
    """
    abs_path = _sanitize_read(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")

    ext = Path(abs_path).suffix.lower()
    if ext not in _VIDEO_EXTS:
        raise HTTPException(status_code=400, detail="Not a supported video format")

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

    master = _video_master_file(abs_path)
    if not (master.exists() and _cache_is_fresh(master, abs_path)):
        backoff = _video_thumb_backoff(abs_path)
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
        if not _queue_video_thumb(abs_path):
            # Worker pool saturated — ask the client to poll again shortly.
            return Response(status_code=202, headers={"Retry-After": "5"})
        return Response(status_code=202, headers={"Retry-After": "2"})

    if width >= VIDEO_THUMB_WIDTH:
        return _serve(master)

    slot = _video_slot_file(abs_path, width)
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

#: Directories never worth crawling on a NAS search (VCS metadata, dependency
#: trees, caches, virtualenvs). They dominate the walk time and almost never
#: hold the file the user is looking for.
_SEARCH_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".cache",
    ".npm", ".yarn", ".gradle", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", "site-packages", ".tox", ".next", ".nuxt", "dist",
}


def _walk_search(root_path: str, term: str, limit: int,
                 exts: set[str] | None = None, min_size: int = 0) -> tuple[list[dict], bool]:
    """Recursively collect items whose names contain term (case-insensitive).

    With ``exts`` set, only regular files with a matching extension are
    returned; with ``min_size`` > 0, smaller files are skipped. When either
    filter is active, directories are excluded from the results.
    Well-known heavy directories (``_SEARCH_SKIP_DIRS``) are not descended into.
    """
    results = []
    truncated = False
    filtered = exts is not None or min_size > 0
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in _SEARCH_SKIP_DIRS]
        names = sorted(filenames) if filtered else sorted(filenames + dirnames)
        for name in names:
            if term not in name.lower():
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            is_dir = os.path.isdir(full)
            if filtered and is_dir:
                continue
            size = 0 if is_dir else st.st_size
            if exts is not None:
                e = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if e not in exts:
                    continue
            if min_size > 0 and size < min_size:
                continue
            results.append({
                "name": name,
                "path": full,
                "is_dir": is_dir,
                "size": size,
                "modified": st.st_mtime,
            })
            if len(results) >= limit:
                truncated = True
                break
        if truncated:
            break
    return results, truncated


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
    results, truncated = await asyncio.to_thread(_walk_search, root_path, term, limit)
    return {"query": q, "root": root_path, "results": results, "truncated": truncated}


# ── Universal Search (global, multi-root) ───────────────────────────────────

#: File-type filters for universal search → allowed extensions.
SEARCH_TYPE_EXTS: dict[str, set[str]] = {
    "images": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "heic", "avif", "tiff", "tif"},
    "video": {"mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "3gp", "ts", "m2ts"},
    "audio": {"mp3", "ogg", "oga", "flac", "wav", "aac", "m4a", "opus", "wma", "mid", "midi"},
    "docs": {"pdf", "doc", "docx", "odt", "txt", "md", "rtf", "xls", "xlsx", "ods", "csv",
             "ppt", "pptx", "odp", "epub", "html", "htm"},
    "archives": {"zip", "tar", "gz", "tgz", "bz2", "tbz", "xz", "txz", "7z", "rar", "zst", "lz4"},
    "code": {"py", "js", "ts", "jsx", "tsx", "c", "cpp", "cc", "h", "hpp", "java", "go",
             "rs", "rb", "php", "sh", "bash", "sql", "json", "yaml", "yml", "toml",
             "xml", "css", "html", "htm", "vue", "svelte"},
}

#: Filesystems that never hold user data — excluded from auto search scopes.
_PSEUDO_FSTYPES = {
    "tmpfs", "squashfs", "overlay", "proc", "procfs", "sysfs", "devtmpfs",
    "devpts", "cgroup", "cgroup2", "securityfs", "debugfs", "tracefs", "pstore",
    "bpf", "configfs", "fusectl", "mqueue", "hugetlbfs", "ramfs", "autofs",
    "binfmt_misc", "nsfs", "efivarfs", "iso9660",
}


def _real_mountpoints() -> list[str]:
    """Readable real (non-pseudo) mountpoints, sorted and deduped."""
    try:
        import psutil
        parts = psutil.disk_partitions(all=False)
    except Exception:  # pragma: no cover — psutil missing or platform quirk
        return []
    out: list[str] = []
    for p in parts:
        if p.fstype in _PSEUDO_FSTYPES:
            continue
        mp = p.mountpoint
        try:
            if not os.path.isdir(mp) or not os.access(mp, os.R_OK):
                continue
        except OSError:
            continue
        out.append(mp)
    return sorted(set(out))


def _default_search_roots() -> list[dict]:
    """Default universal-search scopes for the current identity.

    Admins get /home plus the real data mounts; every other account gets its
    own home plus the shared /mnt and /media mounts. Nested duplicates are
    dropped (shallowest wins) so results are not counted twice. The root
    filesystem itself is never a default scope — searching it means crawling
    the entire disk and must be an explicit admin choice.
    """
    username, role = path_scope.current_identity()
    kept: list[str] = []
    for mp in _real_mountpoints():  # sorted → parents precede children
        if mp == "/":
            continue
        if any(mp.startswith(k + os.sep) for k in kept):
            continue
        kept.append(mp)
    if role == "admin":
        roots = [{"path": "/home", "label": "Home"}]
    else:
        home = path_scope.home_dir(username)
        roots = [{"path": home, "label": "Home"}] if home else []
    for mp in kept:
        if mp.startswith(("/mnt/", "/media/")):
            label = os.path.basename(mp.rstrip("/")) or mp
            roots.append({"path": mp, "label": f"{label} ({mp})"})
    return roots


@router.get("/search-roots")
async def get_search_roots() -> dict:
    """Sensible search scopes for the caller (admin also gets `/`)."""
    roots = _default_search_roots()
    _username, role = path_scope.current_identity()
    if role == "admin" and "/" in _real_mountpoints():
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

    exts = SEARCH_TYPE_EXTS.get(file_type) if file_type != "all" else None
    min_size = int(min_size_mb * 1024 * 1024)

    if roots:
        raw_roots = [r.strip() for r in roots.split(",") if r.strip()]
    else:
        raw_roots = [r["path"] for r in _default_search_roots()]

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
        *[asyncio.to_thread(_walk_search, r, term, limit, exts, min_size) for r in valid]
    ) if valid else []

    root_stats: list[dict] = []
    merged: list[dict] = []
    for root, (res, trunc) in zip(valid, outcomes, strict=True):
        root_stats.append({"root": root, "ok": True, "count": len(res), "truncated": trunc})
        for item in res:
            item["root"] = root
        merged.extend(res)

    merged.sort(key=lambda x: x.get("modified", 0), reverse=True)
    total_cap = 2000
    if len(merged) > total_cap:
        merged = merged[:total_cap]

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
