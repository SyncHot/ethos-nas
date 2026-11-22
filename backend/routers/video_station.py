"""EthOS NAS — Videos router (media library, thumbnails, streaming).

Library model:
- Configured root folders (any allowed location) define the video library.
  Saved to backend/data/video_station_config.json via GET/POST /config.
- Browsing is live (no index): GET /library walks a folder on demand;
  "All Videos" (empty path) does a bounded recursive walk over all roots.

REST:
- GET  /api/videos/config            — saved root folders + display/playback options
- POST /api/videos/config            — save root folders + display/playback options
- GET  /api/videos/bootstrap         — config + engine + home + roots + resume + My List
- GET  /api/videos/status            — ffmpeg/ffprobe + VA-API/QSV/software capabilities
- GET  /api/videos/browse?path=...   — subdirectories for the folder picker
- GET  /api/videos/library?path=&q=  — folders + videos under a path (or all)
- GET  /api/videos/stream?path=...   — Range-capable raw file streaming
- POST /api/videos/hls               — start a bounded fMP4 HLS remux/transcode session
- GET  /api/videos/hls/{sid}/…       — playlist/segments (owner-checked)
- POST|DELETE /api/videos/hls/{sid}/stop
- GET  /api/videos/transcode?path=&t=&a=&q=
  Legacy live fragmented-MP4 pipe (fallback / older clients). The server picks
  the lightest path that works:
    remux     — video copied losslessly (-c:v copy), audio → AAC only when needed.
    transcode — full re-encode (VA-API/QSV when available, libx264 otherwise) +
                hardware decode when the per-file probe accepts it + AAC audio.
- GET  /api/videos/subtitles?path=.. — sibling .srt/.vtt files for a video
- GET  /api/videos/resume?path=...   — saved playback position (or null)
- POST /api/videos/resume            — save/clear playback position
- DELETE /api/videos/resume?path=... — remove from Continue Watching
- GET  /api/videos/list              — per-user My List paths
- POST /api/videos/list              — add/remove a path
- DELETE /api/videos/list?path=...   — remove a path
- GET  /api/videos/thumbnail?path=.  — ffmpeg frame grab, disk-cached (404 w/o ffmpeg)
- GET  /api/videos/info?path=...     — ffprobe metadata, disk-cached
- GET  /api/videos/metadata/status   — provider/keyless state + match counts
- POST /api/videos/metadata/refresh  — fetch metadata (batch or one file) with
                                       Cinemeta/TVmaze (keyless) or TMDB
- GET  /api/videos/metadata/image    — locally cached poster/backdrop
- GET  /api/videos/tmdb/*            — legacy aliases kept for Android 1.4.x
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

# Shared path sanitizer from the File Manager router (delegates to
# services/path_scope.py, so the caller's identity is enforced here too).
from backend.config import settings
from backend.routers.files import _sanitize, _sanitize_read
from backend.services import path_scope, task_registry, user_data, video_engine, video_hls
from backend.services.metadata import service as metadata_service
from backend.services.metadata.base import MetadataConfigError
from backend.services.video_metadata import parse_video_name

logger = logging.getLogger("ethos-nas.routers.video_station")

router = APIRouter(dependencies=[Depends(path_scope.scope_request)])

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".flv",
              ".wmv", ".mpg", ".mpeg", ".ts", ".mts", ".3gp"}
SUBTITLE_EXTS = {".srt", ".vtt"}
# Directories that never contain user video — skip them entirely so dev trees
# (node_modules, venvs) don't slow scans or pollute the library.
NON_MEDIA_DIRS = {"node_modules", "venv", ".venv"}

# settings.DATA_DIR honors ETHOS_DATA_DIR so isolated test instances don't
# clobber production state (was hardcoded to backend/data/).
DATA_DIR = settings.DATA_DIR

from backend.services import json_store


def _config_store():
    """Per-user library config (roots/options/TMDB), never shared."""
    return user_data.user_store("video_station_config.json", version=1)


def _resume_store():
    """Per-user playback positions, keyed by absolute file path."""
    return user_data.user_store("video_station_resume.json")


THUMB_DIR = DATA_DIR / "video_thumbs"
INFO_CACHE_FILE = DATA_DIR / "video_info_cache.json"

_INFO_STORE = json_store.get(INFO_CACHE_FILE)  # {path: info}

DEFAULT_CONFIG = {
    "roots": [],
    "options": {
        "sort": "name",
        "view": "grid",
        # Transcode presets (see QUALITY_PRESETS): full re-encode only happens
        # when the browser can't decode the video codec itself.
        "quality": "balanced",
        # AAC bitrate used for remux/transcode audio (AC3/EAC3/DTS → AAC).
        "audio_bitrate": "192k",
        # Preferred audio track language (ISO 639-2, e.g. "pol", "eng");
        # empty = first track in the file.
        "preferred_audio_lang": "",
        # Auto-attach a sibling .srt/.vtt when playing.
        "auto_subtitles": True,
        # Series: start the next episode automatically on end.
        "autoplay_next": True,
        # Decode with VA-API/QSV when the probe says the codec is supported;
        # falls back to software automatically per file.
        "hardware_decode": True,
    },
    # Metadata (Phase 6/9) — display-only metadata + locally cached artwork.
    # The section keeps its historical `tmdb` name for API compatibility, but
    # `provider` can be keyless (Cinemeta/TVmaze), auto, TMDB or off. api_key is
    # masked in GET /config; an empty value on POST keeps the stored key and
    # `clear_key` removes it explicitly.
    "tmdb": {"api_key": "", "auto_fetch": True, "language": "pl-PL",
             "provider": "auto"},
}

QUALITY_PRESETS = video_engine.QUALITY_PRESETS

AUDIO_BITRATES = video_engine.AUDIO_BITRATES

# Caps for the bounded "All Videos" recursive walk.
ALL_VIDEOS_MAX_DEPTH = 8
ALL_VIDEOS_MAX_FILES = 10000

FFMPEG_BIN = shutil.which("ffmpeg")
# Audio codecs browsers decode natively inside <video>. Anything else (AC3/EAC3/
# DTS/TrueHD — common in movie rips) must be transcoded to AAC.
BROWSER_AUDIO_OK = video_engine.BROWSER_AUDIO_OK
# Video codecs browsers reliably decode. HEVC/AV1/MPEG-4 etc. get re-encoded
# to H.264 so playback works everywhere.
BROWSER_VIDEO_OK = video_engine.BROWSER_VIDEO_OK
# Containers the browser can demux directly in <video>.
STREAMABLE_EXTS = video_engine.STREAMABLE_EXTS


def _playback_mode(info: dict, abs_path: str) -> str:
    """How the browser should get this file (see services/video_engine.py)."""
    return video_engine.select_playback_mode(
        info, abs_path, ffmpeg_available=bool(FFMPEG_BIN),
    )

def _needs_transcode(info: dict, abs_path: str) -> bool:
    """Kept for API compatibility (Android app): True when the file must go
    through /transcode instead of raw /stream."""
    return _playback_mode(info, abs_path) != "direct"
FFPROBE_BIN = shutil.which("ffprobe")

_ENGINE = video_engine.HardwareProbe(FFMPEG_BIN, FFPROBE_BIN)
_HLS = video_hls.MANAGER

_resume_lock = asyncio.Lock()


# ── Config persistence ────────────────────────────────────────────────────────


def _sanitize_tmdb(tmdb_cfg, prev=None) -> dict:
    """Validate the metadata section; partial patches keep previous values."""
    return metadata_service.sanitize_config(tmdb_cfg, prev)

def _mask_tmdb(tmdb_cfg: dict) -> dict:
    """Config-safe view of the metadata section (raw key never leaves the server)."""
    return metadata_service.mask_config(tmdb_cfg)

def _sanitize_options(options: dict) -> dict:
    """Merge unknown/invalid values back to defaults; whitelist-validate the rest."""
    base = {**DEFAULT_CONFIG["options"]}
    if isinstance(options, dict):
        for k in base:
            v = options.get(k)
            if v is None:
                continue
            if (k == "quality" and v in QUALITY_PRESETS) or \
                    (k == "audio_bitrate" and v in AUDIO_BITRATES):
                base[k] = v
            elif k in ("sort", "view") and isinstance(v, str) and v:
                base[k] = v[:32]
            elif k == "preferred_audio_lang" and isinstance(v, str):
                base[k] = v.strip().lower()[:8]
            elif k in ("auto_subtitles", "autoplay_next", "hardware_decode"):
                base[k] = bool(v)
    return base

def _load_config() -> dict:
    data = _config_store().load()
    if not isinstance(data, dict) or not data:
        return json.loads(json.dumps(DEFAULT_CONFIG))
    roots = [r for r in (data.get("roots") or []) if isinstance(r, str) and _is_allowed(r)]
    return {"roots": roots, "options": _sanitize_options(data.get("options") or {}),
            "tmdb": _sanitize_tmdb(data.get("tmdb"))}


def _save_config(config: dict) -> None:
    _config_store().save(config)


def _is_allowed(abs_path: str) -> bool:
    """True when the caller's own path scope accepts `abs_path`."""
    try:
        path_scope.sanitize(abs_path)
        return True
    except HTTPException:
        return False


def _is_allowed_read(abs_path: str) -> bool:
    """Own scope or a received share — for read-only browsing endpoints."""
    try:
        path_scope.sanitize_read(abs_path)
        return True
    except HTTPException:
        return False

def _looks_like_transport_stream(path: str) -> bool:
    """True when a .ts/.mts file actually looks like an MPEG-2 transport
    stream. The .ts extension is shared with TypeScript source, which shows
    up en masse under /home in dev checkouts; real TS video has the 0x47 sync
    byte at every 188-byte packet boundary."""
    try:
        with open(path, "rb") as f:
            head = f.read(188 * 4)
    except OSError:
        return False
    if len(head) < 188 * 3:
        return False
    return all(head[i] == 0x47 for i in (0, 188, 376))

def _is_video_file(name: str, path: str) -> bool:
    """Extension check + content sniff for the ambiguous .ts/.mts extensions."""
    ext = os.path.splitext(name)[1].lower()
    if ext not in VIDEO_EXTS:
        return False
    if ext in (".ts", ".mts"):
        return _looks_like_transport_stream(path)
    return True


# ── Library walking ───────────────────────────────────────────────────────────


def _list_dir(path: str, query: str = "") -> dict:
    """One directory level: subfolders (that contain videos somewhere below or
    are roots of interest) + video files directly inside."""
    abs_path = os.path.abspath(os.path.expanduser(path))
    if not _is_allowed_read(abs_path):
        raise HTTPException(status_code=403, detail="Path outside allowed roots")
    if not os.path.isdir(abs_path):
        raise HTTPException(status_code=404, detail="Folder not found")

    q = query.lower()
    folders: list[dict] = []
    videos: list[dict] = []
    try:
        entries = sorted(os.scandir(abs_path), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Permission denied") from exc

    for entry in entries:
        name = entry.name
        if name.startswith(".") or name in NON_MEDIA_DIRS:
            continue
        try:
            if entry.is_dir(follow_symlinks=False) and _dir_has_videos(entry.path, max_files=200):
                # Only show folders that actually contain video content below.
                folders.append({"name": name, "path": os.path.join(abs_path, name)})
            elif (entry.is_file(follow_symlinks=True)
                    and _is_video_file(name, entry.path) and (not q or q in name.lower())):
                st = entry.stat()
                item = {
                    "name": name,
                    "path": entry.path,
                    "size": st.st_size,
                    "modified": int(st.st_mtime),
                }
                item.update(parse_video_name(name, folder=os.path.basename(abs_path)))
                videos.append(item)
        except OSError:
            continue

    _attach_metadata(videos)
    return {
        "path": abs_path,
        "parent": os.path.dirname(abs_path) if abs_path != os.path.dirname(abs_path) else None,
        "folders": folders,
        "videos": videos,
    }


def _dir_has_videos(path: str, max_files: int = 200) -> bool:
    """Cheap check used to prune empty branches from the folder listing."""
    seen = 0
    stack = [path]
    while stack and seen < max_files:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    name = entry.name
                    if name.startswith(".") or name in NON_MEDIA_DIRS:
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=True):
                            seen += 1
                            if _is_video_file(name, entry.path):
                                return True
                    except OSError:
                        continue
        except OSError:
            continue
    return False


def _walk_all(roots: list[str], query: str = "") -> dict:
    """Bounded recursive walk over all configured roots (the 'All Videos' view)."""
    q = query.lower()
    videos: list[dict] = []

    for root in roots:
        if not os.path.isdir(root):
            continue
        stack = [(root, 0)]
        while stack and len(videos) < ALL_VIDEOS_MAX_FILES:
            cur, depth = stack.pop()
            try:
                entries = list(os.scandir(cur))
            except OSError:
                continue
            for entry in sorted(entries, key=lambda e: e.name.lower()):
                name = entry.name
                if name.startswith(".") or name in NON_MEDIA_DIRS:
                    continue
                try:
                    if (entry.is_dir(follow_symlinks=False)
                            and depth + 1 < ALL_VIDEOS_MAX_DEPTH
                            and len(videos) < ALL_VIDEOS_MAX_FILES):
                        stack.append((entry.path, depth + 1))
                    elif (entry.is_file(follow_symlinks=True)
                            and _is_video_file(name, entry.path) and (not q or q in name.lower())):
                        st = entry.stat()
                        item = {
                            "name": name,
                            "path": entry.path,
                            "folder": cur,
                            "size": st.st_size,
                            "modified": int(st.st_mtime),
                        }
                        item.update(parse_video_name(name, folder=os.path.basename(cur)))
                        videos.append(item)
                except OSError:
                    continue

    # Top-level folders of each root (for the sidebar-style navigation).
    top_folders = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            with os.scandir(root) as it:
                for entry in sorted(it, key=lambda e: e.name.lower()):
                    if (entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".")
                            and entry.name not in NON_MEDIA_DIRS
                            and _dir_has_videos(entry.path)):
                        top_folders.append({"name": entry.name, "path": entry.path})
        except OSError:
            continue

    _attach_metadata(videos)
    return {"path": None, "parent": None, "folders": top_folders, "videos": videos,
            "truncated": len(videos) >= ALL_VIDEOS_MAX_FILES}


# ── Library cache (one walk shared by /home, /library, /bootstrap) ────────────

_LIB_CACHE: dict = {"key": None, "ts": 0.0, "listing": None}
_LIB_CACHE_TTL = 10.0
_lib_cache_lock = asyncio.Lock()


async def _walk_all_cached(roots: list[str], query: str = "", *, refresh: bool = False) -> dict:
    """One bounded walk shared by home/library/bootstrap for a short TTL."""
    key = tuple(roots)
    cached = _LIB_CACHE.get("listing")
    now = time.monotonic()
    if (not refresh and cached is not None and _LIB_CACHE.get("key") == key
            and now - float(_LIB_CACHE.get("ts") or 0) < _LIB_CACHE_TTL):
        listing = cached
    else:
        async with _lib_cache_lock:
            cached = _LIB_CACHE.get("listing")
            now = time.monotonic()
            if (not refresh and cached is not None and _LIB_CACHE.get("key") == key
                    and now - float(_LIB_CACHE.get("ts") or 0) < _LIB_CACHE_TTL):
                listing = cached
            else:
                listing = await asyncio.to_thread(_walk_all, roots, "")
                _LIB_CACHE.update(key=key, ts=time.monotonic(), listing=listing)
    if not query:
        return listing
    q = query.lower()
    return {
        **listing,
        "videos": [v for v in listing.get("videos", []) if q in (v.get("name") or "").lower()],
    }


def _home_payload(videos: list[dict], resumes: dict, limit: int) -> dict:
    cw: list[tuple[float, dict]] = []
    for v in videos:
        r = resumes.get(v["path"])
        if not r or not r.get("duration"):
            continue
        t = float(r.get("time") or 0)
        d = float(r["duration"])
        if t < 5 or t >= d * 0.97:
            continue
        item = {**v, "resume_time": round(t, 2), "resume_duration": round(d, 2)}
        cw.append((float(r.get("updated_at") or 0), item))
    cw.sort(key=lambda x: x[0], reverse=True)
    recently = sorted(videos, key=lambda v: v.get("modified") or 0, reverse=True)[:limit]
    return {"continue_watching": [it for _, it in cw[:limit]], "recently_added": recently}


# ── Resume positions ──────────────────────────────────────────────────────────


def _load_resume_map() -> dict:
    data = _resume_store().load()
    return data if isinstance(data, dict) else {}


def _save_resume_map(m: dict) -> None:
    _resume_store().save(m)


def _list_store():
    """Per-user My List (server-side so web and Android agree)."""
    return user_data.user_store("video_station_list.json")


def _load_list_paths() -> list[str]:
    data = _list_store().load()
    paths = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(paths, list):
        return []
    return [p for p in paths if isinstance(p, str)][:2000]


# ── ffprobe / ffmpeg helpers ──────────────────────────────────────────────────

_info_cache_lock = asyncio.Lock()


def _load_info_cache() -> dict:
    data = _INFO_STORE.load()
    return data if isinstance(data, dict) else {}


def _save_info_cache(cache: dict) -> None:
    # Keep the cache bounded before persisting.
    if len(cache) > 500:
        items = sorted(cache.items(), key=lambda kv: kv[1].get("checked_at", 0))
        for k, _ in items[:len(cache) - 400]:
            cache.pop(k, None)
    _INFO_STORE.save(cache)


def _ffprobe_sync(abs_path: str) -> dict:
    """Run ffprobe and reduce to the fields the UI needs."""
    cmd = [FFPROBE_BIN, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", abs_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr.strip()[:300]}")
    data = json.loads(proc.stdout or "{}")

    fmt = data.get("format") or {}
    vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    astreams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    astream = astreams[0] if astreams else None

    def _int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    info = {
        "duration": _int(fmt.get("duration")),
        "width": _int((vstream or {}).get("width")),
        "height": _int((vstream or {}).get("height")),
        "video_codec": (vstream or {}).get("codec_name"),
        "pix_fmt": (vstream or {}).get("pix_fmt"),
        "audio_codec": (astream or {}).get("codec_name"),
        "bitrate": _int(fmt.get("bit_rate")),
        # Every audio stream, in file order — index is the position among
        # audio streams and maps directly to ffmpeg's `0:a:<index>`.
        "audio_tracks": [
            {
                "index": i,
                "codec": (s.get("codec_name") or "").lower(),
                "language": ((s.get("tags") or {}).get("language") or "").strip().lower()[:3],
                "channels": _int(s.get("channels")),
                # Układ kanałów decyduje o AAC: „5.1(side)” (typowy dla DD+ w
                # WEB-DL) nie ma odpowiednika w MPEG-4, więc ffmpeg zapisuje PCE,
                # którego przeglądarki nie przyjmują w MSE.
                "channel_layout": (s.get("channel_layout") or "").strip().lower(),
                # Rola audio (np. "commentary", "main", "dub") — ffprobe wystawia
                # ją w stream tags. Pomaga rozróżnić ścieżki o tym samym kodzie
                # języka (np. PL main + PL commentary). Puste/null = brak metadanych.
                "role": ((s.get("tags") or {}).get("role") or "").strip().lower(),
            }
            for i, s in enumerate(astreams)
        ],
    }
    return info


async def _probe(abs_path: str) -> dict:
    """ffprobe with disk cache keyed by path, validated against mtime+size."""
    try:
        st = os.stat(abs_path)
    except OSError as exc:
        raise HTTPException(status_code=404, detail=f"File not found ({exc})") from exc

    key = abs_path
    async with _info_cache_lock:
        cache = _load_info_cache()
        hit = cache.get(key)
        # schema 3 added pix_fmt, schema 4 channel_layout; older entries are
        # treated as misses.
        if (hit and hit.get("schema") == 4 and hit.get("mtime") == st.st_mtime
                and hit.get("size") == st.st_size):
            return {k: v for k, v in hit.items() if k not in ("mtime", "size", "schema")}

    try:
        info = await asyncio.to_thread(_ffprobe_sync, abs_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="ffprobe is not installed on the server") from exc
    except Exception as exc:
        logger.warning("ffprobe failed for %s: %s", abs_path, exc)
        raise HTTPException(status_code=502, detail=f"Could not probe file: {exc}") from exc

    async with _info_cache_lock:
        cache = _load_info_cache()
        cache[key] = {"schema": 4, "mtime": st.st_mtime, "size": st.st_size,
                      "checked_at": __import__("time").time(), **info}
        try:
            _save_info_cache(cache)
        except Exception as exc:
            logger.warning("Could not persist info cache: %s", exc)
    return info


def _thumb_key(abs_path: str, st) -> Path:
    h = hashlib.sha256(f"{abs_path}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()
    return THUMB_DIR / f"{h}.jpg"


def _make_thumb_sync(
    abs_path: str,
    out_file: Path,
    width: int,
    duration: int | None,
    seek_override: float | None = None,
) -> None:
    """Grab one frame with ffmpeg. Seeks to ~10% (or 2s) so the first black
    frames of long intros are skipped. ``seek_override`` (sekundy) służy do
    podglądu klatki przy przewijaniu — wtedy bierzemy dokładnie ten moment."""
    if seek_override is not None:
        seek = max(0.0, seek_override)
    else:
        seek = min(2.0, max(0.5, (duration or 10) * 0.1)) if duration else 2.0
    # -f image2 is mandatory: the temp filename ends in .tmp, which ffmpeg
    # cannot use to infer the output format ("Unable to choose an output
    # format" → exit 234 before any frame is written).
    cmd = [FFMPEG_BIN, "-y", "-v", "error", "-ss", f"{seek:.2f}", "-i", abs_path,
           "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5",
           "-f", "image2", str(out_file)]
    subprocess.run(cmd, capture_output=True, timeout=40)


# ── REST endpoints ────────────────────────────────────────────────────────────


@router.get("/config")
async def get_config() -> dict:
    cfg = _load_config()
    username, role = path_scope.current_identity()
    # Auto-seed with the current user's home on first run so the app is not empty.
    if not cfg["roots"]:
        home = path_scope.home_dir(username)
        if home and _is_allowed(home):
            cfg["roots"] = [home]
    return {**cfg, "allowed_roots": list(path_scope.allowed_roots(username, role)),
            "tmdb": _mask_tmdb(cfg.get("tmdb") or {})}


@router.post("/config")
async def set_config(payload: dict = Body(...)) -> dict:
    roots_in = payload.get("roots") or []
    options_in = payload.get("options") or {}
    roots = []
    for raw in roots_in:
        if not isinstance(raw, str):
            continue
        try:
            root = path_scope.sanitize(raw)
        except HTTPException:
            continue
        if os.path.isdir(root):
            roots.append(root)
    prev = _load_config()
    cfg = {"roots": roots, "options": _sanitize_options(options_in),
           "tmdb": _sanitize_tmdb(payload.get("tmdb"), prev.get("tmdb"))}
    _save_config(cfg)
    return {**cfg, "tmdb": _mask_tmdb(cfg["tmdb"])}

def _engine_status_payload(hw: dict) -> dict:
    return {
        "ffmpeg": bool(FFMPEG_BIN),
        "ffprobe": bool(FFPROBE_BIN),
        "qsv": hw.get("encoder") == "qsv",      # kept for older clients
        "vaapi": hw.get("encoder") == "vaapi",
        "encoder": hw.get("encoder"),
        "hw": hw,
        "active_transcodes": video_engine.LIMITER.snapshot(),
    }


@router.get("/status")
async def status(refresh: bool = False) -> dict:
    """Media-engine capabilities for the settings UI (HW probe is cached).

    `?refresh=1` forces a fresh GPU probe — handy after installing a driver or
    fixing device permissions without bouncing the whole service.
    """
    hw = await _ENGINE.status(refresh=refresh)
    return _engine_status_payload(hw)


@router.get("/browse")
async def browse(path: str | None = Query(default=None)) -> dict:
    """Subdirectory picker (restricted to allowed roots)."""
    base = os.path.abspath(os.path.expanduser(path)) if path else None
    if base is None:
        # Top level of every root the caller may use.
        username, role = path_scope.current_identity()
        roots = path_scope.allowed_roots(username, role)

        def _roots() -> list[dict]:
            out = []
            for r in roots:
                if not os.path.isdir(r):
                    continue
                try:
                    entries = sorted((e.name for e in os.scandir(r)
                                      if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")),
                                     key=str.lower)
                except OSError:
                    continue
                out.append({"path": r, "name": os.path.basename(r) or r, "folders": entries})
            return out

        return {"roots": await asyncio.to_thread(_roots)}

    if not _is_allowed_read(base):
        raise HTTPException(status_code=403, detail="Path outside allowed roots")
    if not os.path.isdir(base):
        raise HTTPException(status_code=404, detail="Folder not found")
    try:
        folders = await asyncio.to_thread(
            lambda: sorted((e.name for e in os.scandir(base)
                            if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")),
                           key=str.lower),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Permission denied") from exc
    return {"path": base, "parent": os.path.dirname(base), "folders": folders}


@router.get("/library")
async def library(
    path: str | None = Query(default=None),
    q: str = Query(default=""),
    refresh: bool = Query(default=False),
) -> dict:
    """Folder listing (single level) or the bounded 'All Videos' walk."""
    if not path:
        cfg = _load_config()
        return await _walk_all_cached(cfg["roots"], q, refresh=refresh)
    return await asyncio.to_thread(_list_dir, path, q)


@router.get("/home")
async def home(limit: int = Query(default=12, ge=1, le=50)) -> dict:
    """Jellyfin-style home rows computed from one bounded walk over all roots."""
    cfg = _load_config()
    listing = await _walk_all_cached(cfg["roots"])
    return _home_payload(listing.get("videos", []), _load_resume_map(), limit)


@router.get("/bootstrap")
async def bootstrap(limit: int = Query(default=12, ge=1, le=50)) -> dict:
    """One call for the app shell: config + engine + home + roots + resume + list."""
    cfg = _load_config()
    listing = await _walk_all_cached(cfg["roots"])
    resumes = _load_resume_map()
    hw = await _ENGINE.status()
    username, role = path_scope.current_identity()
    _schedule_auto_metadata(username, cfg, listing.get("videos", []))
    return {
        "config": {**cfg, "tmdb": _mask_tmdb(cfg.get("tmdb") or {}),
                   "allowed_roots": list(path_scope.allowed_roots(username, role))},
        "status": _engine_status_payload(hw),
        "home": _home_payload(listing.get("videos", []), resumes, limit),
        "roots": cfg.get("roots", []),
        "folders": listing.get("folders", []),
        "total": len(listing.get("videos", [])),
        "truncated": bool(listing.get("truncated")),
        "resume": resumes,
        "list": _load_list_paths(),
    }

@router.get("/stream")
async def stream(path: str = Query(...)) -> FileResponse:
    """Range-capable video streaming (Starlette handles HTTP Range natively).

    Serves the raw file. Files the browser can't decode directly (AC3/EAC3/DTS
    audio, HEVC video, MKV containers) should be played via /transcode instead
    — see `info` which reports `needs_transcode`. Received shares are readable.
    """
    abs_path = _sanitize_read(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")
    ext = os.path.splitext(abs_path)[1].lower()
    media_type = {
        ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
        ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
        ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv", ".mpg": "video/mpeg",
        ".mpeg": "video/mpeg", ".ts": "video/mp2t", ".mts": "video/mp2t", ".3gp": "video/3gpp",
    }.get(ext, "application/octet-stream")
    return FileResponse(abs_path, media_type=media_type)


async def _hw_encoder() -> dict:
    """Compatibility wrapper around the cached engine probe."""
    st = await _ENGINE.status()
    return {"encoder": st.get("encoder"), "device": st.get("device"),
            "reason": st.get("reason", "")}


def _transcode_bitrate(width: int, height: int) -> str:
    return video_engine.bitrate_for(width, height)


def _build_transcode_cmd(abs_path: str, info: dict, start: float, hw: str | None,
                         mode: str, track_idx: int, quality: str,
                         audio_bitrate: str, *, decode_hw: bool = False,
                         device: str | None = None) -> list[str]:
    """Compatibility wrapper — the real builder lives in services/video_engine.py."""
    return video_engine.build_transcode_cmd(
        FFMPEG_BIN or "ffmpeg", abs_path, info, start, hw, mode, track_idx,
        quality, audio_bitrate, decode_hw=decode_hw, device=device,
        loglevel=settings.FFMPEG_LOGLEVEL,
    )


async def _drain_stderr(stream, sink: deque) -> None:
    while True:
        chunk = await stream.read(1 << 16)
        if not chunk:
            break
        sink.append(chunk)


@router.get("/transcode")
async def transcode(
    path: str = Query(...),
    t: float = Query(default=0.0, ge=0),
    a: int = Query(default=0, ge=0),
    q: str = Query(default="balanced"),
):
    """Live processing for files the browser can't play directly.

    The server picks the lightest path that works (see `_playback_mode`):
    'remux' copies the video stream losslessly and only re-encodes audio to AAC
    when needed; 'transcode' re-encodes video (hardware VA-API/QuickSync when
    the service user can open a /dev/dri render node, libx264 otherwise) with downscaling per the quality preset.
    Output is a fragmented MP4 piped straight into the response — nothing is
    written to disk. `t` = start offset in seconds; `a` = audio track index;
    `q` = quality preset (fast|balanced|high). The player re-requests with a new
    t when seeking past its buffer (a live pipe has no byte-range seek).
    """
    abs_path = _sanitize_read(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")
    if not FFMPEG_BIN:
        raise HTTPException(status_code=503, detail="ffmpeg not available")

    info = await _probe(abs_path)
    mode = _playback_mode(info, abs_path)
    if mode == "direct":
        mode = "remux"  # defensive: nothing to fix, but remux is still safe
    duration = float(info.get("duration") or 0)
    start = max(0.0, t)
    if duration and start >= duration - 1:
        start = max(0.0, duration - 2)  # don't start inside the final GOP

    quality = q if q in QUALITY_PRESETS else "balanced"
    cfg_options = _load_config()["options"]
    audio_bitrate = cfg_options["audio_bitrate"]
    engine_status = await _ENGINE.status()
    encoder = engine_status.get("encoder") if mode == "transcode" else None
    decode_hw = False
    if mode == "transcode" and encoder and cfg_options.get("hardware_decode", True):
        decode_hw, _decode_reason = await _ENGINE.can_decode_hw(
            abs_path, info, encoder, engine_status.get("device"),
        )
    cmd = video_engine.build_transcode_cmd(
        FFMPEG_BIN, abs_path, info, start, encoder, mode, a, quality,
        audio_bitrate, decode_hw=decode_hw, device=engine_status.get("device"),
        loglevel=settings.FFMPEG_LOGLEVEL,
    )
    hw_tag = encoder or "none"
    decode_tag = "hw" if decode_hw else "sw"
    logger.info("Transcode start | mode=%s hw=%s decode=%s track=%d q=%s input=%s t=%.2f\ncmd: %s",
                mode, hw_tag, decode_tag, a, quality, abs_path, start, shlex.join(cmd))

    if not await video_engine.LIMITER.acquire(timeout=5.0):
        raise HTTPException(status_code=429, detail="Transcoder is busy — try again in a moment",
                            headers={"Retry-After": "5"})

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception as exc:
        await video_engine.LIMITER.release()
        logger.error("Transcode spawn failed | input=%s: %s", abs_path, exc)
        raise HTTPException(status_code=502, detail=f"Could not start transcode: {exc}") from exc

    async def _stream():
        err_tail = deque(maxlen=64)  # 16 KiB chunks → ≤1 MiB of stderr kept
        drain: asyncio.Task | None = None
        t0 = time.monotonic()
        try:
            if proc.stdout is not None and proc.stderr is not None:
                drain = asyncio.create_task(_drain_stderr(proc.stderr, err_tail))
                while True:
                    chunk = await proc.stdout.read(1 << 16)
                    if not chunk:
                        break
                    yield chunk
                await proc.wait()
        finally:
            if drain is not None:
                drain.cancel()
            if proc.returncode is None:  # client went away → kill ffmpeg
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await proc.wait()
            await video_engine.LIMITER.release()
            tail = b"".join(err_tail).decode("utf-8", "replace")[-2000:]
            elapsed = time.monotonic() - t0
            if proc.returncode == 0:
                logger.info("Transcode done | mode=%s hw=%s decode=%s input=%s elapsed=%.1fs",
                            mode, hw_tag, decode_tag, abs_path, elapsed)
            elif proc.returncode is None or proc.returncode < 0:
                # Killed by us — the client disconnected (seek/close). Normal.
                logger.info("Transcode stopped (client gone) | mode=%s hw=%s decode=%s input=%s "
                            "elapsed=%.1fs", mode, hw_tag, decode_tag, abs_path, elapsed)
            else:
                logger.error(
                    "Transcode FAILED | mode=%s hw=%s decode=%s input=%s rc=%s elapsed=%.1f\n"
                    "--- stderr ---\n%s", mode, hw_tag, decode_tag, abs_path, proc.returncode,
                    elapsed, tail or "(empty)")

    return StreamingResponse(_stream(), media_type="video/mp4",
                             headers={"Cache-Control": "no-store",
                                      "X-Ethos-Mode": mode,
                                      "X-Ethos-Encoder": hw_tag,
                                      "X-Ethos-Decode": decode_tag})


class HlsRequest(BaseModel):
    path: str
    t: float = 0.0
    a: int = 0
    q: str = "balanced"


@router.post("/hls")
async def start_hls(payload: HlsRequest) -> dict:
    """Start a bounded fMP4 HLS session for non-direct media (mobile-friendly)."""
    abs_path = _sanitize_read(payload.path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")
    if not FFMPEG_BIN:
        raise HTTPException(status_code=503, detail="ffmpeg not available")
    info = await _probe(abs_path)
    mode = _playback_mode(info, abs_path)
    if mode == "direct":
        raise HTTPException(status_code=400, detail="This file can be streamed directly")
    duration = float(info.get("duration") or 0)
    start = max(0.0, float(payload.t))
    if duration and start >= duration - 1:
        start = max(0.0, duration - 2)
    quality = payload.q if payload.q in QUALITY_PRESETS else "balanced"
    cfg_options = _load_config()["options"]
    audio_bitrate = cfg_options["audio_bitrate"]
    engine_status = await _ENGINE.status()
    encoder = engine_status.get("encoder") if mode == "transcode" else None
    decode_hw = False
    if mode == "transcode" and encoder and cfg_options.get("hardware_decode", True):
        decode_hw, _reason = await _ENGINE.can_decode_hw(
            abs_path, info, encoder, engine_status.get("device"),
        )

    def _factory(out_dir: str, use_encoder: str | None = encoder,
                 use_decode_hw: bool = decode_hw) -> list[str]:
        return video_engine.build_hls_cmd(
            FFMPEG_BIN, abs_path, info, start, use_encoder, mode, payload.a, quality,
            audio_bitrate, out_dir, decode_hw=use_decode_hw,
            device=engine_status.get("device"), loglevel=settings.FFMPEG_LOGLEVEL,
        )

    try:
        session = await _HLS.start(
            user_data.current_username(), abs_path, _factory,
            mode=mode, encoder=encoder, decode_hw=decode_hw,
        )
    except video_hls.HlsLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc),
                            headers={"Retry-After": "5"}) from exc
    except video_hls.HlsStartError as exc:
        # A hardware probe can pass globally while a specific file fails (e.g.
        # 10-bit HEVC on h264_vaapi). Retry once with software so the browser
        # gets a stream instead of a dead player.
        msg = str(exc).lower()
        hw_failure = encoder in ("vaapi", "qsv") and any(marker in msg for marker in (
            "no usable encoding profile", "error while opening encoder",
            "function not implemented", "hwupload", "device creation failed",
        ))
        if hw_failure:
            logger.warning(
                "Hardware encode failed | owner=%s encoder=%s input=%s — retrying with libx264: %s",
                user_data.current_username(), encoder, abs_path, exc,
            )
            encoder = "libx264"
            decode_hw = False
            try:
                session = await _HLS.start(
                    user_data.current_username(), abs_path,
                    lambda out_dir: _factory(out_dir, "libx264", False),
                    mode=mode, encoder="libx264", decode_hw=False,
                )
            except video_hls.HlsLimitError as retry_exc:
                raise HTTPException(status_code=429, detail=str(retry_exc),
                                    headers={"Retry-After": "5"}) from retry_exc
            except video_hls.HlsStartError as retry_exc:
                logger.error("HLS software fallback failed | owner=%s input=%s: %s",
                             user_data.current_username(), abs_path, retry_exc)
                raise HTTPException(status_code=502, detail=str(retry_exc)) from retry_exc
        else:
            logger.error("HLS start failed | owner=%s mode=%s encoder=%s decode=%s "
                         "input=%s: %s", user_data.current_username(), mode, encoder or "none",
                         "hw" if decode_hw else "sw", abs_path, exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "session": session.sid,
        "playlist": f"/api/videos/hls/{session.sid}/index.m3u8",
        "duration": duration,
        "mode": mode,
        "encoder": encoder,
        "decode": "hw" if decode_hw else "sw",
        "audio_tracks": info.get("audio_tracks", []),
    }


@router.get("/hls/{sid}/index.m3u8")
async def hls_playlist(sid: str, request: Request) -> Response:
    """Serve the rewritten playlist (segment URIs keep ?token= for API keys)."""
    session = await _HLS.get(sid, user_data.current_username())
    if not session:
        raise HTTPException(status_code=404, detail="Stream session not found")
    try:
        text = await asyncio.to_thread(session.playlist.read_text, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Playlist is not ready") from exc
    body = video_hls.rewrite_playlist(text, request.query_params.get("token"))
    return Response(content=body, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store"})


@router.api_route("/hls/{sid}/stop", methods=["POST", "DELETE"])
async def stop_hls(sid: str) -> dict:
    if not await _HLS.stop(sid, user_data.current_username()):
        raise HTTPException(status_code=404, detail="Stream session not found")
    return {"ok": True}


@router.get("/hls/{sid}/{name}")
async def hls_segment(sid: str, name: str) -> FileResponse:
    session = await _HLS.get(sid, user_data.current_username())
    if not session:
        raise HTTPException(status_code=404, detail="Stream session not found")
    if session.failed:
        # Enkoder padł w trakcie: 404 wygląda dla hls.js jak brak pliku i mieli
        # w miejscu. 410 z powodem pozwala UI pokazać prawdziwą przyczynę.
        raise HTTPException(status_code=410, detail=session.failure_detail())
    target = video_hls.segment_path(session, name)
    if target is None:
        raise HTTPException(status_code=404, detail="Segment not found")
    return FileResponse(str(target), media_type="video/mp4",
                        headers={"Cache-Control": "no-store"})


@router.get("/subtitles")
async def subtitles(path: str = Query(...)) -> dict:
    """Sibling subtitle files (.srt/.vtt) next to the video."""
    abs_path = _sanitize_read(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")
    folder = os.path.dirname(abs_path)
    stem = os.path.splitext(os.path.basename(abs_path))[0]

    def _find_subs() -> list[dict]:
        out: list[dict] = []
        try:
            for entry in sorted(os.scandir(folder), key=lambda e: e.name.lower()):
                if not entry.is_file(follow_symlinks=True):
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if ext in SUBTITLE_EXTS and not entry.name.startswith("."):
                    out.append({
                        "name": entry.name,
                        "path": entry.path,
                        # Preferred match: same stem as the video.
                        "preferred": os.path.splitext(entry.name)[0] == stem,
                    })
        except OSError:
            pass  # intentional: a failure here must not break the caller
        return out

    return {"subtitles": await asyncio.to_thread(_find_subs)}


@router.get("/resume")
async def get_resume(path: str = Query(...)) -> dict:
    m = _load_resume_map()
    entry = m.get(_sanitize(path))
    if not entry:
        return {"resume": None}
    return {"resume": {k: entry[k] for k in ("time", "duration", "updated_at") if k in entry}}


@router.get("/resume/all")
async def get_resume_all() -> dict:
    """Whole resume map in one request (used to draw progress bars on cards)."""
    return {"resumes": _load_resume_map()}


@router.post("/resume")
async def set_resume(payload: dict = Body(...)) -> dict:
    path = payload.get("path") or ""
    time_ = float(payload.get("time") or 0)
    duration = float(payload.get("duration") or 0)
    if not path:
        raise HTTPException(status_code=400, detail="Missing path")
    abs_path = _sanitize(path)

    async with _resume_lock:
        m = _load_resume_map()
        # Clear when the video is (nearly) finished or position is meaningless.
        if time_ < 5 or (duration and time_ >= duration * 0.97):
            m.pop(abs_path, None)
        else:
            m[abs_path] = {"time": round(time_, 2), "duration": round(duration, 2),
                           "updated_at": __import__("time").time()}
        _save_resume_map(m)
    return {"ok": True}


@router.delete("/resume")
async def clear_resume(path: str = Query(...)) -> dict:
    """Remove a title from Continue Watching without touching the file."""
    abs_path = _sanitize(path)

    def _drop(data):
        if isinstance(data, dict):
            data.pop(abs_path, None)
            return data
        return {}

    _resume_store().update(_drop)
    return {"ok": True}


class ListUpdate(BaseModel):
    path: str
    add: bool = True


@router.get("/list")
async def get_list() -> dict:
    """Per-user My List (server-side so web and Android agree)."""
    return {"paths": _load_list_paths()}


@router.post("/list")
async def set_list(payload: ListUpdate) -> dict:
    abs_path = _sanitize(payload.path)
    if payload.add and not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")

    def _mutate(data):
        paths = data.get("paths") if isinstance(data, dict) else []
        if not isinstance(paths, list):
            paths = []
        paths = [p for p in paths if isinstance(p, str) and p != abs_path]
        if payload.add:
            paths.insert(0, abs_path)
        data["paths"] = paths[:2000]
        return data

    _list_store().update(_mutate)
    return {"paths": _load_list_paths()}


@router.delete("/list")
async def delete_list(path: str = Query(...)) -> dict:
    abs_path = _sanitize(path)

    def _mutate(data):
        paths = data.get("paths") if isinstance(data, dict) else []
        if not isinstance(paths, list):
            paths = []
        data["paths"] = [p for p in paths if isinstance(p, str) and p != abs_path]
        return data

    _list_store().update(_mutate)
    return {"paths": _load_list_paths()}


@router.get("/thumbnail")
async def thumbnail(
    path: str = Query(...),
    w: int = Query(default=480, ge=160, le=1280),
    # t >= 0 → klatka z tego momentu (podgląd przy przewijaniu w aplikacji).
    # Cache kluczowany co 5 s, żeby przeciąganie nie tworzyło setek plików.
    t: float = Query(default=-1.0),
):
    """One-frame JPEG via ffmpeg, cached on disk keyed by path+mtime+size.

    Returns 404 when ffmpeg is missing or the grab fails — the UI falls back
    to a generic icon.
    """
    if not FFMPEG_BIN:
        raise HTTPException(status_code=404, detail="ffmpeg not available")
    abs_path = _sanitize_read(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")

    st = os.stat(abs_path)
    out_file = _thumb_key(abs_path, st)
    if t >= 0:
        bucket = int(t // 5) * 5
        out_file = out_file.with_name(out_file.stem + f"_t{bucket}.jpg")
    if out_file.exists() and out_file.stat().st_size > 0:
        return FileResponse(str(out_file), media_type="image/jpeg",
                            headers={"Cache-Control": "max-age=86400"})

    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    duration = None
    try:
        info = await _probe(abs_path)
        duration = info.get("duration")
    except HTTPException:
        pass  # probe failed — still try a blind grab at 2s

    tmp_file = out_file.with_suffix(".jpg.tmp")
    try:
        await asyncio.to_thread(
            _make_thumb_sync,
            abs_path,
            tmp_file,
            w,
            duration,
            (int(t // 5) * 5) if t >= 0 else None,
        )
        if not (tmp_file.exists() and tmp_file.stat().st_size > 0):
            raise RuntimeError("empty frame")
        os.replace(tmp_file, out_file)
    except Exception as exc:
        logger.warning("Thumbnail failed for %s: %s", abs_path, exc)
        tmp_file.unlink(missing_ok=True)
        raise HTTPException(status_code=404, detail="Could not generate thumbnail") from exc

    return FileResponse(str(out_file), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})


@router.get("/info")
async def info(path: str = Query(...)) -> dict:
    """ffprobe metadata (duration, resolution, codecs, bitrate) + file size."""
    abs_path = _sanitize_read(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")
    result = await _probe(abs_path)
    with contextlib.suppress(OSError):
        result["size"] = os.stat(abs_path).st_size
    mode = _playback_mode(result, abs_path)
    result["playback_mode"] = mode
    result["needs_transcode"] = mode != "direct"
    st = _ENGINE.cached_status() or {}
    decode = st.get("decode") or {}
    result["engine"] = {
        "mode": mode,
        "encoder": st.get("encoder") if mode == "transcode" else None,
        "hw_decode_capable": bool(decode.get("vaapi") or decode.get("qsv")),
        "probed": bool(st),
    }
    return {"info": result}

# ── Metadata + local artwork (Phase 9) ───────────────────────────────────────
_metadata_refresh_guard = asyncio.Lock()   # one refresh run at a time
_metadata_auto_last: dict[str, float] = {}
_AUTO_FETCH_TTL = 6 * 3600
_AUTO_FETCH_MAX_GROUPS = 20


def _metadata_config(cfg: dict) -> dict:
    return cfg.get("tmdb") or {}


def _attach_metadata(videos: list[dict]) -> None:
    """Attach cached provider metadata to library items (cache only)."""
    metadata_service.attach(videos)


def _metadata_disabled(cfg: dict) -> str:
    provider = _metadata_config(cfg).get("provider") or metadata_service.DEFAULT_PROVIDER
    if provider == "off":
        return "Metadata is disabled — choose a source in Settings → Metadata"
    if provider == "tmdb" and not _metadata_config(cfg).get("api_key"):
        return "TMDB API key is not configured — add it or choose a keyless source"
    return ""


async def _auto_metadata_fetch(groups: list, config: dict) -> None:
    """Background metadata refresh; never blocks or fails the bootstrap call."""
    await asyncio.sleep(2.0)
    if _metadata_refresh_guard.locked():
        return
    async with _metadata_refresh_guard:
        try:
            stats = await asyncio.to_thread(
                metadata_service.refresh, groups, config,
                max_groups=_AUTO_FETCH_MAX_GROUPS,
            )
            logger.info("Metadata auto-fetch finished: %s", stats)
        except Exception as exc:   # background work must never crash the app
            logger.warning("Metadata auto-fetch failed: %s", exc)


def _schedule_auto_metadata(username: str, cfg: dict, videos: list[dict]) -> None:
    """One bounded auto-fetch per user per 6 h, only for missing titles."""
    meta_cfg = _metadata_config(cfg)
    if not meta_cfg.get("auto_fetch"):
        return
    provider = meta_cfg.get("provider") or metadata_service.DEFAULT_PROVIDER
    if provider == "off" or (provider == "tmdb" and not meta_cfg.get("api_key")):
        return
    now = time.monotonic()
    last = _metadata_auto_last.get(username, 0.0)
    if now - last < _AUTO_FETCH_TTL:
        return
    matches = metadata_service.load_matches()
    groups = metadata_service.build_groups(
        videos, matches, max_groups=_AUTO_FETCH_MAX_GROUPS, only_missing=True,
    )
    if not groups or _metadata_refresh_guard.locked():
        return
    _metadata_auto_last[username] = now
    task_registry.spawn(
        _auto_metadata_fetch(groups, dict(meta_cfg)),
        name=f"metadata-auto:{username}",
    )


@router.get("/tmdb/status")
@router.get("/metadata/status")
async def metadata_status() -> dict:
    """Metadata provider/key state + match counts (raw key never returned)."""
    cfg = _load_config()
    return metadata_service.status(_metadata_config(cfg))


@router.get("/metadata/test")
async def metadata_test() -> dict:
    """Check every configured source; keyless works without any API key."""
    cfg = _load_config()
    return await asyncio.to_thread(
        metadata_service.test_providers, _metadata_config(cfg),
    )


@router.get("/tmdb/test")
async def tmdb_test_legacy() -> dict:
    """Legacy TMDB-only test used by package 2.0.x (keeps its 400 semantics)."""
    cfg = _load_config()
    key = _metadata_config(cfg).get("api_key", "")
    if not key:
        raise HTTPException(
            status_code=400,
            detail="TMDB API key not configured — use a keyless source or add a key",
        )
    result = await asyncio.to_thread(
        metadata_service.test_providers, {**_metadata_config(cfg), "provider": "tmdb"},
    )
    return result


@router.post("/tmdb/refresh")
@router.post("/metadata/refresh")
async def metadata_refresh(payload: dict = Body(default={})) -> dict:
    """Fetch metadata for the library (batch) or one file (`path`).

    Uses the configured provider chain: TMDB when a key is present, otherwise
    Cinemeta and TVmaze. `force` re-queries cached entries (a provider switch or
    "Re-fetch all"); without it fresh entries are skipped. Artwork is cached
    locally; files are never renamed or moved.
    """
    cfg = _load_config()
    meta_cfg = _metadata_config(cfg)
    disabled = _metadata_disabled(cfg)
    if disabled:
        raise HTTPException(status_code=400, detail=disabled)
    single = payload.get("path") if isinstance(payload, dict) else None
    force = bool(payload.get("force")) if isinstance(payload, dict) else False

    async with _metadata_refresh_guard:
        try:
            if single:
                abs_path = _sanitize(single)
                result = await asyncio.to_thread(
                    metadata_service.refresh_single, abs_path, meta_cfg, force=force,
                )
            else:
                listing = await asyncio.to_thread(_walk_all, cfg["roots"], "")
                groups = metadata_service.build_groups(
                    listing.get("videos", []), only_missing=not force,
                )
                result = await asyncio.to_thread(
                    metadata_service.refresh, groups, meta_cfg, force=force,
                )
        except MetadataConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(result, dict):
        # Package 2.0.x read `matched`/`updated`; keep them working.
        result.setdefault("matched", result.get("titles_matched", 0))
        result.setdefault("updated", result.get("titles_matched", 0))
    return result


@router.get("/tmdb/image")
@router.get("/metadata/image")
async def metadata_image(file: str = Query(...)) -> FileResponse:
    """Locally cached artwork from any provider (legacy TMDB dir included)."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", file):
        raise HTTPException(status_code=400, detail="Invalid image name")
    path = metadata_service.image_path(file)
    if not path:
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(
        str(path),
        media_type=metadata_service.image_media_type(path),
        headers={"Cache-Control": "max-age=604800"},
    )
