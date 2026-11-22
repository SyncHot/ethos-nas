"""Resource resolvers for user-to-user sharing.

A grant stores a stable resource identity, not a snapshot of paths:

- ``path``     — an absolute file or folder path;
- ``album``    — a Photos album id in the owner's ``albums.json``;
- ``playlist`` — a Music playlist id in the owner's ``music_library.json``.

Resolving here (instead of inside the app routers) keeps
``services/sharing_service.py`` free of router imports — the boundary rule in
``scripts/check_boundaries.py`` — and lets album/playlist edits take effect
without touching the grant.

This module never opens media files. It only reads JSON stores owned by the
grant owner and returns path strings; access to the bytes still goes through
``path_scope.sanitize_read`` and the normal file/media endpoints.
"""

import logging
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from backend.config import settings
from backend.services import user_data

logger = logging.getLogger("ethos-nas.services.sharing_resources")

KINDS = ("path", "album", "playlist")
SHAREABLE_ROOTS = ("/home", "/mnt", "/media")

# Mirrors the Music router's default; the owner's music_config.json overrides it.
_DEFAULT_MUSIC_DIR = Path.home() / "Music" / "Ethos"


def _under(path: str, root: str) -> bool:
    root = root.rstrip(os.sep) or root
    return path == root or path.startswith(root + os.sep)


def is_shareable_path(real_path: str) -> bool:
    """True when a resolved path may be exposed to another account.

    Shares are deliberately narrower than an admin's own scope: only NAS data
    roots, never the EthOS state directory (which stores API keys, configs and
    logs), even when DATA_DIR lives under /home.
    """
    if not real_path:
        return False
    real = os.path.realpath(real_path)
    data_dir = os.path.realpath(str(settings.DATA_DIR))
    if _under(real, data_dir):
        return False
    return any(_under(real, root) for root in SHAREABLE_ROOTS)


def _load_albums(owner: str) -> list:
    data = user_data.user_store("albums.json", list, username=owner).load()
    return data if isinstance(data, list) else []


def _find_album(owner: str, album_id: str) -> dict | None:
    for album in _load_albums(owner):
        if isinstance(album, dict) and str(album.get("id") or "") == album_id:
            return album
    return None


def _load_music_state(owner: str) -> dict:
    data = user_data.user_store("music_library.json", dict, username=owner).load()
    return data if isinstance(data, dict) else {}


def _find_playlist(owner: str, playlist_id: str) -> dict | None:
    for playlist in (_load_music_state(owner).get("playlists") or []):
        if isinstance(playlist, dict) and str(playlist.get("id") or "") == playlist_id:
            return playlist
    return None


def music_dir(owner: str) -> str:
    """The owner's configured download directory (router default as fallback)."""
    cfg = user_data.user_store("music_config.json", dict, username=owner).load()
    value = str((cfg or {}).get("download_dir") or "").strip()
    return value or str(_DEFAULT_MUSIC_DIR)


def local_track_path(owner: str, file_url: str) -> str | None:
    """Resolve a playlist track's local file URL to an absolute path.

    Remote/engine URLs (YouTube streams, podcasts) have no local path and are
    skipped — they are already served by the core proxies, not per-user files.
    """
    raw = str(file_url or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if parts.scheme in ("http", "https"):
        return None
    if parts.scheme == "file":
        path = unquote(parts.path)
        return os.path.abspath(path) if path else None
    if parts.path in ("/api/music/library-file", "/api/music/library-file/"):
        names = parse_qs(parts.query).get("name") or []
        if not names:
            return None
        name = os.path.basename(names[0])
        if not name:
            return None
        return os.path.realpath(os.path.join(music_dir(owner), name))
    if parts.path in ("/api/files/raw", "/api/files/download"):
        paths = parse_qs(parts.query).get("path") or []
        if not paths:
            return None
        return os.path.realpath(paths[0])
    return None


def resource_exists(owner: str, kind: str, resource: str) -> bool:
    """True when the resource belongs to `owner` and can be granted."""
    if kind == "path":
        return bool(resource) and os.path.exists(resource)
    if kind == "album":
        return _find_album(owner, resource) is not None
    if kind == "playlist":
        return _find_playlist(owner, resource) is not None
    return False


def label(owner: str, kind: str, resource: str) -> str:
    """Human label for a grant (album/playlist name, path basename)."""
    try:
        if kind == "album":
            album = _find_album(owner, resource)
            return str(album.get("name") or "") if album else ""
        if kind == "playlist":
            playlist = _find_playlist(owner, resource)
            return str(playlist.get("name") or "") if playlist else ""
        if kind == "path":
            return os.path.basename(resource.rstrip(os.sep)) or resource
    except Exception:  # noqa: BLE001 — a corrupt store must not break sharing
        logger.warning("Could not resolve sharing label for %s/%s/%s",
                       owner, kind, resource, exc_info=True)
    return ""


def paths(owner: str, kind: str, resource: str) -> set[str]:
    """Paths covered by a grant (empty when the resource disappeared)."""
    try:
        if kind == "path":
            return {resource} if resource else set()
        if kind == "album":
            album = _find_album(owner, resource)
            if not album:
                return set()
            return {str(p) for p in (album.get("paths") or []) if isinstance(p, str) and p}
        if kind == "playlist":
            playlist = _find_playlist(owner, resource)
            if not playlist:
                return set()
            out: set[str] = set()
            for track in (playlist.get("tracks") or []):
                if not isinstance(track, dict):
                    continue
                resolved = local_track_path(owner, track.get("file_url") or "")
                if resolved:
                    out.add(resolved)
            return out
    except Exception:  # noqa: BLE001 — one corrupt resource must not widen access
        logger.warning("Could not resolve sharing paths for %s/%s/%s",
                       owner, kind, resource, exc_info=True)
    return set()


def shareable_paths(owner: str, kind: str, resource: str) -> set[str]:
    """Resolved paths that pass the shareable-path rule."""
    return {path for path in paths(owner, kind, resource)
            if is_shareable_path(path)}
