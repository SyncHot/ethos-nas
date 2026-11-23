"""Video/image thumbnail engine (master frame, slots, covers, face crop).

The file manager used to run ffmpeg inline per request and per size — a grid
of videos answered 202 for most tiles and the thumbnails never appeared. The
replacement contract:

* one ffmpeg run per file → a *master* frame at ``VIDEO_THUMB_WIDTH``; every
  requested size is a cheap PIL downscale of that master (a "slot")
* cache hits are mtime+size checks against a sidecar meta file
* failures back off exponentially and finally become a permanent marker that
  serves a static SVG instead of retrying forever
* custom covers (a frame picked by the user) live in their own cache tree and
  override the master until the source changes

Extracted from ``routers/files.py`` (TODO.md P2-03); the router keeps the HTTP
endpoints and delegates here.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from fastapi import Response

from backend.config import settings
from backend.services import child_limits, media_cache, package_client, pressure_guard, task_registry

logger = logging.getLogger("ethos-nas.services.thumbnails")

THUMB_DIR = media_cache.THUMB_DIR  # compat alias; resolve at call time
_ensure_thumb_dir = media_cache.ensure_thumb_dir
_make_thumb_key = media_cache.make_thumb_key
_thumb_meta_file = media_cache.thumb_meta_file
_cache_is_fresh = media_cache.cache_is_fresh
_write_thumb_meta = media_cache.write_thumb_meta
_probe_duration = media_cache.probe_duration
_append_bytes = media_cache.append_bytes

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


def _video_master_file(abs_path: str, face: bool = True) -> Path:
    """Full-width frame extracted from the video (the only ffmpeg artifact).

    ``face=False`` keeps a separate master for callers that opt out of the
    face-detection crop (the File Manager).
    """
    suffix = "" if face else "_noface"
    return media_cache.THUMB_DIR / (
        f"{_video_cache_base(abs_path)}_{_VIDEO_THUMB_VERSION}{suffix}.webp")


def _video_slot_file(abs_path: str, size: int, face: bool = True) -> Path:
    """Per-size WebP rendered from the master frame."""
    suffix = "" if face else "_noface"
    return media_cache.THUMB_DIR / (
        f"{_video_cache_base(abs_path)}_{_VIDEO_THUMB_VERSION}{suffix}_{size}.webp")


def _video_fail_file(abs_path: str, face: bool = True) -> Path:
    suffix = "" if face else "_noface"
    return media_cache.THUMB_DIR / (
        f"{_video_cache_base(abs_path)}_{_VIDEO_THUMB_VERSION}{suffix}.fail.json")


def _video_master_is_fresh(abs_path: str, face: bool = True) -> bool:
    master = _video_master_file(abs_path, face)
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


def _read_video_thumb_failure(abs_path: str, face: bool = True) -> dict:
    """Failure marker for the current source; {} when absent or stale.

    The marker stores the source mtime+size, so replacing/re-encoding a file
    that used to fail starts a fresh set of attempts.
    """
    try:
        data = json.loads(_video_fail_file(abs_path, face).read_text())
        if not isinstance(data, dict):
            return {}
        st = os.stat(abs_path)
        if (abs(st.st_mtime - float(data.get("src_mtime", 0))) >= 1.0
                or int(data.get("src_size", -1)) != st.st_size):
            return {}
        return data
    except (OSError, ValueError, TypeError):
        return {}


def _record_video_thumb_failure(abs_path: str, reason: str, permanent: bool = False,
                                face: bool = True) -> dict:
    """Bump the attempt counter and schedule the next try (exponential)."""
    try:
        st = os.stat(abs_path)
    except OSError:
        return {}
    attempts = int(_read_video_thumb_failure(abs_path, face).get("attempts", 0)) + 1
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
        _video_fail_file(abs_path, face).write_text(json.dumps(data))
    except OSError:
        pass  # bookkeeping only — a lost marker costs one extra attempt
    return data


def _clear_video_thumb_failure(abs_path: str, face: bool = True):
    with contextlib.suppress(OSError):
        _video_fail_file(abs_path, face).unlink()


def _video_thumb_backoff(abs_path: str, face: bool = True) -> dict | None:
    """Active backoff marker, or None when a new attempt is allowed now."""
    data = _read_video_thumb_failure(abs_path, face)
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


async def _generate_video_master(abs_path: str, face: bool = True) -> bool:
    """Background worker: one niced ffmpeg frame grab into the master slot."""
    if _cover_is_fresh(abs_path):
        # A user cover supersedes the automatic frame — not a failure.
        return False
    try:
        if os.path.getsize(abs_path) > _VIDEO_THUMB_MAX_BYTES:
            logger.warning("Video too large for thumbnail: %s", abs_path)
            _record_video_thumb_failure(abs_path, "too_large", permanent=True, face=face)
            return False
    except OSError:
        return False
    master = _video_master_file(abs_path, face)
    ok = False
    async with _video_thumb_semaphore:
        try:
            ok = await asyncio.to_thread(
                _extract_video_frame, abs_path, VIDEO_THUMB_WIDTH, master, face_crop=face)
        except Exception as exc:
            logger.warning("Video thumbnail generation failed for %s: %s",
                           abs_path, exc, exc_info=True)
    if ok and master.exists():
        _clear_video_thumb_failure(abs_path, face)
        return True
    _record_video_thumb_failure(abs_path, "no_frame", face=face)
    return False


def _queue_video_thumb(abs_path: str, face: bool = True) -> bool:
    """Deduplicated enqueue of a master-frame job. True when a job is pending."""
    key = abs_path if face else abs_path + "|noface"
    task = _VIDEO_THUMB_INFLIGHT.get(key)
    if task is not None and not task.done():
        return True
    if len(_VIDEO_THUMB_INFLIGHT) >= _VIDEO_THUMB_QUEUE_MAX:
        return False
    if not pressure_guard.heavy_work_allowed():
        # Low-memory window: do not start new ffmpeg runs; the grid polls again.
        return False
    task = task_registry.spawn(_generate_video_master(abs_path, face), name="video-thumb")
    if task is None:
        return False
    _VIDEO_THUMB_INFLIGHT[key] = task
    task.add_done_callback(lambda _t, k=key: _VIDEO_THUMB_INFLIGHT.pop(k, None))
    return True

# ── Custom video covers (user-captured frame shown as the file's thumbnail) ──

def _cover_master_file(abs_path: str) -> Path:
    """Full-resolution JPEG master of a user-set cover, keyed by path only."""
    h = hashlib.sha256(f"cover:{abs_path}".encode()).hexdigest()[:16]
    return media_cache.THUMB_DIR / f"{h}_cover.jpg"

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
    subprocess.run(child_limits.wrap(cmd, mem_mb=settings.CHILD_THUMB_MEM_LIMIT_MB, nice=0),
                   capture_output=True, timeout=60)


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
                child_limits.wrap(
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
                    mem_mb=settings.CHILD_THUMB_MEM_LIMIT_MB,
                    nice=0,  # the prefix already runs `nice -n 19`
                ),
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
