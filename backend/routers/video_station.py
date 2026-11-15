"""EthOS NAS — Videos router (media library, thumbnails, streaming).

Library model:
- Configured root folders (any allowed location) define the video library.
  Saved to backend/data/video_station_config.json via GET/POST /config.
- Browsing is live (no index): GET /library walks a folder on demand;
  "All Videos" (empty path) does a bounded recursive walk over all roots.

REST:
- GET  /api/videos/config            — saved root folders + display/playback options
- POST /api/videos/config            — save root folders + display/playback options
- GET  /api/videos/status            — ffmpeg/ffprobe presence + QSV availability
- GET  /api/videos/browse?path=...   — subdirectories for the folder picker
- GET  /api/videos/library?path=&q=  — folders + videos under a path (or all)
- GET  /api/videos/stream?path=...   — Range-capable raw file streaming
- GET  /api/videos/transcode?path=&t=&a=&q=
  Live processing piped as fragmented MP4; `t` = start offset, `a` = audio track
  index (0-based among audio streams), `q` = quality preset. The server picks the
  lightest path that works:
    remux     — video copied losslessly (-c:v copy), audio → AAC only when needed.
                Real-time on any CPU; the common case for H.264 rips with AC3/EAC3/DTS.
    transcode — full re-encode (Intel QSV h264 when available, libx264 otherwise)
                + downscale to the preset's max height + AAC audio.
- GET  /api/videos/subtitles?path=.. — sibling .srt/.vtt files for a video
- GET  /api/videos/resume?path=...   — saved playback position (or null)
- POST /api/videos/resume            — save/clear playback position
- GET  /api/videos/thumbnail?path=.  — ffmpeg frame grab, disk-cached (404 w/o ffmpeg)
- GET  /api/videos/info?path=...     — ffprobe metadata, disk-cached
- GET  /api/videos/tmdb/status       — TMDB key configured? + match count
- POST /api/videos/tmdb/refresh      — fetch TMDB metadata (batch or one file),
                                       downloads poster/backdrop to local disk
- GET  /api/videos/tmdb/image?file=. — locally cached TMDB artwork
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

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

# Shared path sanitizer from the File Manager router (abspath/expanduser only —
# OS-level access control is the security boundary, same as gallery.py).
from backend.config import settings
from backend.routers.files import _sanitize
from backend.services import tmdb_service
from backend.services.video_metadata import parse_video_name

logger = logging.getLogger("ethos-nas.routers.video_station")

router = APIRouter()

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".flv",
              ".wmv", ".mpg", ".mpeg", ".ts", ".mts", ".3gp"}
SUBTITLE_EXTS = {".srt", ".vtt"}
# Directories that never contain user video — skip them entirely so dev trees
# (node_modules, venvs) don't slow scans or pollute the library.
NON_MEDIA_DIRS = {"node_modules", "venv", ".venv"}

ALLOWED_ROOTS = ["/home", "/mnt", "/media"]

# settings.DATA_DIR honors ETHOS_DATA_DIR so isolated test instances don't
# clobber production state (was hardcoded to backend/data/).
DATA_DIR = settings.DATA_DIR
CONFIG_FILE = DATA_DIR / "video_station_config.json"

from backend.services import json_store

_CONFIG_STORE = json_store.get(CONFIG_FILE, version=1)
RESUME_FILE = DATA_DIR / "video_station_resume.json"

_RESUME_STORE = json_store.get(RESUME_FILE)  # {username: position}
THUMB_DIR = DATA_DIR / "video_thumbs"
INFO_CACHE_FILE = DATA_DIR / "video_info_cache.json"

_INFO_STORE = json_store.get(INFO_CACHE_FILE)  # {path: info}
TMDB_DIR = DATA_DIR / "tmdb_images"
TMDB_MATCHES_FILE = DATA_DIR / "tmdb_matches.json"

_MATCHES_STORE = json_store.get(TMDB_MATCHES_FILE)  # {path: match}

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
    },
    # TMDB (Phase 6) — display-only metadata + locally cached artwork. Files
    # are never renamed or moved. api_key is masked in GET /config; an empty
    # value on POST keeps the previously stored key.
    "tmdb": {"api_key": "", "auto_fetch": True, "language": "pl-PL"},
}

QUALITY_PRESETS = {
    # Tuned for a 4-core Celeron-class CPU (Intel N150): libx264 veryfast does
    # ~8 fps at 1440p, so downscaling is what keeps live playback watchable.
    "fast":     {"preset": "ultrafast", "crf": 28, "max_height": 720},
    "balanced": {"preset": "veryfast",  "crf": 23, "max_height": 1080},
    "high":     {"preset": "veryfast",  "crf": 20, "max_height": 0},  # 0 = source res
}

AUDIO_BITRATES = {"96k", "128k", "160k", "192k", "256k"}

# Caps for the bounded "All Videos" recursive walk.
ALL_VIDEOS_MAX_DEPTH = 8
ALL_VIDEOS_MAX_FILES = 10000

FFMPEG_BIN = shutil.which("ffmpeg")
# Audio codecs browsers decode natively inside <video>. Anything else (AC3/EAC3/
# DTS/TrueHD — common in movie rips) must be transcoded to AAC.
BROWSER_AUDIO_OK = {"aac", "mp3", "opus"}
# Video codecs browsers reliably decode. HEVC/AV1/MPEG-4 etc. get re-encoded
# to H.264 so playback works everywhere.
BROWSER_VIDEO_OK = {"h264", "vp8", "vp9"}
# Containers the browser can demux directly in <video>.
STREAMABLE_EXTS = {".mp4", ".m4v", ".webm", ".mov"}


def _playback_mode(info: dict, abs_path: str) -> str:
    """How the browser should get this file.

    - 'direct':   raw /stream — video codec, audio codec and container are all
      natively playable (h264/vp8/vp9 + aac/mp3/opus in mp4/webm/mov).
    - 'remux':    video stream copied losslessly into fragmented MP4; audio is
      re-encoded to AAC only when the browser can't decode it (AC3/EAC3/DTS…).
      Real-time on any CPU — the common case for H.264 movie rips with Dolby
      Digital audio in MKV.
    - 'transcode': full video re-encode + AAC; used when the video codec itself
      is not browser-playable (HEVC/AV1/MPEG-2…).
    """
    if not FFMPEG_BIN:
        return "direct"  # nothing else we can do; the player surfaces the error
    ac = (info.get("audio_codec") or "").lower()
    vc = (info.get("video_codec") or "").lower()
    ext = os.path.splitext(abs_path)[1].lower()
    video_ok = (not vc) or vc in BROWSER_VIDEO_OK
    audio_ok = (not ac) or ac in BROWSER_AUDIO_OK
    if video_ok and audio_ok and ext in STREAMABLE_EXTS:
        return "direct"
    if video_ok:
        return "remux"
    return "transcode"

def _needs_transcode(info: dict, abs_path: str) -> bool:
    """Kept for API compatibility (Android app): True when the file must go
    through /transcode instead of raw /stream."""
    return _playback_mode(info, abs_path) != "direct"
FFPROBE_BIN = shutil.which("ffprobe")

_resume_lock = asyncio.Lock()


# ── Config persistence ────────────────────────────────────────────────────────


def _sanitize_tmdb(tmdb_cfg, prev=None) -> dict:
    """Validate the TMDB config section. An empty/missing api_key keeps the
    previously stored key so a settings save never wipes it by accident."""
    # Missing keys fall back to the previously stored values so a partial
    # patch (e.g. saving only the API key) never resets other options.
    out = {"api_key": (prev or {}).get("api_key", ""),
           "auto_fetch": bool((prev or {}).get("auto_fetch", True)),
           "language": (prev or {}).get("language") or DEFAULT_CONFIG["tmdb"]["language"]}
    if isinstance(tmdb_cfg, dict):
        k = tmdb_cfg.get("api_key")
        if isinstance(k, str) and k.strip():
            out["api_key"] = k.strip()[:64]
        af = tmdb_cfg.get("auto_fetch")
        if af is not None:
            out["auto_fetch"] = bool(af)
        lang = tmdb_cfg.get("language")
        # Conservative TMDB-style code (pl-PL, en-US, de…); an invalid or empty
        # value keeps the previous one — "no language" mode is unreachable.
        if isinstance(lang, str) and re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z]{2,4})?", lang.strip()):
            out["language"] = lang.strip()
    return out

def _mask_tmdb(tmdb_cfg: dict) -> dict:
    """Config-safe view of the TMDB section (raw key never leaves the server)."""
    key = (tmdb_cfg or {}).get("api_key", "")
    return {"api_key_set": bool(key),
            "api_key_masked": ("…" + key[-4:]) if key else "",
            "auto_fetch": (tmdb_cfg or {}).get("auto_fetch", True),
            "language": (tmdb_cfg or {}).get("language") or DEFAULT_CONFIG["tmdb"]["language"]}

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
            elif k in ("auto_subtitles", "autoplay_next"):
                base[k] = bool(v)
    return base

def _load_config() -> dict:
    data = _CONFIG_STORE.load()
    if not isinstance(data, dict) or not data:
        return json.loads(json.dumps(DEFAULT_CONFIG))
    roots = [r for r in (data.get("roots") or []) if isinstance(r, str) and _is_allowed(r)]
    return {"roots": roots, "options": _sanitize_options(data.get("options") or {}),
            "tmdb": _sanitize_tmdb(data.get("tmdb"))}


def _save_config(config: dict) -> None:
    _CONFIG_STORE.save(config)


def _is_allowed(abs_path: str) -> bool:
    return any(abs_path == r or abs_path.startswith(r + os.sep) for r in ALLOWED_ROOTS)

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
    if not _is_allowed(abs_path):
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

    _attach_tmdb(videos)
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

    _attach_tmdb(videos)
    return {"path": None, "parent": None, "folders": top_folders, "videos": videos}


# ── Resume positions ──────────────────────────────────────────────────────────


def _load_resume_map() -> dict:
    data = _RESUME_STORE.load()
    return data if isinstance(data, dict) else {}


def _save_resume_map(m: dict) -> None:
    _RESUME_STORE.save(m)


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
        # schema 2 added audio_tracks; older entries are treated as misses.
        if (hit and hit.get("schema") == 2 and hit.get("mtime") == st.st_mtime
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
        cache[key] = {"schema": 2, "mtime": st.st_mtime, "size": st.st_size,
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
    # Auto-seed with the current user's home on first run so the app is not empty.
    if not cfg["roots"]:
        home = os.path.expanduser("~")
        if _is_allowed(home):
            cfg["roots"] = [home]
    return {**cfg, "allowed_roots": ALLOWED_ROOTS, "tmdb": _mask_tmdb(cfg.get("tmdb") or {})}


@router.post("/config")
async def set_config(payload: dict = Body(...)) -> dict:
    roots_in = payload.get("roots") or []
    options_in = payload.get("options") or {}
    roots = [os.path.abspath(os.path.expanduser(r)) for r in roots_in if isinstance(r, str)]
    roots = [r for r in roots if _is_allowed(r) and os.path.isdir(r)]
    prev = _load_config()
    cfg = {"roots": roots, "options": _sanitize_options(options_in),
           "tmdb": _sanitize_tmdb(payload.get("tmdb"), prev.get("tmdb"))}
    _save_config(cfg)
    return {**cfg, "tmdb": _mask_tmdb(cfg["tmdb"])}

@router.get("/status")
async def status(refresh: bool = False) -> dict:
    """Media-engine capabilities for the settings UI (HW probe is cached).

    `?refresh=1` forces a fresh GPU probe — handy after installing a driver or
    fixing device permissions without bouncing the whole service.
    """
    global _hw_cache, _dri_node_cache
    if refresh:
        _hw_cache = None
        _dri_node_cache = None
    hw = await _hw_encoder()
    return {
        "ffmpeg": bool(FFMPEG_BIN),
        "ffprobe": bool(FFPROBE_BIN),
        "qsv": hw["encoder"] == "qsv",          # kept for older clients
        "hw": hw,
    }


@router.get("/browse")
async def browse(path: str | None = Query(default=None)) -> dict:
    """Subdirectory picker (restricted to allowed roots)."""
    base = os.path.abspath(os.path.expanduser(path)) if path else None
    if base is None:
        # Top level of every allowed root.
        def _roots() -> list[dict]:
            out = []
            for r in ALLOWED_ROOTS:
                if not os.path.isdir(r):
                    continue
                try:
                    entries = sorted((e.name for e in os.scandir(r)
                                      if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")),
                                     key=str.lower)
                except OSError:
                    continue
                out.append({"path": r, "name": os.path.basename(r), "folders": entries})
            return out

        return {"roots": await asyncio.to_thread(_roots)}

    if not _is_allowed(base):
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
) -> dict:
    """Folder listing (single level) or the bounded 'All Videos' walk."""
    if not path:
        cfg = _load_config()
        return await asyncio.to_thread(_walk_all, cfg["roots"], q)
    return await asyncio.to_thread(_list_dir, path, q)


@router.get("/home")
async def home(limit: int = Query(default=12, ge=1, le=50)) -> dict:
    """Jellyfin-style home rows computed from one bounded walk over all roots.

    - continue_watching: items with a live resume position (5 s < t < 97 %),
      most recently watched first; each carries resume_time/resume_duration.
    - recently_added: top N by file mtime.
    """
    cfg = _load_config()
    listing = await asyncio.to_thread(_walk_all, cfg["roots"], "")
    videos = listing.get("videos", [])
    resumes = _load_resume_map()

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

@router.get("/stream")
async def stream(path: str = Query(...)) -> FileResponse:
    """Range-capable video streaming (Starlette handles HTTP Range natively).

    Serves the raw file. Files the browser can't decode directly (AC3/EAC3/DTS
    audio, HEVC video, MKV containers) should be played via /transcode instead
    — see `info` which reports `needs_transcode`.
    """
    abs_path = _sanitize(path)
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


_hw_cache: dict | None = None
_dri_node_cache: str | None = None
_dri_reason_cache: str | None = None


def _dri_probe() -> tuple[str | None, str]:
    """(first usable /dev/dri/renderD* node, explanation when unusable).

    Permission denied is by far the common case — the node is root:render 0660
    and the service user is not in `render` — so the explanation names the exact
    commands to run instead of a generic "unavailable".
    """
    global _dri_node_cache, _dri_reason_cache
    if _dri_node_cache is None:
        try:
            nodes = sorted(f"/dev/dri/{n}" for n in os.listdir("/dev/dri")
                           if n.startswith("renderD"))
        except OSError:
            nodes = []
        _dri_node_cache, _dri_reason_cache = "", ""
        if not nodes:
            _dri_reason_cache = "no /dev/dri/renderD* node — no GPU is exposed to this host"
        else:
            for p in nodes:
                if os.access(p, os.R_OK | os.W_OK):
                    _dri_node_cache = p
                    break
            if not _dri_node_cache:
                import getpass
                try:
                    user = getpass.getuser()
                except Exception:
                    user = "ethos-nas"
                _dri_reason_cache = (
                    f"{nodes[0]} exists but user '{user}' cannot open it (root:render 0660) — run: "
                    f"sudo usermod -aG render,video {user} && sudo systemctl restart ethos-nas")
    return (_dri_node_cache or None), (_dri_reason_cache or "")


def _dri_render_node() -> str | None:
    """First /dev/dri/renderD* node this process can actually open, or None."""
    return _dri_probe()[0]


async def _probe_encoder(args: list[str]) -> bool:
    """Micro-encode 0.4 s of testsrc through the given encoder arguments."""
    if not FFMPEG_BIN:
        return False
    cmd = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "lavfi", "-i", "testsrc2=duration=0.4:size=640x360:rate=30",
           *args, "-f", "null", "-"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return (await asyncio.wait_for(proc.wait(), timeout=25)) == 0
    except Exception as exc:
        logger.warning("HW encoder probe failed (%s): %s", " ".join(args[:2]), exc)
        return False


async def _hw_encoder() -> dict:
    """Detect the best hardware H.264 encoder once per server run.

    Cascade: Intel QuickSync (h264_qsv / oneVPL) → VA-API (h264_vaapi on the
    iHD driver) → None (software libx264). Each tier is validated with a real
    micro-encode, so a build with the encoder compiled in but a missing or
    permission-denied render node is never assumed to work.
    """
    global _hw_cache
    if _hw_cache is not None:
        return _hw_cache
    res: dict = {"encoder": None, "device": None, "reason": ""}
    dev, why = _dri_probe()
    if not dev:
        res["reason"] = why
        _hw_cache = res
        logger.info("Videos media engine: software libx264 (%s)", res["reason"])
        return res
    if await _probe_encoder(["-c:v", "h264_qsv", "-b:v", "2M", "-pix_fmt", "nv12"]):
        res.update(encoder="qsv", device=dev, reason=f"Intel QuickSync via {dev}")
    elif await _probe_encoder(["-vaapi_device", dev, "-vf", "format=nv12,hwupload",
                               "-c:v", "h264_vaapi", "-b:v", "2M"]):
        res.update(encoder="vaapi", device=dev, reason=f"VA-API H.264 encode via {dev}")
    else:
        res["reason"] = f"no working hardware H.264 encoder on {dev} — using libx264"
    _hw_cache = res
    logger.info("Videos media engine: %s", res["reason"] or "software libx264")
    return res


def _transcode_bitrate(width: int, height: int) -> str:
    px = (width or 0) * (height or 0)
    if px >= 1920 * 1080:
        return "6M"
    if px >= 1280 * 720:
        return "4M"
    if px >= 854 * 480:
        return "2.5M"
    return "1.5M"


def _build_transcode_cmd(abs_path: str, info: dict, start: float, hw: str | None,
                         mode: str, track_idx: int, quality: str,
                         audio_bitrate: str) -> list[str]:
    w = info.get("width") or 0
    h = info.get("height") or 0
    preset = QUALITY_PRESETS.get(quality) or QUALITY_PRESETS["balanced"]

    # Filters run in order. Hardware encoders need frames on the GPU, so the
    # CPU-side downscale happens first and hwupload is always the last step.
    vf: list[str] = []
    if mode == "transcode" and preset["max_height"] and h > preset["max_height"]:
        # Downscale before encoding — on a Celeron-class CPU this is the
        # difference between watchable and unwatchable for 4K/8K sources.
        vf.append(f"scale=-2:{preset['max_height']}")

    pre: list[str] = []   # global options (must precede -i)
    if mode == "remux":
        v = ["-c:v", "copy"]
    elif hw == "qsv":
        # Intel QuickSync: hardware H.264 encode, no B-frames for low latency,
        # short GOP so fragments stay small in the live pipe.
        v = ["-c:v", "h264_qsv", "-preset", "veryfast",
             "-b:v", _transcode_bitrate(w, h), "-g", "30", "-bf", "0",
             "-pix_fmt", "yuv420p"]
    elif hw == "vaapi":
        # VA-API (iHD driver, low-power EncSliceLP on Alder Lake-N / N150).
        dev = _dri_render_node()
        pre = ["-vaapi_device", dev] if dev else []
        vf += ["format=nv12", "hwupload"]
        v = ["-c:v", "h264_vaapi", "-b:v", _transcode_bitrate(w, h),
             "-g", "30", "-bf", "0"]
    else:
        v = ["-c:v", "libx264", "-preset", preset["preset"], "-crf", str(preset["crf"]),
             # Short GOP: with the default 250-frame GOP a slow CPU would take
             # ~a minute to emit the first fragment of the live pipe.
             "-g", "30",
             "-maxrate", _transcode_bitrate(w, h), "-bufsize", "16M",
             "-pix_fmt", "yuv420p"]
    vf_args = ["-vf", ",".join(vf)] if vf else []

    # Audio: pick the requested track (clamped); copy it when it's already AAC,
    # otherwise encode to AAC so every browser can play it with sound.
    tracks = info.get("audio_tracks") or []
    idx = min(max(0, int(track_idx)), max(0, len(tracks) - 1)) if tracks else 0
    acodec = (tracks[idx].get("codec") or "") if idx < len(tracks) else ""
    if mode == "remux" and acodec == "aac":
        a = ["-map", f"0:a:{idx}?", "-c:a", "copy"]
    else:
        a = ["-map", f"0:a:{idx}?", "-c:a", "aac", "-b:a", audio_bitrate]

    return [FFMPEG_BIN or "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", settings.FFMPEG_LOGLEVEL,
            *pre, "-ss", f"{start:.3f}", "-i", abs_path,
            "-map", "0:v:0", *v, *vf_args, *a,
            # Fragmented MP4: empty moov up front + one fragment per keyframe so
            # the browser starts playing immediately from a non-seekable pipe.
            "-f", "mp4", "-movflags", "+frag_keyframe+empty_moov", "pipe:1"]


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
    abs_path = _sanitize(path)
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
    audio_bitrate = _load_config()["options"]["audio_bitrate"]
    hw = (await _hw_encoder())["encoder"] if mode == "transcode" else None
    hw_tag = hw or "none"
    cmd = _build_transcode_cmd(abs_path, info, start, hw, mode, a, quality,
                               audio_bitrate)
    logger.info("Transcode start | mode=%s hw=%s track=%d q=%s input=%s t=%.2f\ncmd: %s",
                mode, hw_tag, a, quality, abs_path, start, shlex.join(cmd))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception as exc:
        logger.error("Transcode spawn failed | input=%s: %s", abs_path, exc)
        raise HTTPException(status_code=502, detail=f"Could not start transcode: {exc}") from exc

    async def _stream():
        if proc.stdout is None or proc.stderr is None:   # unreachable — both are PIPEd
            return
        err_tail = deque(maxlen=64)  # 16 KiB chunks → ≤1 MiB of stderr kept
        drain = asyncio.create_task(_drain_stderr(proc.stderr, err_tail))
        t0 = time.monotonic()
        try:
            while True:
                chunk = await proc.stdout.read(1 << 16)
                if not chunk:
                    break
                yield chunk
            await proc.wait()
        finally:
            drain.cancel()
            if proc.returncode is None:  # client went away → kill ffmpeg
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await proc.wait()
            tail = b"".join(err_tail).decode("utf-8", "replace")[-2000:]
            elapsed = time.monotonic() - t0
            if proc.returncode == 0:
                logger.info("Transcode done | mode=%s hw=%s input=%s elapsed=%.1fs",
                            mode, hw_tag, abs_path, elapsed)
            elif proc.returncode is None or proc.returncode < 0:
                # Killed by us — the client disconnected (seek/close). Normal.
                logger.info("Transcode stopped (client gone) | mode=%s hw=%s input=%s "
                            "elapsed=%.1fs", mode, hw_tag, abs_path, elapsed)
            else:
                logger.error(
                    "Transcode FAILED | mode=%s hw=%s input=%s rc=%s elapsed=%.1f\n"
                    "--- stderr ---\n%s", mode, hw_tag, abs_path, proc.returncode,
                    elapsed, tail or "(empty)")

    return StreamingResponse(_stream(), media_type="video/mp4",
                             headers={"Cache-Control": "no-store"})


@router.get("/subtitles")
async def subtitles(path: str = Query(...)) -> dict:
    """Sibling subtitle files (.srt/.vtt) next to the video."""
    abs_path = _sanitize(path)
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
    abs_path = _sanitize(path)
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
    abs_path = _sanitize(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")
    result = await _probe(abs_path)
    with contextlib.suppress(OSError):
        result["size"] = os.stat(abs_path).st_size
    mode = _playback_mode(result, abs_path)
    result["playback_mode"] = mode
    result["needs_transcode"] = mode != "direct"
    return {"info": result}

# ── TMDB metadata + local artwork (Phase 6) ───────────────────────────────────
_tmdb_cache_lock = asyncio.Lock()
_tmdb_refresh_guard = asyncio.Lock()   # one refresh run at a time

TMDB_MATCH_FIELDS = ("kind", "tmdb_id", "title", "year", "rating", "overview",
                     "poster_file", "backdrop_file", "genres", "cast", "runtime")

_matches_memo: tuple[int, int, dict] | None = None    # (mtime_ns, size, data)


def _load_matches() -> dict:
    """TMDB match cache (path → match), memoized on the file's mtime + size.

    Every /library, /home and /browse request attaches metadata via this, so
    re-parsing a multi-MB JSON on each call was pure overhead. Saves invalidate
    the memo, and an mtime/size change (e.g. a hand edit) is detected too.
    """
    global _matches_memo
    try:
        st = os.stat(TMDB_MATCHES_FILE)
    except OSError:
        return {}
    if _matches_memo and _matches_memo[0] == st.st_mtime_ns and _matches_memo[1] == st.st_size:
        return _matches_memo[2]
    try:
        with open(TMDB_MATCHES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    data = data if isinstance(data, dict) else {}
    _matches_memo = (st.st_mtime_ns, st.st_size, data)
    return data

def _save_matches(m: dict) -> None:
    global _matches_memo
    if len(m) > 4000:   # keep the cache bounded; drop oldest matches first
        items = sorted(m.items(), key=lambda kv: (kv[1].get("matched_at") or 0))
        for k, _ in items[:len(m) - 3500]:
            m.pop(k, None)
    _MATCHES_STORE.save(m)
    _matches_memo = None   # force a re-read (mtime granularity is coarse)

def _attach_tmdb(videos: list[dict]) -> None:
    """Attach cached TMDB matches to library items (cache read only — no network)."""
    m = _load_matches()
    if not m:
        return
    for v in videos:
        hit = m.get(v["path"])
        if hit:
            v["tmdb"] = {k: hit[k] for k in TMDB_MATCH_FIELDS if k in hit}

def _tmdb_query_for(item: dict):
    """(kind, query, year, imdb_id) to search TMDB with, or None when not worth it.

    Movies match on parsed title (+ year); episodes on the show name — but a
    folder-derived show name this generic ("TV", "Series"…) is skipped so we
    don't slap some random poster on an unparseable branch of the library.
    An IMDb id embedded in the filename (tt\\d{7}) enables exact /find matching.
    """
    imdb_id = tmdb_service.extract_imdb_id(os.path.basename(item.get("path", "")))
    if item.get("kind") == "episode":
        show = (item.get("show") or "").strip()
        if len(show) < 3 or show.lower() in tmdb_service.GENERIC_SHOW_NAMES:
            return None
        return ("tv", show, None, imdb_id)
    title = (item.get("title") or "").strip()
    if not title:
        return None
    return ("movie", title, item.get("year"), imdb_id)

def _sweep_tmdb_images(matches: dict) -> None:
    """Delete local artwork no longer referenced by any cached match (e.g. the
    old w780/w342 files after an original-size re-download). Best-effort."""
    try:
        if not TMDB_DIR.is_dir():
            return
        referenced = set()
        for hit in matches.values():
            for k in ("poster_file", "backdrop_file"):
                f = (hit or {}).get(k)
                if f:
                    referenced.add(f)
        removed = 0
        for p in TMDB_DIR.iterdir():
            if p.is_file() and not p.name.endswith(".tmp") and p.name not in referenced:
                p.unlink(missing_ok=True)
                removed += 1
        if removed:
            logger.info("TMDB image sweep: removed %d unreferenced file(s)", removed)
    except OSError as exc:
        logger.warning("TMDB image sweep failed: %s", exc)

def _tmdb_refresh_sync(api_key: str, single_path: str | None = None,
                       language: str = "pl-PL") -> dict:
    """Query TMDB for unmatched titles and persist matches + local artwork.

    `single_path` limits the run to one file (per-card refresh). Batch runs do
    one bounded walk over all roots and group files by search query so a whole
    season costs exactly one API call; matches are saved incrementally so a
    long batch survives a restart. Cached entries without the detail extras
    (`genres`) are treated as stale and re-fetched, which upgrades pre-6b
    caches to localized metadata + original-size artwork in place. Never
    raises — network errors are counted.
    """
    stats: dict = {"titles_checked": 0, "titles_matched": 0, "files_matched": 0, "errors": 0}
    matches = _load_matches()

    if single_path:
        name = os.path.basename(single_path)
        folder = os.path.basename(os.path.dirname(single_path)) or None
        item = {"path": single_path, **parse_video_name(name, folder=folder)}
        q = _tmdb_query_for(item)
        stats["titles_checked"] = 1 if q else 0
        if q:
            try:
                m = tmdb_service.fetch_match(api_key, q[0], q[1], q[2], TMDB_DIR,
                                             language=language, imdb_id=q[3])
            except tmdb_service.TmdbError as exc:
                logger.warning("TMDB refresh failed for %s: %s", single_path, exc)
                stats["errors"] += 1
            except Exception as exc:   # never let one title 500 the request
                logger.warning("TMDB refresh crashed for %s: %s", single_path, exc)
                stats["errors"] += 1
            else:
                if m:
                    matches[single_path] = {**m, "matched_at": time.time()}
                    _save_matches(matches)
                    stats["titles_matched"] += 1
                    stats["files_matched"] += 1
        return {**stats, "match": matches.get(single_path)}

    cfg = _load_config()
    listing = _walk_all(cfg["roots"], "")
    groups: dict[tuple, list] = {}
    for v in listing.get("videos", []):
        hit = matches.get(v["path"])
        if hit and "genres" in hit:
            continue   # fresh entry — already localized + detailed
        q = _tmdb_query_for(v)
        if not q:
            continue
        key = (q[0], q[1].lower(), q[2], q[3])
        groups.setdefault(key, []).append((v["path"], q))

    for _key, entries in groups.items():
        kind, query, year, imdb_id = entries[0][1]
        stats["titles_checked"] += 1
        try:
            m = tmdb_service.fetch_match(api_key, kind, query, year, TMDB_DIR,
                                         language=language, imdb_id=imdb_id)
        except tmdb_service.TmdbError as exc:
            stats["errors"] += 1
            if "401" in str(exc) or "403" in str(exc):
                # Invalid/expired key — every remaining call fails the same way.
                # Abort instead of logging one warning per title (hundreds on a
                # large library, see app_events 2026-10-01).
                logger.warning("TMDB batch refresh aborted: API key rejected by TMDB (%s) — fix it in Settings → Metadata", exc)
                stats["aborted"] = "API key rejected by TMDB (HTTP 401/403) — check Settings → Metadata"
                return stats
            logger.warning("TMDB batch refresh failed for %s/%r: %s", kind, query, exc)
            continue
        except Exception as exc:   # never let one title abort the whole batch
            stats["errors"] += 1
            logger.warning("TMDB batch refresh crashed for %s/%r: %s", kind, query, exc)
            continue
        if not m:
            continue   # no match — stays filename-only display
        m = {**m, "matched_at": time.time()}
        for path, _q in entries:
            matches[path] = m
            stats["files_matched"] += 1
        stats["titles_matched"] += 1
        try:
            _save_matches(matches)
        except Exception as exc:
            logger.warning("Could not persist TMDB match cache: %s", exc)
    _sweep_tmdb_images(matches)
    return stats

@router.get("/tmdb/status")
async def tmdb_status() -> dict:
    """TMDB configuration state for the settings UI (key is masked)."""
    cfg = _load_config()
    return {**_mask_tmdb(cfg.get("tmdb") or {}), "matched_count": len(_load_matches())}

@router.get("/tmdb/test")
async def tmdb_test() -> dict:
    """Validate the stored API key against TMDB's /configuration endpoint."""
    cfg = _load_config()
    key = (cfg.get("tmdb") or {}).get("api_key", "")
    if not key:
        raise HTTPException(status_code=400, detail="TMDB API key not configured — set it in Settings → Metadata")
    return await asyncio.to_thread(tmdb_service.test_connection, key)

@router.post("/tmdb/refresh")
async def tmdb_refresh(payload: dict = Body(default={})) -> dict:
    """Fetch TMDB metadata for the library (batch) or one file (`path`).

    `force` re-queries a single file even when it already has a cached match.
    Artwork is downloaded to disk per match; matches persist in
    tmdb_matches.json keyed by absolute path. Display-only — files are never
    renamed or moved.
    """
    cfg = _load_config()
    key = (cfg.get("tmdb") or {}).get("api_key", "")
    if not key:
        raise HTTPException(status_code=400, detail="TMDB API key not configured — set it in Settings → Metadata")
    single = payload.get("path") if isinstance(payload, dict) else None
    force = bool(payload.get("force")) if isinstance(payload, dict) else False

    async with _tmdb_refresh_guard:
        if single and force:
            m = _load_matches()
            abs_path = _sanitize(single)
            m.pop(abs_path, None)
            _save_matches(m)
        lang = (cfg.get("tmdb") or {}).get("language") or DEFAULT_CONFIG["tmdb"]["language"]
        result = await asyncio.to_thread(_tmdb_refresh_sync, key,
                                        _sanitize(single) if single else None, lang)
    return result

@router.get("/tmdb/image")
async def tmdb_image(file: str = Query(...)) -> FileResponse:
    """Locally cached TMDB artwork (poster/backdrop downloaded at refresh)."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", file):
        raise HTTPException(status_code=400, detail="Invalid image name")
    p = TMDB_DIR / file
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(str(p), media_type="image/jpeg", headers={"Cache-Control": "max-age=604800"})
