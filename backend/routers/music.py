"""EthOS NAS — Music router (yt-dlp based search / streaming / download).

Backs the "Music" web app and the EthOS Music Android app. YouTube blocks
unofficial clients on phone IPs, so all yt-dlp work happens here on the NAS:

- ``GET  /api/music/search?q=``      — flat search (ytsearchN:) → track list
- ``GET  /api/music/resolve?video_id=|url=`` — metadata + direct stream URL
- ``GET  /api/music/stream?video_id=`` — **proxied** audio with HTTP Range
  support. YouTube stream URLs are bound to the resolving IP, so clients
  (phones, browsers) must always consume audio through this endpoint.
- ``POST /api/music/download``       — download bestaudio → m4a (ffmpeg) into
  the music folder; sidecar ``<file>.meta.json`` stores source metadata.
- ``GET  /api/music/library``        — list downloaded files + metadata
- ``DELETE /api/music/library/{name}`` — remove a file from the library

yt-dlp is blocking → every call runs in ``asyncio.to_thread``. Resolved stream
URLs are cached in memory (TTL) because they expire and re-resolving costs a
round-trip to YouTube per track start.
"""

import asyncio
import copy
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from backend.config import settings
from backend.services import (
    music_search, package_client, path_scope, sharing_resources, sharing_service, user_data,
)

logger = logging.getLogger("ethos-nas.routers.music")

router = APIRouter(dependencies=[Depends(path_scope.scope_request)])

DATA_DIR = settings.DATA_DIR

DEFAULT_CONFIG = {
    # Where downloaded tracks are stored (NAS-side library)
    "download_dir": str(Path.home() / "Music" / "Ethos"),
}


def _config_store():
    """Per-user music settings (each account has its own download dir)."""
    return user_data.user_store("music_config.json")

# The yt-dlp engine lives in the `ethos-music-engine` package (its own venv,
# installed from Packages). The router only talks to it over loopback.
MUSIC_ENGINE = "ethos-music-engine"
ENGINE_MISSING = (
    "Silnik muzyczny nie jest zainstalowany — zainstaluj pakiet "
    "\"Music Engine\" w Packages."
)


def _engine_failure(exc: Exception):
    """Map a package-client failure to the HTTP error the app expects."""
    if isinstance(exc, package_client.PackageUnavailable):
        return HTTPException(status_code=503, detail=ENGINE_MISSING)
    return HTTPException(status_code=502, detail=f"Silnik muzyczny: {exc}")


# Resolved stream URLs expire on YouTube's side; keep them short-lived.
STREAM_CACHE_TTL_SEC = 30 * 60
_stream_cache: Dict[str, Tuple[str, str, float]] = {}  # video_id -> (url, content_type, expires_at)


def _load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    data = _config_store().load()
    if isinstance(data, dict):
        cfg.update(data)
    return cfg


def _save_config(cfg: dict) -> None:
    _config_store().save(cfg)


def _music_dir() -> Path:
    d = Path(_load_config()["download_dir"]).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


# Library listing is reused by /library, /search and /search/suggest; a short
# cache keeps per-keystroke suggestions from scanning the directory each time.
LIBRARY_CACHE_TTL = 20.0
_library_cache: Optional[Tuple[float, List[dict]]] = None


def _library_items() -> List[dict]:
    """Downloaded tracks (metadata + orphan audio), newest first."""
    music_dir = _music_dir()
    items: List[dict] = []
    for meta_file in sorted(music_dir.glob("*.meta.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            items.append(_file_info(meta_file))
        except OSError:
            continue
    # Orphan audio files (no metadata) — still playable
    seen = {it["name"] for it in items}
    for f in sorted(music_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.is_file() and f.suffix.lower() in (".m4a", ".mp3", ".opus", ".webm", ".ogg") and f.name not in seen:
            items.append({
                "name": f.name,
                "title": f.stem.replace(" - ", " — "),
                "artist": "",
                "thumbnail": "",
                "duration": 0,
                "video_id": "",
                "size": int(f.stat().st_size),
                "modified": f.stat().st_mtime,
                "url": f"/api/music/library-file?name={urllib.parse.quote(f.name)}",
            })
    return items


def _library_items_cached() -> List[dict]:
    global _library_cache
    now = time.monotonic()
    if _library_cache and now - _library_cache[0] < LIBRARY_CACHE_TTL:
        return _library_cache[1]
    items = _library_items()
    _library_cache = (now, items)
    return items


# ── yt-dlp helpers (blocking — always call via asyncio.to_thread) ─────────────

# ── Engine calls (blocking — always call via asyncio.to_thread) ───────────────

def _search_sync(query: str, limit: int) -> List[dict]:
    """Flat search — delegated to the Music engine package (already flat)."""
    try:
        payload = package_client.post_json(
            MUSIC_ENGINE, "/search", {"query": query, "limit": limit}, timeout=60,
        )
    except (package_client.PackageUnavailable, package_client.PackageCallError) as exc:
        raise _engine_failure(exc) from exc
    items = payload.get("items") if isinstance(payload, dict) else None
    return [item for item in items or [] if isinstance(item, dict)]


# ── Search layer (cache, ranking, unified results) ───────────────────────────
# yt-dlp costs seconds per call, so identical queries are cached briefly and
# concurrent identical queries share a single call (single-flight per key).
# Ranking itself (`services/music_search.py`) is pure Python and cheap.

SEARCH_CACHE_TTL = 600.0     # seconds
SEARCH_CACHE_MAX = 200       # distinct (query, fetch) pairs
_search_cache: "OrderedDict[Tuple[str, int], Tuple[float, List[dict]]]" = OrderedDict()
_search_locks: Dict[Tuple[str, int], asyncio.Lock] = {}


async def _search_cached(query: str, fetch: int) -> Tuple[List[dict], bool]:
    """yt-dlp results for ``query`` (cached). Returns ``(items, was_cached)``."""
    fetch = max(1, min(int(fetch), 100))
    key = (music_search.fold(query), fetch)
    now = time.monotonic()

    hit = _search_cache.get(key)
    if hit and now - hit[0] < SEARCH_CACHE_TTL:
        _search_cache.move_to_end(key)
        return [dict(item) for item in hit[1]], True

    lock = _search_locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _search_cache.get(key)
        if hit and time.monotonic() - hit[0] < SEARCH_CACHE_TTL:
            return [dict(item) for item in hit[1]], True
        items = await asyncio.to_thread(_search_sync, query, fetch)
        _search_cache[key] = (time.monotonic(), items)
        _search_cache.move_to_end(key)
        while len(_search_cache) > SEARCH_CACHE_MAX:
            _search_cache.popitem(last=False)
        _search_locks.pop(key, None)
        return [dict(item) for item in items], False


async def _search_tracks(query: str, limit: int, offset: int) -> dict:
    """Ranked YouTube results window + pagination info."""
    fetch = min(100, max(offset + limit + 10, limit))
    variants = music_search.query_variants(query)
    raw, cached = await _search_cached(variants[0], fetch)
    ranked = music_search.rank_tracks(variants[0], raw)

    # Diacritics-free fallback: “zolw” can still match “Żółw” when YouTube's own
    # relevance for the literal query is poor.
    if len(ranked) < max(5, limit // 3) and len(variants) > 1:
        extra, extra_cached = await _search_cached(variants[1], fetch)
        cached = cached and extra_cached
        ranked = music_search.rank_tracks(variants[0], raw + extra)

    window = ranked[offset:offset + limit]
    return {
        "items": window,
        "total": len(ranked),
        "has_more": offset + limit < len(ranked),
        "cached": cached,
    }


def _search_podcasts(query: str, limit: int) -> List[dict]:
    catalog = _load_podcast_catalog()
    return music_search.search_catalog(
        catalog["podcasts"], query, name_keys=("name",), field_weights=_PODCAST_FIELD_WEIGHTS, limit=limit
    )


def _search_radio(query: str, limit: int) -> List[dict]:
    catalog = _load_radio_catalog()
    return music_search.search_catalog(
        catalog["stations"], query, name_keys=("name",), field_weights=_RADIO_FIELD_WEIGHTS, limit=limit
    )


def _search_library(query: str, limit: int) -> List[dict]:
    items = _library_items_cached()
    return music_search.search_catalog(
        items, query, name_keys=("title",), field_weights={"title": 20.0, "artist": 5.0}, limit=limit
    )


def _search_playlists(request: Request, query: str, limit: int) -> List[dict]:
    """Playlist names + their track titles (top 20 tracks per playlist)."""
    _, state = _user_state(request)
    items = []
    for pl in state.get("playlists") or []:
        if not isinstance(pl, dict):
            continue
        tracks = [t for t in (pl.get("tracks") or []) if isinstance(t, dict)]
        items.append({
            "id": str(pl.get("id") or ""),
            "name": str(pl.get("name") or "Playlista"),
            "tracks_count": len(tracks),
            "tracks_text": " ".join(str(t.get("title") or "") for t in tracks[:20]),
        })
    return music_search.search_catalog(
        items, query, name_keys=("name",), field_weights={"name": 20.0, "tracks_text": 3.0}, limit=limit
    )


async def _search_all(query: str, limit: int, offset: int, request: Request) -> dict:
    """One round-trip for the client: tracks + podcasts + radio + library + playlists.

    Catalogs/library/playlists are *optional* sections: a broken or unreadable
    catalog file must never fail the whole search (that produced HTTP 500 and
    “Brak wyników” in the app even though YouTube tracks were available).
    """
    async def optional_section(name: str, func, *args) -> List[dict]:
        try:
            return await asyncio.to_thread(func, *args)
        except Exception as exc:  # HTTPException 500 from a broken catalog included
            logger.warning("music search: %s section unavailable: %s", name, exc)
            return []

    tracks_task = asyncio.create_task(_search_tracks(query, limit, offset))
    podcasts_task = asyncio.create_task(optional_section("podcasts", _search_podcasts, query, 8))
    radio_task = asyncio.create_task(optional_section("radio", _search_radio, query, 8))
    library_task = asyncio.create_task(optional_section("library", _search_library, query, 8))
    tracks, podcasts, stations, library = await asyncio.gather(
        tracks_task, podcasts_task, radio_task, library_task
    )
    playlists = await optional_section("playlists", _search_playlists, request, query, 8)

    def section(items: List[dict]) -> dict:
        return {"items": items, "total": len(items)}

    return {
        "query": query,
        "tracks": tracks,
        "podcasts": section(podcasts),
        "radio": section(stations),
        "library": section(library),
        "playlists": section(playlists),
        "cached": tracks.get("cached", False),
    }


@router.get("/search/suggest")
async def search_suggest(
    request: Request,
    q: str = Query(..., min_length=1, max_length=120),
    limit: int = Query(8, ge=1, le=20),
):
    """Instant suggestions from local catalogs (no yt-dlp, safe to call per keystroke)."""
    query = q.strip()
    podcasts, stations, library = await asyncio.gather(
        asyncio.to_thread(_search_podcasts, query, limit),
        asyncio.to_thread(_search_radio, query, limit),
        asyncio.to_thread(_search_library, query, limit),
    )
    playlists = _search_playlists(request, query, limit)

    buckets: List[List[dict]] = [
        [{
            "type": "podcast", "id": p["id"], "title": p["name"],
            "subtitle": p.get("author") or p.get("category") or "Podcast",
            "image": p.get("cover_url") or "", "rss_url": p["rss_url"],
        } for p in podcasts],
        [{
            "type": "radio", "id": s["id"], "title": s["name"],
            "subtitle": s.get("category") or "Radio",
            "image": s.get("logo") or "", "stream_url": s["stream_url"],
        } for s in stations],
        [{
            "type": "playlist", "id": pl["id"], "title": pl["name"],
            "subtitle": f"{pl['tracks_count']} utworów", "image": "",
        } for pl in playlists],
        [{
            "type": "library", "id": t.get("video_id") or t.get("name"),
            "title": t.get("title") or t.get("name"),
            "subtitle": t.get("artist") or "Biblioteka",
            "image": t.get("thumbnail") or "", "file_url": t.get("url") or "",
        } for t in library],
    ]

    # Round-robin so one source cannot crowd out the others.
    items: List[dict] = []
    while len(items) < limit and any(buckets):
        for bucket in buckets:
            if bucket and len(items) < limit:
                items.append(bucket.pop(0))
    return {"query": query, "items": items}


@router.get("/search")
async def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=500),
    type: str = Query(default="track", pattern="^(track|all)$"),
):
    """Flat YouTube search (yt-dlp ``ytsearchN:``) with local ranking.

    ``type=all`` additionally matches the podcast catalog, radio catalog, the
    downloaded library and the user's playlists — one request for the whole
    search screen. ``type=track`` (default) keeps the original response shape.
    """
    query = q.strip()
    try:
        if type == "all":
            return await _search_all(query, limit, offset, request)
        tracks = await _search_tracks(query, limit, offset)
    except HTTPException:
        raise
    except Exception as exc:  # yt_dlp.utils.DownloadError & friends
        logger.warning("music search failed for %r: %s", query, exc)
        raise HTTPException(status_code=502, detail=f"Wyszukiwanie nie powiodło się: {exc}") from exc
    return {
        "query": query,
        "results": tracks["items"],
        "total": tracks["total"],
        "has_more": tracks["has_more"],
        "offset": offset,
        "cached": tracks["cached"],
    }


# Stream format for the /stream proxy: prefer AAC (m4a) because it plays in
# every browser — Chrome, Firefox *and* Safari. Plain `bestaudio` picks
# webm/Opus, which Safari cannot decode at all.
STREAM_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"

# YouTube rotates which player clients are allowed per video/IP: some videos
# refuse the default client ("This video is not available") while serving the
# same request from another one (yt-dlp issue #11528). The engine package owns
# the client fallback list; the core only chooses the stream format.

# Google's CDN closes long-lived connections; if the upstream ends before we
# have delivered the whole file, re-open with a Range from where we stopped.
MAX_RECONNECTS = 5


def _resolve_sync(video_id: str, fmt: Optional[str] = None) -> dict:
    """Resolve one video → metadata + direct (IP-bound) stream URL (engine package)."""
    try:
        info = package_client.post_json(
            MUSIC_ENGINE, "/resolve", {"video_id": video_id, "fmt": fmt or ""}, timeout=60,
        )
    except (package_client.PackageUnavailable, package_client.PackageCallError) as exc:
        raise _engine_failure(exc) from exc
    if not isinstance(info, dict):
        raise HTTPException(status_code=502, detail="Silnik muzyczny zwrócił niepoprawne dane")
    return {
        "id": info.get("id") or video_id,
        "title": (info.get("title") or "").strip(),
        "artist": (info.get("artist") or "").strip(),
        "duration": int(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
        "url": info.get("url") or "",
        "ext": info.get("ext") or "",
    }


def _resolve_stream(video_id: str) -> Tuple[str, str]:
    """Cached stream URL + content type for the proxy endpoint."""
    now = time.time()
    hit = _stream_cache.get(video_id)
    if hit and hit[2] > now:
        return hit[0], hit[1]

    info = _resolve_sync(video_id, fmt=STREAM_FORMAT)
    url = info["url"]
    if not url:
        raise HTTPException(status_code=502, detail="YouTube nie zwróciła strumienia dla tego utworu")
    # ext=mp4: android client serves combined A/V MP4 (AAC audio) — still fine
    ctype = f"audio/{'mp4' if info['ext'] in ('m4a', 'aac', 'mp4') else 'webm'}"
    _stream_cache[video_id] = (url, ctype, now + STREAM_CACHE_TTL_SEC)
    return url, ctype


def _clean_stem(title: str, artist: str) -> str:
    """'Artist — Title' filename without duplicating the artist when it's in the title."""
    t = re.sub(r'[\\/:*?"<>|]+', " ", title or "").strip()
    a = re.sub(r'[\\/:*?"<>|]+', " ", artist or "").strip()
    if a and t:
        idx = t.lower().find(a.lower())
        if idx >= 0:  # strip the first occurrence of the artist from the title
            stripped = (t[:idx] + t[idx + len(a):]).strip(" -–—|·")
            if stripped:  # don't end up with an empty title
                t = stripped
    stem = f"{a} - {t}" if a else t
    return re.sub(r"\s{2,}", " ", stem).strip()[:150]


def _download_sync(video_id: str) -> dict:
    """Download bestaudio → m4a into the music dir (engine package does the fetch)."""
    music_dir = _music_dir()
    resolved = _resolve_sync(video_id)
    stem = _clean_stem(resolved["title"], resolved["artist"]) or video_id

    # Skip duplicates (same source video already downloaded)
    for meta_file in music_dir.glob("*.meta.json"):
        try:
            meta = json.loads(meta_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if meta.get("video_id") == video_id and (music_dir / meta_file.name[: -len(".meta.json")]).is_file():
            return {"status": "exists", **_file_info(meta_file)}

    try:
        payload = package_client.post_json(
            MUSIC_ENGINE, "/download",
            {"video_id": video_id, "dest_dir": str(music_dir), "stem": stem},
            timeout=900,
        )
    except (package_client.PackageUnavailable, package_client.PackageCallError) as exc:
        raise _engine_failure(exc) from exc

    produced = Path(str((payload or {}).get("path") or ""))
    try:
        inside = music_dir.resolve() in produced.resolve().parents
    except OSError:
        inside = False
    if not inside or not produced.is_file():
        logger.error("Engine reported %r, which is not a file inside %s", produced, music_dir)
        raise HTTPException(status_code=502, detail="Pobieranie zakończone, ale nie znaleziono pliku")

    # Remove leftovers (e.g. the native .webm the engine may have left behind).
    AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".webm", ".ogg", ".aac"}
    prefix = stem + "."
    for leftover in music_dir.iterdir():
        if leftover != produced and leftover.name.startswith(prefix) and leftover.suffix.lower() in AUDIO_EXTS:
            try:
                leftover.unlink()
            except OSError:
                pass  # best-effort cleanup — nothing to report

    meta = {
        "video_id": video_id,
        "title": resolved["title"],
        "artist": resolved["artist"],
        "thumbnail": resolved["thumbnail"],
        "duration": resolved["duration"],
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_file = produced.with_name(produced.name + ".meta.json")
    meta_file.write_text(json.dumps(meta, indent=2))
    return {"status": "downloaded", **_file_info(meta_file)}


def _file_info(meta_file: Path) -> dict:
    """Build a library entry from a sidecar meta file (or bare audio file).

    ``url`` is token-less on purpose — clients append their own `&token=`.
    """
    try:
        meta = json.loads(meta_file.read_text())
    except (json.JSONDecodeError, OSError):
        meta = {}
    name = meta_file.name[:-len(".meta.json")] if meta_file.name.endswith(".meta.json") else meta_file.name
    audio = meta_file.with_name(name) if meta_file.name.endswith(".meta.json") else meta_file
    return {
        "name": audio.name,
        "title": meta.get("title", Path(audio.stem).name),
        "artist": meta.get("artist", ""),
        "thumbnail": meta.get("thumbnail", ""),
        "duration": int(meta.get("duration") or 0),
        "video_id": meta.get("video_id", ""),
        "size": int(audio.stat().st_size) if audio.is_file() else 0,
        "modified": audio.stat().st_mtime if audio.is_file() else 0,
        "url": f"/api/music/library-file?name={urllib.parse.quote(audio.name)}",
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/resolve")
async def resolve(video_id: Optional[str] = Query(default=None), url: Optional[str] = Query(default=None)):
    """Metadata + direct stream URL for one video (debug / advanced clients)."""
    vid = video_id or ""
    if not vid and url:
        m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
        vid = m.group(1) if m else ""
    if not vid:
        raise HTTPException(status_code=422, detail="Podaj video_id lub url")
    try:
        return await asyncio.to_thread(_resolve_sync, vid)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("music resolve failed for %r: %s", vid, exc)
        raise HTTPException(status_code=502, detail=f"Rozpoznawanie nie powiodło się: {exc}") from exc


def _open_upstream(url: str, range_spec: Optional[str]):
    """Blocking open of an upstream URL (optional ``Range`` value).

    60 s per-read timeout: a stalled YouTube connection raises instead of
    hanging forever — the caller treats that as a reconnectable drop.
    """
    headers = {"User-Agent": "Mozilla/5.0 (EthOS NAS music proxy)"}
    if range_spec:
        headers["Range"] = range_spec
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=60)


def _total_size(resp) -> Optional[int]:
    """Total media size from Content-Range (206) or Content-Length (200)."""
    cr = resp.headers.get("Content-Range") or ""
    if "/" in cr:
        try:
            return int(cr.rsplit("/", 1)[1])
        except ValueError:
            pass  # intentional: a failure here must not break the caller
    cl = resp.headers.get("Content-Length")
    if cl:
        try:
            return int(cl)
        except ValueError:
            pass  # intentional: a failure here must not break the caller
    return None


@router.get("/stream")
async def stream(request: Request, video_id: str = Query(...)):
    """Proxy the audio stream with Range support.

    Clients (ExoPlayer / <audio>) always play through this endpoint — YouTube
    URLs are bound to the NAS IP that resolved them. Robustness:

    - expired cached URL (upstream 403/410) → cache invalidated, re-resolved,
      retried once before giving up;
    - upstream connection dropped mid-track → re-opened with ``Range`` from
      the last delivered byte (Google's CDN closes long connections), so the
      client keeps playing instead of hitting a media error.
    """
    try:
        upstream_url, ctype = await asyncio.to_thread(_resolve_stream, video_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("music stream resolve failed for %r: %s", video_id, exc)
        raise HTTPException(status_code=502, detail=f"Nie udało się pobrać strumienia: {exc}") from exc

    range_header = request.headers.get("range", "")

    # Suffix ranges (``bytes=-N``, i.e. "last N bytes" — sent by some players to
    # probe the file tail) are rejected by Google's CDN with 416, so convert
    # them into an absolute range using the total size probed from upstream.
    m_suffix = re.match(r"bytes=-(\d+)$", range_header) if range_header else None
    if m_suffix:
        try:
            probe = await asyncio.to_thread(_open_upstream, upstream_url, "bytes=0-0")
            try:
                total_probe = _total_size(probe)
            finally:
                probe.close()
        except Exception:
            total_probe = None
        if total_probe:
            range_header = f"bytes={max(0, total_probe - int(m_suffix.group(1)))}-"
        else:  # size unknown — serve the whole stream instead of failing
            range_header = ""

    # Absolute offset of this request (for mid-stream resume) and whether the
    # client asked for an *unbounded* range — only those can be resumed.
    m_range = re.match(r"bytes=(\d+)-$", range_header) if range_header else None
    base_start = int(m_range.group(1)) if m_range else 0
    # Only unbounded requests (no Range, or ``bytes=N-``) can be resumed.
    resumable = (not range_header) or bool(re.match(r"bytes=\d+-$", range_header))

    upstream = None
    for attempt in range(2):
        try:
            upstream = await asyncio.to_thread(_open_upstream, upstream_url, range_header or None)
            break
        except urllib.error.HTTPError as e:
            if e.code == 416:
                cr = e.headers.get("Content-Range") or ""
                return Response(status_code=416, headers={"Content-Range": cr} if cr else {})
            if e.code in (403, 410) and attempt == 0:
                # Cached stream URL expired on YouTube's side — drop it and re-resolve.
                logger.info("music stream URL for %s rejected (HTTP %d) — re-resolving", video_id, e.code)
                _stream_cache.pop(video_id, None)
                try:
                    upstream_url, ctype = await asyncio.to_thread(_resolve_stream, video_id)
                    continue
                except Exception as exc2:
                    raise HTTPException(status_code=502, detail=f"Nie udało się pobrać strumienia: {exc2}") from exc2
            raise HTTPException(status_code=502, detail=f"Błąd strumienia YouTube (HTTP {e.code})") from e
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Nie udało się połączyć z YouTube: {exc}") from exc

    status = getattr(upstream, "status", 200)
    total = _total_size(upstream)
    headers = {"Content-Type": ctype, "Accept-Ranges": "bytes"}
    content_range = upstream.headers.get("Content-Range")
    if content_range:
        headers["Content-Range"] = content_range
    content_length = upstream.headers.get("Content-Length")
    if content_length:
        headers["Content-Length"] = content_length

    async def _pipe():
        nonlocal upstream, upstream_url, ctype
        sent = 0
        for attempt in range(MAX_RECONNECTS + 1):
            try:
                while True:
                    chunk = await asyncio.to_thread(upstream.read, 65536)
                    if not chunk:
                        break
                    yield chunk
                    sent += len(chunk)
            except (OSError, urllib.error.URLError) as exc:
                logger.warning("music stream for %s interrupted at byte %d (%s)", video_id, base_start + sent, exc)
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass  # best-effort cleanup — nothing to report

            # Done? (normal EOF, or client asked for a bounded range that just finished)
            if not resumable or total is None or base_start + sent >= total:
                return
            if attempt == MAX_RECONNECTS:
                logger.error("music stream for %s gave up after %d reconnects at byte %d/%d",
                             video_id, MAX_RECONNECTS, base_start + sent, total)
                return

            # Upstream dropped early — resume from where we stopped.
            await asyncio.sleep(0.5 * (attempt + 1))
            try:
                upstream = await asyncio.to_thread(_open_upstream, upstream_url, f"bytes={base_start + sent}-")
            except urllib.error.HTTPError as e:
                if e.code in (403, 410):
                    _stream_cache.pop(video_id, None)
                    try:
                        upstream_url, ctype = await asyncio.to_thread(_resolve_stream, video_id)
                        upstream = await asyncio.to_thread(_open_upstream, upstream_url, f"bytes={base_start + sent}-")
                    except Exception as exc2:
                        logger.error("music stream for %s re-resolve failed: %s", video_id, exc2)
                        return
                else:
                    logger.error("music stream for %s reconnect rejected (HTTP %d) at byte %d",
                                 video_id, e.code, base_start + sent)
                    return
            except Exception as exc2:
                logger.error("music stream for %s reconnect failed: %s", video_id, exc2)
                return

    return StreamingResponse(_pipe(), status_code=status, headers=headers)


@router.post("/download")
async def download(payload: dict):
    """Download a track to the NAS music folder (m4a when ffmpeg is present)."""
    video_id = str(payload.get("video_id") or "").strip()
    url = str(payload.get("url") or "")
    if not video_id and url:
        m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
        video_id = m.group(1) if m else ""
    if not video_id:
        raise HTTPException(status_code=422, detail="Podaj video_id lub url")
    try:
        return await asyncio.to_thread(_download_sync, video_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("music download failed for %r: %s", video_id, exc)
        raise HTTPException(status_code=502, detail=f"Pobieranie nie powiodło się: {exc}") from exc


# ── Batch download (cała playlista → biblioteka NAS) ────────────────────────
# Parallel workers are the main speedup: each track is network-bound (YouTube
# extraction + Google CDN), so 3 concurrent downloads ≈ 3x faster than a
# sequential loop without meaningfully loading the uplink.
DOWNLOAD_CONCURRENCY = 3
MAX_BATCH_SIZE = 100

_batch_state: Dict[str, object] = {
    "running": False,
    "total": 0,
    "done": 0,
    "failed": 0,
    # video_id -> {"status": queued|downloading|done|failed, "title", "error"}
    "items": {},
}


async def _batch_worker(sem: asyncio.Semaphore, video_id: str) -> None:
    async with sem:
        item = _batch_state["items"][video_id]  # type: ignore[index]
        item["status"] = "downloading"
        try:
            result = await asyncio.to_thread(_download_sync, video_id)
            item["status"] = "done"
            item["title"] = str(result.get("title") or "")
            _batch_state["done"] += 1
        except Exception as exc:  # noqa: BLE001 — per-track failure must not kill the batch
            logger.warning("music batch download failed for %r: %s", video_id, exc)
            item["status"] = "failed"
            item["error"] = str(exc)[:200]
            _batch_state["failed"] += 1


@router.post("/download-batch")
async def download_batch(payload: dict):
    """Queue a bulk download (e.g. an entire playlist) into the NAS library.

    Runs up to ``DOWNLOAD_CONCURRENCY`` downloads in parallel and returns
    immediately — poll ``GET /downloads/progress`` for status. Tracks already
    present in the library are detected per-track and finish instantly.
    """
    if _batch_state["running"]:
        raise HTTPException(status_code=409, detail="Inne pobieranie zbiorcze jest już w toku")
    ids = [str(v).strip() for v in (payload.get("video_ids") or [])]
    ids = list(dict.fromkeys(i for i in ids if re.fullmatch(r"[A-Za-z0-9_-]{11}", i)))
    if not ids:
        raise HTTPException(status_code=422, detail="Podaj listę video_ids")
    if len(ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=422, detail=f"Maksymalnie {MAX_BATCH_SIZE} utworów na raz")

    _batch_state.update({
        "running": True,
        "total": len(ids),
        "done": 0,
        "failed": 0,
        "items": {vid: {"status": "queued", "title": "", "error": ""} for vid in ids},
    })

    sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
    tasks = [asyncio.create_task(_batch_worker(sem, vid)) for vid in ids]

    async def _finish() -> None:
        await asyncio.gather(*tasks, return_exceptions=True)
        _batch_state["running"] = False

    from backend.services import task_registry
    task_registry.spawn(_finish(), name="music-batch-finish")
    logger.info("music batch download started: %d tracks (concurrency=%d)", len(ids), DOWNLOAD_CONCURRENCY)
    return {"queued": len(ids)}


@router.get("/downloads/progress")
async def downloads_progress():
    """Status of the current (or last finished) batch download."""
    items: Dict[str, dict] = _batch_state["items"]  # type: ignore[assignment]
    return {
        "running": bool(_batch_state["running"]),
        "total": int(_batch_state["total"]),
        "done": int(_batch_state["done"]),
        "failed": int(_batch_state["failed"]),
        "items": {vid: dict(it) for vid, it in items.items()},
    }


@router.get("/library")
async def library(limit: int = Query(0, ge=0, le=5000),
                  offset: int = Query(0, ge=0)):
    """Downloaded tracks (audio + sidecar metadata), newest first, paginated."""
    items = await asyncio.to_thread(_library_items)
    total = len(items)
    window = items[offset:offset + limit] if limit else items[offset:]
    return {"dir": str(_music_dir()), "count": total, "total": total,
            "items": window, "limit": limit, "offset": offset}


@router.get("/library-file")
async def library_file(name: str = Query(...)):
    """Serve a downloaded file (Range-capable) for local playback.

    Starlette's FileResponse streams from disk in chunks and handles HTTP
    Range requests natively — the previous hand-rolled version read whole
    files into memory on non-range requests (OOM risk on big tracks).
    """
    music_dir = _music_dir()
    safe = Path(name).name  # strip any path components
    f = music_dir / safe
    if not f.is_file():
        raise HTTPException(status_code=404, detail="Plik nie istnieje")

    ctype = "audio/mp4" if f.suffix.lower() == ".m4a" else ("audio/ogg" if f.suffix.lower() in (".opus", ".ogg") else "application/octet-stream")
    return FileResponse(str(f), media_type=ctype)


@router.delete("/library/{name}")
async def library_delete(name: str):
    """Remove a downloaded file (+ its metadata sidecar)."""
    global _library_cache
    music_dir = _music_dir()
    safe = Path(name).name
    f = music_dir / safe
    meta = f.with_name(f.name + ".meta.json")
    if not f.is_file():
        raise HTTPException(status_code=404, detail="Plik nie istnieje")
    f.unlink()
    if meta.is_file():
        meta.unlink()
    _library_cache = None
    return {"deleted": safe}


# ── Radio (live streams) ──────────────────────────────────────────────
# Catalog shared with the Android app and the web player. Generated by
# scripts/build_radio_catalog.py (radio-browser.info + curated list) and read on
# every request so regenerating it needs no restart. Kept in the repo (not in
# backend/data/, which is gitignored runtime state).
RADIO_FILE = Path(__file__).resolve().parent.parent / "radio_stations.json"
PODCAST_FILE = Path(__file__).resolve().parent.parent / "podcast_catalog.json"


def _load_radio_catalog() -> dict:
    """Read the station catalog (empty catalog if missing)."""
    try:
        with RADIO_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        stations = []
        for s in data.get("stations", []):
            url = str(s.get("stream_url") or "")
            name = str(s.get("name") or "")
            if not url or not name:
                continue
            stations.append({
                "id": str(s.get("id") or ""),
                "name": name,
                "category": str(s.get("category") or ""),
                "stream_url": url,
                # Optional richer metadata (schema v2). Old catalogs simply omit it.
                "tags": [str(t) for t in (s.get("tags") or []) if str(t).strip()],
                "city": str(s.get("city") or ""),
                "language": str(s.get("language") or "pl"),
                "logo": str(s.get("logo") or ""),
                "codec": str(s.get("codec") or ""),
                "bitrate": int(s.get("bitrate") or 0),
                "hls": bool(s.get("hls")),
                "homepage": str(s.get("homepage") or ""),
                "verified": bool(s.get("verified")),
            })
        categories = [str(c) for c in (data.get("categories") or []) if str(c)]
        return {
            "categories": categories,
            "stations": stations,
            "total": len(stations),
            "generated_at": str(data.get("generated_at") or ""),
        }
    except FileNotFoundError:
        logger.warning("Radio catalog missing at %s — returning an empty list", RADIO_FILE)
        return {"categories": [], "stations": [], "total": 0, "generated_at": ""}
    except Exception as e:
        logger.error("Failed to read radio catalog %s: %s", RADIO_FILE, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Nie udało się wczytać listy stacji radiowych") from e


def _load_podcast_catalog() -> dict:
    """Read the discovery catalog (empty catalog if the generator has not run)."""
    try:
        with PODCAST_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        podcasts = []
        for p in data.get("podcasts", []):
            rss = str(p.get("rss_url") or "")
            name = str(p.get("name") or "")
            if not rss or not name:
                continue
            podcasts.append({
                "id": str(p.get("id") or ""),
                "name": name,
                "author": str(p.get("author") or ""),
                "category": str(p.get("category") or ""),
                "description": str(p.get("description") or ""),
                "rss_url": rss,
                "cover_url": str(p.get("cover_url") or ""),
                "language": str(p.get("language") or "pl"),
                "episode_count": int(p.get("episode_count") or 0),
                "last_episode": str(p.get("last_episode") or ""),
            })
        return {
            "categories": [str(c) for c in (data.get("categories") or []) if str(c)],
            "podcasts": podcasts,
            "total": len(podcasts),
            "generated_at": str(data.get("generated_at") or ""),
        }
    except FileNotFoundError:
        logger.warning("Podcast catalog missing at %s — returning an empty list", PODCAST_FILE)
        return {"categories": [], "podcasts": [], "total": 0, "generated_at": ""}
    except Exception as e:
        logger.error("Failed to read podcast catalog %s: %s", PODCAST_FILE, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Nie udało się wczytać katalogu podcastów") from e


_RADIO_FIELD_WEIGHTS = {"name": 20.0, "category": 5.0, "tags": 4.0, "city": 3.0}
_PODCAST_FIELD_WEIGHTS = {"name": 20.0, "author": 6.0, "category": 4.0, "description": 1.5}


@router.get("/radio/stations")
async def radio_stations(
    q: str = Query("", max_length=120),
    category: str = Query("", max_length=60),
):
    """Polish radio stations (live streams) with optional text/category filtering."""
    catalog = await asyncio.to_thread(_load_radio_catalog)
    stations = catalog["stations"]
    used_categories = [c for c in catalog["categories"] if any(s["category"] == c for s in stations)]

    query = q.strip()
    if category and category not in ("", "Wszystkie"):
        stations = [s for s in stations if s["category"] == category]
    if query:
        stations = music_search.search_catalog(
            stations, query, name_keys=("name",), field_weights=_RADIO_FIELD_WEIGHTS
        )

    return {
        "categories": used_categories,
        "stations": stations,
        "total": len(stations),
        "generated_at": catalog.get("generated_at") or "",
    }


@router.get("/podcasts/catalog")
async def podcast_catalog(
    q: str = Query("", max_length=120),
    category: str = Query("", max_length=60),
    sort: str = Query("popular", pattern="^(popular|name|recent)$"),
    limit: int = Query(0, ge=0, le=500),
    offset: int = Query(0, ge=0, le=5000),
):
    """Podcast discovery catalog (Polish shows), searchable and paginated."""
    catalog = await asyncio.to_thread(_load_podcast_catalog)
    items = catalog["podcasts"]
    used_categories = [c for c in catalog["categories"] if any(p["category"] == c for p in items)]

    if category and category not in ("", "Wszystkie"):
        items = [p for p in items if p["category"] == category]
    if q.strip():
        items = music_search.search_catalog(
            items, q, name_keys=("name",), field_weights=_PODCAST_FIELD_WEIGHTS
        )
    elif sort == "name":
        items = sorted(items, key=lambda p: music_search.fold(p.get("name")))
    elif sort == "recent":
        items = sorted(items, key=lambda p: str(p.get("last_episode") or ""), reverse=True)

    total = len(items)
    window = items[offset:offset + limit] if limit else items[offset:]
    return {
        "categories": used_categories,
        "items": window,
        "total": total,
        "has_more": bool(limit) and offset + limit < total,
        "generated_at": catalog.get("generated_at") or "",
    }


@router.get("/config")
async def get_config():
    """Music app settings (download dir)."""
    cfg = _load_config()
    return {
        "download_dir": str(Path(cfg["download_dir"]).expanduser()),
        "ffmpeg_available": bool(shutil.which("ffmpeg")),
        "ytdlp_version": _ytdlp_version(),
    }


@router.post("/config")
async def set_config(payload: dict):
    """Update music settings (only download_dir is user-configurable)."""
    cfg = _load_config()
    new_dir = str(payload.get("download_dir") or "").strip()
    if new_dir:
        # Absolute-path requirement, symlink resolution and the caller's scope
        # all live in path_scope.sanitize(); a non-admin cannot point the
        # library outside their home or the shared mounts.
        cfg["download_dir"] = path_scope.sanitize(new_dir)
    _save_config(cfg)
    return {"download_dir": str(Path(cfg["download_dir"]).expanduser())}


# ── Engine version (reported in /config) ─────────────────────────────

def _ytdlp_version() -> Optional[str]:
    """yt-dlp version inside the engine package (None when it is not installed)."""
    try:
        info = package_client.get_json(MUSIC_ENGINE, "/health", timeout=5)
    except (package_client.PackageUnavailable, package_client.PackageCallError):
        return None
    version = info.get("version") if isinstance(info, dict) else None
    return str(version) if version else None


# ── Library state (server-side sync) ────────────────────────────────
# One document per account in <DATA_DIR>/users/<user>/music_library.json —
# the single source of truth for liked tracks, playlists, playback history
# and podcast subscriptions, shared by the web app and Android.

HISTORY_CAP = 50
_lib_lock = asyncio.Lock()


def _library_store():
    """Per-user library document (no cross-account keys to leak)."""
    return user_data.user_store("music_library.json")


def _empty_state() -> dict:
    return {
        "liked": [],
        "playlists": [],          # [{id, name, tracks: [track]}]
        "history": [],            # newest first, capped at HISTORY_CAP
        "podcasts": {"subscribed": [], "episodes": {}},  # podcast subs + fetched episodes
        "radio": {"favorites": [], "recents": []},        # radio station ids (web + Android)
    }


def _load_states() -> dict:
    """The current user's state, wrapped in the legacy {uid: state} shape.

    Endpoints keep using the ``states[uid]`` pattern; the file itself now
    belongs to one account.
    """
    uid = user_data.current_username()
    data = _library_store().load()
    return {uid: data} if isinstance(data, dict) else {}


def _save_states(states: dict) -> None:
    uid = user_data.current_username()
    try:
        _library_store().save(states.get(uid) or _empty_state())
    except Exception as e:
        logger.error("music: cannot write the user library: %s", e)


def _user_state(request: Request) -> tuple[str, dict]:
    """(username, state-doc) for the authenticated user."""
    uid = user_data.current_username()
    data = _library_store().load()
    st = data if isinstance(data, dict) else _empty_state()
    # backfill missing sections (older docs)
    for k, v in _empty_state().items():
        st.setdefault(k, copy.deepcopy(v) if isinstance(v, (list, dict)) else v)
    return uid, st


def _track_from_payload(payload: Any) -> dict:
    """Utwór z ciała requesta — akceptuje zarówno {"track": {...}}, jak i goły obiekt."""
    if isinstance(payload, dict):
        inner = payload.get("track")
        return inner if isinstance(inner, dict) else payload
    return {}


def _norm_track(t: Any) -> Optional[dict]:
    """Normalize a track JSON to the shared shape used by both clients."""
    if not isinstance(t, dict):
        return None
    tid = str(t.get("id") or "").strip()
    title = str(t.get("title") or "").strip()
    if not tid and not title:
        return None
    return {
        "id": tid,
        "title": title,
        "artist": str(t.get("artist") or ""),
        "thumb": str(t.get("thumb") or t.get("thumbnail") or ""),
        "dur": int(t.get("dur") or t.get("durationSec") or 0),
        "file_url": str(t.get("file_url") or t.get("fileUrl") or ""),
        "date": int(t.get("date") or t.get("dateEpochSec") or 0),
    }


def _track_key(t: dict) -> str:
    return f"{t['id']}|{t['title']}"


def _add_dedup(lst: list, track: dict) -> bool:
    """Append if not already present (by key). Returns True when added."""
    k = _track_key(track)
    for existing in lst:
        if _track_key(existing) == k:
            return False
    lst.append(track)
    return True


def _remove_by_key(lst: list, key: str) -> bool:
    before = len(lst)
    lst[:] = [t for t in lst if _track_key(t) != key]
    return len(lst) != before


def _norm_podcast(p: Any) -> Optional[dict]:
    if not isinstance(p, dict):
        return None
    rss = str(p.get("rss_url") or p.get("rssUrl") or "").strip()
    if not rss:
        return None
    # Id nadaje wyłącznie serwer (sha256 feeda) — klienci mogą wysyłać dowolny id,
    # bo to gwarantuje jedną tożsamość podcastu na web + Android.
    pid = hashlib.sha256(rss.encode()).hexdigest()[:16]
    return {
        "id": pid,
        "title": str(p.get("title") or "Podcast"),
        "desc": str(p.get("desc") or p.get("description") or ""),
        "rss_url": rss,
        "cover": str(p.get("cover") or p.get("coverUrl") or ""),
    }


@router.get("/state")
async def state_get(request: Request):
    """Full library state for the current user (web + Android share this)."""
    _, st = _user_state(request)
    return st


@router.put("/state")
async def state_put(payload: dict, request: Request):
    """Merge the client's state into the server document. Server data wins per
    section — used for one-time import of data that previously lived only on the
    phone (library.json / podcasts.json). Safe when both Android stores import
    concurrently: each fills only what is still missing."""
    uid, _ = _user_state(request)
    liked_in = [t for t in (_norm_track(t) for t in (payload.get("liked") or [])) if t]
    playlists_in = []
    for pl in (payload.get("playlists") or []):
        if not isinstance(pl, dict):
            continue
        pid = str(pl.get("id") or "") or uuid.uuid4().hex[:12]
        tracks = [_norm_track(t) for t in (pl.get("tracks") or [])]
        playlists_in.append({"id": pid, "name": str(pl.get("name") or "Playlist"),
                             "tracks": [t for t in tracks if t]})
    hist_in = [t for t in (_norm_track(t) for t in (payload.get("history") or [])) if t][:HISTORY_CAP]
    pc_in = payload.get("podcasts") or {}
    subs_in = [p for p in (_norm_podcast(p) for p in ((pc_in or {}).get("subscribed") or [])) if p]
    episodes_in: dict = {}
    for pid, eps in ((pc_in or {}).get("episodes") or {}).items():
        norm = [t for t in (_norm_track(t) for t in (eps or [])) if t]
        if norm:
            episodes_in[str(pid)] = norm

    async with _lib_lock:
        states = _load_states()
        cur = states.get(uid)
        if not isinstance(cur, dict):
            cur = _empty_state()
        for k, v in _empty_state().items():
            cur.setdefault(k, copy.deepcopy(v) if isinstance(v, (list, dict)) else v)
        if not cur["liked"]:
            cur["liked"] = liked_in
        if not cur["playlists"]:
            cur["playlists"] = playlists_in
        if not cur["history"]:
            cur["history"] = hist_in
        pc = cur["podcasts"]
        if not pc.get("subscribed"):
            pc["subscribed"] = subs_in
        eps_cur = pc.setdefault("episodes", {})
        for pid, eps in episodes_in.items():
            if not eps_cur.get(pid):
                eps_cur[pid] = eps
        states[uid] = cur
        _save_states(states)
    return cur


# ── Radio favourites (shared by web + Android) ────────────────────────
RADIO_FAVORITES_CAP = 500
RADIO_RECENTS_CAP = 8
_RADIO_ID_RE = re.compile(r"^[0-9a-zA-Z_-]{1,40}$")


def _clean_radio_ids(values: Any, cap: int) -> List[str]:
    """Validate station ids, de-duplicate, keep order and cap the list."""
    out: List[str] = []
    for value in values or []:
        sid = str(value or "").strip()
        if not sid or not _RADIO_ID_RE.match(sid) or sid in out:
            continue
        out.append(sid)
        if len(out) >= cap:
            break
    return out


@router.get("/radio/state")
async def radio_state_get(request: Request):
    """Favourite + recently played station ids for the current user."""
    _, state = _user_state(request)
    radio = state.get("radio") if isinstance(state.get("radio"), dict) else {}
    return {
        "favorites": _clean_radio_ids(radio.get("favorites"), RADIO_FAVORITES_CAP),
        "recents": _clean_radio_ids(radio.get("recents"), RADIO_RECENTS_CAP),
    }


@router.put("/radio/state")
async def radio_state_put(payload: dict, request: Request):
    """Replace the user's radio state (clients debounce and send the whole doc)."""
    uid, _ = _user_state(request)
    radio = {
        "favorites": _clean_radio_ids((payload or {}).get("favorites"), RADIO_FAVORITES_CAP),
        "recents": _clean_radio_ids((payload or {}).get("recents"), RADIO_RECENTS_CAP),
    }
    async with _lib_lock:
        states = _load_states()
        cur = states.get(uid) or _empty_state()
        cur["radio"] = radio
        states[uid] = cur
        _save_states(states)
    return radio


@router.post("/liked")
async def liked_add(payload: dict, request: Request):
    """Add a track to liked (idempotent — dedup by id|title)."""
    track = _norm_track(_track_from_payload(payload))
    if not track:
        raise HTTPException(status_code=422, detail="Brak poprawnego utworu")
    uid, st = _user_state(request)
    async with _lib_lock:
        states = _load_states()
        cur = states.get(uid) or _empty_state()
        added = _add_dedup(cur.setdefault("liked", []), track)
        if added:
            states[uid] = cur
            _save_states(states)
    return {"added": added, "count": len(cur.get("liked") or [])}


@router.delete("/liked")
async def liked_remove(payload: dict, request: Request):
    """Remove a track from liked (by id|title key)."""
    key = str((payload or {}).get("key") or "")
    if not key:
        raise HTTPException(status_code=422, detail="Brak klucza utworu")
    uid, st = _user_state(request)
    async with _lib_lock:
        states = _load_states()
        cur = states.get(uid) or _empty_state()
        removed = _remove_by_key(cur.setdefault("liked", []), key)
        if removed:
            states[uid] = cur
            _save_states(states)
    return {"removed": removed, "count": len(cur.get("liked") or [])}


@router.get("/playlists/shared")
async def shared_playlist(owner: str = Query(...), id: str = Query(...)) -> dict:
    """Read-only view of another account's playlist (requires a grant).

    Local tracks are rewritten to ``/api/files/raw?path=…`` so the recipient
    streams them through the normal read scope; remote/engine tracks keep their
    existing URL. Playlist mutation endpoints stay owner-only.
    """
    caller = user_data.current_username()
    if not owner or owner == caller:
        raise HTTPException(status_code=400, detail="Owner must be another user")
    if not sharing_service.allows_resource(caller, owner, "playlist", id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    state = user_data.user_store("music_library.json", dict, username=owner).load()
    playlist = next((p for p in (state.get("playlists") or [])
                     if isinstance(p, dict) and str(p.get("id") or "") == id), None)
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    allowed_paths = sharing_resources.shareable_paths(owner, "playlist", id)
    tracks = []
    for raw in playlist.get("tracks") or []:
        if not isinstance(raw, dict):
            continue
        track = dict(raw)
        local = sharing_resources.local_track_path(owner, track.get("file_url") or "")
        if local:
            if local not in allowed_paths:
                continue  # not shareable (e.g. path outside the owner's scope)
            track["file_url"] = "/api/files/raw?path=" + urllib.parse.quote(local)
            track["shared_local"] = True
        tracks.append(track)
    return {
        "id": id,
        "name": str(playlist.get("name") or ""),
        "owner": owner,
        "tracks": tracks,
        "count": len(tracks),
    }


@router.post("/playlists")
async def playlist_create(payload: dict, request: Request):
    """Create a playlist. Body: {name, id?}. The client may supply its own id so the
    same playlist keeps one identity across web + Android; a server id is generated
    when omitted or when the supplied id already exists."""
    name = str((payload or {}).get("name") or "").strip() or "Nowa playlista"
    wanted_id = str((payload or {}).get("id") or "").strip()
    uid, st = _user_state(request)
    async with _lib_lock:
        states = _load_states()
        cur = states.get(uid) or _empty_state()
        pls = cur.setdefault("playlists", [])
        existing_ids = {str(p.get("id")) for p in pls}
        pid = wanted_id if (wanted_id and wanted_id not in existing_ids) else uuid.uuid4().hex[:12]
        pl = {"id": pid, "name": name, "tracks": []}
        pls.append(pl)
        states[uid] = cur
        _save_states(states)
    return pl


@router.delete("/playlists/{pid}")
async def playlist_delete(pid: str, request: Request):
    """Delete a playlist by id."""
    uid, st = _user_state(request)
    async with _lib_lock:
        states = _load_states()
        cur = states.get(uid) or _empty_state()
        before = len(cur.setdefault("playlists", []))
        cur["playlists"] = [p for p in cur["playlists"] if str(p.get("id")) != pid]
        deleted = len(cur["playlists"]) != before
        if deleted:
            states[uid] = cur
            _save_states(states)
    return {"deleted": deleted}


@router.put("/playlists/{pid}/tracks")
async def playlist_track_add(pid: str, payload: dict, request: Request):
    """Add a track to a playlist (idempotent — dedup by id|title)."""
    track = _norm_track(_track_from_payload(payload))
    if not track:
        raise HTTPException(status_code=422, detail="Brak poprawnego utworu")
    uid, st = _user_state(request)
    async with _lib_lock:
        states = _load_states()
        cur = states.get(uid) or _empty_state()
        pl = next((p for p in cur.setdefault("playlists", []) if str(p.get("id")) == pid), None)
        if not pl:
            raise HTTPException(status_code=404, detail="Playlista nie istnieje")
        added = _add_dedup(pl.setdefault("tracks", []), track)
        if added:
            states[uid] = cur
            _save_states(states)
    return {"added": added, "count": len(pl.get("tracks") or [])}


@router.delete("/playlists/{pid}/tracks")
async def playlist_track_remove(pid: str, payload: dict, request: Request):
    """Remove a track from a playlist (by id|title key)."""
    key = str((payload or {}).get("key") or "")
    if not key:
        raise HTTPException(status_code=422, detail="Brak klucza utworu")
    uid, st = _user_state(request)
    async with _lib_lock:
        states = _load_states()
        cur = states.get(uid) or _empty_state()
        pl = next((p for p in cur.setdefault("playlists", []) if str(p.get("id")) == pid), None)
        if not pl:
            raise HTTPException(status_code=404, detail="Playlista nie istnieje")
        removed = _remove_by_key(pl.setdefault("tracks", []), key)
        if removed:
            states[uid] = cur
            _save_states(states)
    return {"removed": removed, "count": len(pl.get("tracks") or [])}


@router.post("/history")
async def history_add(payload: dict, request: Request):
    """Record a played track (newest first, capped). Idempotent per key."""
    track = _norm_track(_track_from_payload(payload))
    if not track:
        raise HTTPException(status_code=422, detail="Brak poprawnego utworu")
    uid, st = _user_state(request)
    async with _lib_lock:
        states = _load_states()
        cur = states.get(uid) or _empty_state()
        hist = cur.setdefault("history", [])
        k = _track_key(track)
        if not any(_track_key(t) == k for t in hist):
            hist.insert(0, track)
            del hist[HISTORY_CAP:]
            states[uid] = cur
            _save_states(states)
    return {"ok": True, "count": len(cur.get("history") or [])}


@router.post("/podcasts/subscribe")
async def podcast_subscribe(payload: dict, request: Request):
    """Add a podcast subscription (dedup by feed URL)."""
    pod = _norm_podcast(payload.get("podcast") if isinstance(payload, dict) else payload)
    if not pod:
        raise HTTPException(status_code=422, detail="Brak poprawnego podcastu")
    uid, st = _user_state(request)
    async with _lib_lock:
        states = _load_states()
        cur = states.get(uid) or _empty_state()
        subs = cur.setdefault("podcasts", {}).setdefault("subscribed", [])
        existing = next((s for s in subs if s.get("rss_url") == pod["rss_url"]), None)
        if existing is not None:
            # upsert — odśwież metadane (tytuł/okładka/opis) po pobraniu feeda
            existing["id"] = pod["id"]  # normalizacja tożsamości (sha256 feeda)
            for k in ("title", "desc", "cover"):
                if pod.get(k):
                    existing[k] = pod[k]
        else:
            subs.append(pod)
        states[uid] = cur
        _save_states(states)
    return {"added": existing is None, "count": len(subs)}


@router.delete("/podcasts/{pid}")
async def podcast_unsubscribe(pid: str, request: Request):
    """Remove a subscription (+ its cached episodes) by id."""
    uid, st = _user_state(request)
    async with _lib_lock:
        states = _load_states()
        cur = states.get(uid) or _empty_state()
        pc = cur.setdefault("podcasts", {})
        before = len(pc.setdefault("subscribed", []))
        pc["subscribed"] = [s for s in pc["subscribed"] if str(s.get("id")) != pid]
        removed = len(pc["subscribed"]) != before
        if removed:
            pc.get("episodes", {}).pop(pid, None)
            states[uid] = cur
            _save_states(states)
    return {"removed": removed}


@router.put("/podcasts/{pid}/episodes")
async def podcast_episodes_save(pid: str, request: Request):
    """Store the latest fetched episodes for a subscription (replaces)."""
    try:
        payload = await request.json()
    except Exception:
        payload = None
    eps_in = (payload if isinstance(payload, list)
              else ((payload or {}).get("episodes") or (payload or {}).get("tracks") or []))
    norm = [_norm_track(t) for t in eps_in]
    norm = [t for t in norm if t]
    uid, st = _user_state(request)
    async with _lib_lock:
        states = _load_states()
        cur = states.get(uid) or _empty_state()
        pc = cur.setdefault("podcasts", {})
        subs = pc.setdefault("subscribed", [])
        if not any(str(s.get("id")) == pid for s in subs):
            raise HTTPException(status_code=404, detail="Subskrypcja nie istnieje")
        episodes = pc.setdefault("episodes", {})
        if norm:
            episodes[pid] = norm
        else:
            episodes.pop(pid, None)
        states[uid] = cur
        _save_states(states)
    return {"ok": True, "count": len(norm)}


# ── Podcast feed proxy (RSS/Atom) ───────────────────────────────────
# The Android app fetches feeds directly from the internet; browsers would hit
# CORS, so the web client asks the NAS to fetch + parse instead. Parser is a
# 1:1 port of ethos-music's RssClient.kt (tolerant regex-based parsing).

_feed_cache: dict[str, tuple[float, dict]] = {}
FEED_CACHE_TTL = 600  # s


def _parse_feed(xml: str, max_episodes: int) -> dict:
    """Parse an RSS/Atom document via services/podcast_service (TODO.md P2-04).

    The regex parser that used to live here could be fooled by a CDATA body
    containing `</item>`; the service walks real XML. A document it cannot
    understand stays a 422 with the same message the UI already shows.
    """
    from backend.services.podcast_service import FeedError, parse_feed

    try:
        return parse_feed(xml, max_episodes)
    except FeedError as exc:
        logger.warning("music: podcast feed rejected: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validate_public_url(url: str, *, kind: str = "RSS") -> str:
    """Validate a user-supplied URL before the NAS fetches it.

    ``/podcast-feed?url=`` and ``/episode-audio?url=`` make the server perform
    an HTTP GET, so an authenticated client could otherwise probe the LAN or
    localhost (SSRF). Only http(s) URLs whose host resolves exclusively to
    public addresses pass.
    """
    raw = (url or "").strip()
    if not raw.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail=f"Nieprawidłowy adres {kind}")

    parsed = urllib.parse.urlparse(raw)
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=422, detail=f"Adres {kind} bez nazwy hosta")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise HTTPException(status_code=422, detail=f"Nie można rozwiązać hosta: {host}") from e

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            logger.warning("music: blocked non-public %s target %s (%s)", kind, host, ip)
            raise HTTPException(
                status_code=422,
                detail=f"{kind} nie może wskazywać adresu w sieci lokalnej",
            )
    return raw


def _validate_feed_url(url: str) -> str:
    """Backwards-compatible wrapper used by ``/podcast-feed``."""
    return _validate_public_url(url, kind="Feed")


def _fetch_feed_sync(url: str, max_episodes: int) -> dict:
    # Caller validates the URL (see _validate_feed_url) — bad targets never reach here.
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "EthosMusic/1.0 (podcast player; via EthOS NAS)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        if getattr(resp, "status", 200) >= 400:
            raise HTTPException(status_code=502, detail=f"HTTP {resp.status} — feed niedostępny")
        raw = resp.read(5 * 1024 * 1024)
    try:
        xml_text = raw.decode("utf-8", errors="replace")
    except Exception:
        xml_text = ""
    return _parse_feed(xml_text, max_episodes)


@router.get("/podcast-feed")
async def podcast_feed(url: str, limit: int = 25):
    """Fetch + parse a public RSS/Atom feed (proxy for the web client).
    Auth-gated; results cached in memory for 10 minutes."""
    url = _validate_feed_url(url)
    limit = max(1, min(int(limit), 50))

    now = time.time()
    hit = _feed_cache.get(url)
    if hit and now - hit[0] < FEED_CACHE_TTL:
        return {**hit[1], "cached": True}

    try:
        result = await asyncio.to_thread(_fetch_feed_sync, url, limit)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("music: feed fetch failed for %s: %s", url, e)
        raise HTTPException(status_code=502, detail=f"Nie udało się pobrać feeda: {e}")
    _feed_cache[url] = (now, result)
    return {**result, "cached": False}


# ── Podcast episode audio (offline downloads) ────────────────────────
# Episode enclosures live on third-party CDNs, usually without CORS headers, so
# a browser cannot read them into a Blob and store them for offline playback.
# This proxy lets the /music PWA download an episode the same way it downloads
# a track from /stream. Nothing is written to disk: the bytes are relayed.
#
# The URL comes from the client, so the SSRF guard is mandatory — every URL and
# every redirect hop must resolve to a public address. Redirects are followed
# manually for exactly this reason (urllib would follow them blindly).

EPISODE_AUDIO_MAX_BYTES = 400 * 1024 * 1024  # hard stop, ~400 MiB
EPISODE_AUDIO_TIMEOUT = 60
EPISODE_REDIRECTS = 4
_AUDIO_TYPES = ("audio/", "video/", "application/octet-stream", "binary/octet-stream")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn a redirect into an HTTPError so the caller can validate the target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _open_public_media(url: str, range_spec: Optional[str]):
    """Open an external media URL, validating the host of every redirect hop."""
    current = _validate_public_url(url, kind="Adres odcinka")
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(EPISODE_REDIRECTS):
        headers = {"User-Agent": "Mozilla/5.0 (EthOS NAS episode proxy)"}
        if range_spec:
            headers["Range"] = range_spec
        request = urllib.request.Request(current, headers=headers)
        try:
            return opener.open(request, timeout=EPISODE_AUDIO_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                raise
            target = urllib.parse.urljoin(current, exc.headers.get("Location") or "")
            exc.close()
            if not target:
                raise HTTPException(status_code=502, detail="Przekierowanie bez adresu docelowego")
            current = _validate_public_url(target, kind="Adres odcinka")
    raise HTTPException(status_code=502, detail="Zbyt wiele przekierowań odcinka")


@router.get("/episode-audio")
async def episode_audio(request: Request, url: str = Query(...)):
    """Relay a podcast enclosure so the PWA can store it for offline playback.

    Range requests are forwarded unchanged (``<audio>`` and the downloader use
    them), the payload size is capped and only audio-ish content types pass —
    an HTML error page from a CDN must not be cached as an episode.
    """
    range_header = request.headers.get("range", "")
    try:
        upstream = await asyncio.to_thread(_open_public_media, url, range_header or None)
    except HTTPException:
        raise
    except urllib.error.HTTPError as exc:
        logger.warning("music: episode audio rejected (HTTP %s): %s", exc.code, url[:200])
        raise HTTPException(status_code=502, detail=f"Nie udało się pobrać odcinka (HTTP {exc.code})") from exc
    except Exception as exc:
        logger.warning("music: episode audio failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Nie udało się pobrać odcinka: {exc}") from exc

    ctype = (upstream.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip().lower()
    if not ctype.startswith(_AUDIO_TYPES):
        upstream.close()
        logger.warning("music: episode audio has non-audio type %r: %s", ctype, url[:200])
        raise HTTPException(status_code=502, detail="Adres nie zwrócił pliku audio")

    status = getattr(upstream, "status", 200)
    headers = {"Content-Type": ctype, "Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    for header in ("Content-Length", "Content-Range"):
        value = upstream.headers.get(header)
        if value:
            headers[header] = value

    async def _pipe():
        sent = 0
        try:
            while True:
                chunk = await asyncio.to_thread(upstream.read, 65536)
                if not chunk:
                    break
                sent += len(chunk)
                if sent > EPISODE_AUDIO_MAX_BYTES:
                    logger.warning("music: episode audio truncated at %d bytes: %s",
                                   sent, url[:200])
                    break
                yield chunk
        except Exception as exc:  # noqa: BLE001 — a dropped CDN connection ends the body
            logger.warning("music: episode audio interrupted after %d bytes: %s", sent, exc)
        finally:
            try:
                upstream.close()
            except Exception:
                pass  # best-effort cleanup

    return StreamingResponse(_pipe(), status_code=status, headers=headers)
