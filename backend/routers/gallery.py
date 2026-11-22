"""EthOS NAS — Gallery Router (v4, timeline-style indexing)

Library model:
- Configured folders (any allowed location) define the media library.
- A persistent SQLite index (backend/data/gallery_index.db) is maintained by
  background scans. The first scan walks everything; every later scan is
  *incremental* — only new or changed files (size+mtime diff) are re-read and
  EXIF-extracted, deleted files are dropped. Rescans on big libraries stay fast.
- Images get their EXIF "taken" date at index time (DateTimeOriginal →
  DateTime fallback), so sorting/grouping follows when the photo was taken,
  not file mtime — same behaviour as the current Photos apps. Videos use
  file time. The in-memory copy of the index survives restarts and is
  refreshed lazily when configured folders change or on explicit POST /rescan.
- The grid data API (GET /items) serves paginated, sorted, filtered results
  straight from the index — fast even with hundreds of thousands of files.

REST:
- GET  /api/gallery/config          — saved folder list + display options
- POST /api/gallery/config          — save folder list + display options
- GET  /api/gallery/mounts            — top-level directories (sidebar mount points)
- GET  /api/gallery/browse?path=... — list subdirectories for the folder picker
- GET  /api/gallery/rescan-status   — index size, scanning flag, last scan time
- POST /api/gallery/rescan          — trigger background rescan (idempotent)
- GET  /api/gallery/items           — paginated media items (the main data API;
                                       private items hidden unless view=private,
                                       which requires a valid Private session)
- POST /api/gallery/private         — flag items private/public; making private
                                       encrypts the file at rest, making public
                                       decrypts and restores it
- GET  /api/gallery/scan?paths=...  — legacy one-shot scan (compat with v1 UI)

Private section security (PIN + at-rest encryption):
- POST /api/gallery/private-pin         — set a new PIN (first time) or change it;
                                          both re-key all encrypted private files
- GET  /api/gallery/private-pin/status  — whether a PIN is configured
- POST /api/gallery/private-pin/verify  — check PIN, issue a short-lived Private
                                          session token (ps_token)
- GET  /api/gallery/private-media       — decrypted bytes of one private item
                                          (session-gated; Range-capable via temp file)
- GET  /api/gallery/private-thumb       — on-the-fly WebP thumbnail (in-memory cache only)
- GET  /api/gallery/private-download    — the ENCRYPTED blob as an attachment, so a
                                          downloaded copy of a private item is useless
"""

# ── Module map — grep a section name, then read only that slice ─────────────
# Config persistence   gallery_config.json
# Private section      PIN, at-rest encryption, session gates
# Index                in-memory items, incremental rescan (size+mtime)
# Index persistence    SQLite gallery_index.db + EXIF extraction
# Items data API       pagination / sort / filter helpers for GET /items
# Endpoints            /api/gallery/*
# Private PIN          unlock/lock + session-gated media
# Albums               albums.json named path collections
# Per-file metadata    on-demand EXIF for the details panel

import asyncio
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import secrets
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from PIL import Image
from starlette.background import BackgroundTask

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Shared path sanitizer + video-thumb helpers from the File Manager router
from backend.config import settings
from backend.routers.files import _sanitize, _extract_video_frame, _video_fallback_svg
# JWT payload decoder (to bind Private sessions to the logged-in user)
from backend.routers.auth import _verify_token
from backend.services import json_store, path_scope, sharing_service, user_data
from backend.services import trash_service

logger = logging.getLogger("ethos-nas.routers.gallery")

router = APIRouter(dependencies=[Depends(path_scope.scope_request)])

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".flv"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

DEFAULT_CONFIG = {
    "folders": [],
    "options": {"sort": "date_desc", "include_videos": True, "grid_size": "medium"},
    "private_pin": {},   # {salt, hash, enc_salt} — see Private PIN section below
}


@dataclass
class _Library:
    """One account's gallery: config, albums, index DB and in-memory state."""
    username: str
    config_store: json_store.Store
    albums_store: json_store.Store
    db_path: Path
    state: dict = field(default_factory=lambda: {
        "items": [],          # dicts: path,name,folder,size,modified,type,taken
        "scanned_at": 0.0,    # epoch of last finished scan
        "scanning": False,    # background walk in flight?
        "progress": {"dirs": 0, "added": 0, "changed": 0, "removed": 0},
    })
    loaded_config_signature: object | None = None
    last_auto_rescan_at: float = 0.0


_LIBRARIES: dict[str, _Library] = {}
_LIBRARIES_LOCK = threading.Lock()


def _library(username: str | None = None) -> _Library:
    """The caller's gallery library (created and loaded on first use)."""
    name = user_data.safe_username(username or user_data.current_username())
    with _LIBRARIES_LOCK:
        lib = _LIBRARIES.get(name)
        if lib is None:
            lib = _Library(
                username=name,
                config_store=user_data.user_store(
                    "gallery_config.json", dict, version=1, username=name),
                albums_store=user_data.user_store(
                    "albums.json", list, username=name),
                db_path=user_data.user_path("gallery_index.db", name),
            )
            _LIBRARIES[name] = lib
            _load_library_index(lib)
        return lib


def _load_config_from(store: json_store.Store) -> dict:
    """Read a gallery config store; a corrupt file is quarantined by json_store."""
    data = store.load()
    try:
        if not data:
            raise ValueError("empty config")
        if not isinstance(data, dict):
            raise ValueError("config root is not an object")
        folders = data.get("folders") or []
        options = data.get("options") or {}
        pin = data.get("private_pin") or {}
        return {
            "folders": [f for f in folders if isinstance(f, dict) and f.get("path")],
            "options": {**DEFAULT_CONFIG["options"], **{k: v for k, v in options.items()
                                                        if k in DEFAULT_CONFIG["options"]}},
            "private_pin": {k: pin[k] for k in ("salt", "hash", "enc_salt")
                            if isinstance(pin, dict) and isinstance(pin.get(k), str)},
        }
    except Exception as exc:
        logger.warning("Gallery config unusable (%s) — using defaults", exc)
        return json.loads(json.dumps(DEFAULT_CONFIG))


def _load_config() -> dict:
    return _load_config_from(_library().config_store)


def _save_config(config: dict) -> None:
    _library().config_store.save(config)


def _is_allowed(abs_path: str) -> bool:
    """True when the caller's own path scope accepts `abs_path`."""
    try:
        path_scope.sanitize(abs_path)
        return True
    except HTTPException:
        return False


def _is_allowed_read(abs_path: str) -> bool:
    """Own scope or a received share — for read-only gallery scans."""
    try:
        path_scope.sanitize_read(abs_path)
        return True
    except HTTPException:
        return False


# ── Private section: PIN + at-rest encryption ───────────────────────────────
#
# A user-chosen PIN (min 6 chars) protects the Private section on top of the
# normal login. Only a PBKDF2 verification hash is persisted — never the PIN.
# The AES-256-GCM key for file encryption is derived from the same PIN with a
# separate salt, so there is no stored key and no recovery path: forgetting
# the PIN makes encrypted private items unrecoverable (deliberate).
#
# Files flagged private are re-encrypted in place to <path>.ethos.enc:
#   ETHOS-ENC1\x00 || nonce(12) || AESGCM ciphertext+tag
# Displaying them requires a short-lived Private session (ps_token, 30 min,
# in-memory only, bound to the logged-in user). Downloads of private items
# serve the ciphertext blob — a copied file cannot be shown without the PIN.

PIN_MIN_LEN = 6
PBKDF2_ITERATIONS = 600_000          # OWASP recommendation for PBKDF2-SHA256
ENC_MAGIC = b"ETHOS-ENC1\x00"
ENC_SUFFIX = ".ethos.enc"
MAX_ENCRYPT_SIZE = 512 * 1024 * 1024  # refuse to encrypt files larger than this
SESSION_TTL = 1800                    # Private session lifetime (seconds)

_priv_sessions: dict[str, dict] = {}  # ps_token → {user, exp, key}
_PRIV_TMP_DIR = settings.DATA_DIR / ".privtmp"
_priv_thumb_cache: dict[tuple, tuple] = {}   # (path,size) → (mtime,size,webp bytes)


def _pbkdf2(pin: str, salt_hex: str, dklen: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex),
        PBKDF2_ITERATIONS, dklen=dklen)


def _pin_hash(pin: str, salt_hex: str) -> str:
    return _pbkdf2(pin, salt_hex, 32).hex()


def _enc_key(pin: str, enc_salt_hex: str) -> bytes:
    """AES-256 key for file encryption (separate derivation from the hash)."""
    return _pbkdf2(pin, enc_salt_hex, 32)


def _encrypt_bytes(key: bytes, data: bytes) -> bytes:
    nonce = os.urandom(12)
    return ENC_MAGIC + nonce + AESGCM(key).encrypt(nonce, data, None)


def _decrypt_bytes(key: bytes, blob: bytes) -> bytes:
    if not blob.startswith(ENC_MAGIC):
        raise ValueError("not an EthOS-encrypted file")
    off = len(ENC_MAGIC)
    nonce, ct = blob[off:off + 12], blob[off + 12:]
    return AESGCM(key).decrypt(nonce, ct, None)   # InvalidTag on wrong key/corrupt


def _encrypt_file(path: str, key: bytes) -> None:
    """Encrypt <path> in place → <path>.ethos.enc (atomic; original removed)."""
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("not a regular file")
    if st.st_size > MAX_ENCRYPT_SIZE:
        raise ValueError(f"file too large to encrypt (> {MAX_ENCRYPT_SIZE // (1024 * 1024)} MiB)")
    with open(path, "rb") as f:
        data = f.read()
    blob = _encrypt_bytes(key, data)
    tmp = path + ENC_SUFFIX + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path + ENC_SUFFIX)
    os.remove(path)


def _decrypt_file(enc_path: str, key: bytes) -> None:
    """Decrypt <path>.ethos.enc back to the original (atomic)."""
    path = enc_path[: -len(ENC_SUFFIX)]
    with open(enc_path, "rb") as f:
        blob = f.read()
    data = _decrypt_bytes(key, blob)
    tmp = path + ".dec.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    os.remove(enc_path)


def _reencrypt_file(enc_path: str, old_key: bytes, new_key: bytes) -> None:
    """Re-key one encrypted file (PIN change)."""
    with open(enc_path, "rb") as f:
        blob = f.read()
    data = _decrypt_bytes(old_key, blob)
    tmp = enc_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(_encrypt_bytes(new_key, data))
    os.replace(tmp, enc_path)


def _request_user(request: Request) -> str:
    """Username of the authenticated caller (JWT via header/cookie/query).

    The auth middleware already validated the token; this only re-decodes it.
    """
    token = None
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
    else:
        for value in request.cookies.values():
            if isinstance(value, str) and value.startswith("eyJ"):
                token = value
                break
        if not token:
            token = (request.query_params.get("token") or "").strip()
    payload = _verify_token(token or "")
    return payload["username"] if payload else ""


def _issue_session(username: str, key: bytes) -> str:
    now = time.time()
    for t in [t for t, v in _priv_sessions.items() if v["exp"] < now]:
        del _priv_sessions[t]
    token = secrets.token_urlsafe(32)
    _priv_sessions[token] = {"user": username, "exp": now + SESSION_TTL, "key": key}
    return token


def _session_for(token: str, username: str) -> Optional[dict]:
    v = _priv_sessions.get(token or "")
    if not v or v["user"] != username or v["exp"] < time.time():
        return None
    return v


def _resolve_priv_key(request: Request, pin: str, ps_token: str) -> tuple:
    """Get the AES key for a privacy operation.

    Accepts either an active Private session (ps token) or the PIN itself in
    the request body. Returns (key, error): error is '' | 'pin_not_set' |
    'no_session' | 'unauthenticated'.
    """
    user = _request_user(request)
    if not user:
        return None, "unauthenticated"
    sess = _session_for(ps_token, user)
    if sess is not None:
        return sess["key"], ""
    pin_cfg = _load_config().get("private_pin") or {}
    stored_hash = pin_cfg.get("hash", "")
    if pin and stored_hash:
        if hmac.compare_digest(_pin_hash(pin, pin_cfg["salt"]), stored_hash):
            return _enc_key(pin, pin_cfg["enc_salt"]), ""
    if not stored_hash:
        return None, "pin_not_set"
    return None, "no_session"


def _require_priv_session(request: Request, ps_token: str) -> tuple[str, bytes]:
    user = _request_user(request)
    sess = _session_for(ps_token, user) if user else None
    if not sess:
        raise HTTPException(status_code=403, detail="pin_required")
    return user, sess["key"]


def _private_item_path(path: str) -> str:
    """Validate that `path` is an indexed private item; return its abs path."""
    ap = os.path.abspath(os.path.expanduser(path or ""))
    for it in _index_state["items"]:
        if it.get("private") and it["path"] == ap:
            return ap
    raise HTTPException(status_code=404, detail="Not a private item")


def _decrypt_to_tmp(enc: str, key: bytes, orig: str) -> str:
    """Decrypt an encrypted file to a 0600 temp file (Range-capable serving)."""
    _PRIV_TMP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(_PRIV_TMP_DIR, 0o700)
    with open(enc, "rb") as f:
        blob = f.read()
    data = _decrypt_bytes(key, blob)   # may raise InvalidTag
    ext = os.path.splitext(orig)[1] or ".bin"
    tmp = _PRIV_TMP_DIR / (hashlib.sha256(orig.encode()).hexdigest() + ext)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return str(tmp)


def _rm_quiet(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass  # best-effort cleanup — nothing to report


def _private_image_thumb(enc: str, key: bytes, size: int) -> bytes:
    """Decrypt an image in memory → square WebP thumbnail (no disk cache)."""
    import io
    with open(enc, "rb") as f:
        blob = f.read()
    data = _decrypt_bytes(key, blob)
    with Image.open(io.BytesIO(data)) as img:
        if img.mode not in ("RGB", "RGBA", "P", "L"):
            img = img.convert("RGBA")
        img.thumbnail((size, size), Image.Resampling.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=85)
        return buf.getvalue()


def _private_video_thumb(enc: str, key: bytes, orig: str, size: int) -> Optional[bytes]:
    """Decrypt a video to a temp file, extract one frame via ffmpeg (files.py),
    read the WebP back into memory, then delete all plaintext artifacts."""
    tmp = _decrypt_to_tmp(enc, key, orig)
    out = _PRIV_TMP_DIR / (hashlib.sha256(f"vt:{orig}:{size}".encode()).hexdigest() + ".webp")
    try:
        ok = _extract_video_frame(tmp, size, out)
        if not ok or not out.exists():
            return None
        with open(out, "rb") as f:
            return f.read()
    finally:
        _rm_quiet(tmp)
        _rm_quiet(str(out))


# ── Index (in-memory, persist on rescan) ─────────────────────────────────────

_INDEX_LOCK = asyncio.Lock()
AUTO_RESCAN_MIN_INTERVAL = 30.0


class _IndexState:
    """Per-identity view of the current account's in-memory index state."""

    def __getitem__(self, key):
        return _library().state[key]

    def __setitem__(self, key, value):
        _library().state[key] = value

    def get(self, key, default=None):
        return _library().state.get(key, default)


_index_state = _IndexState()


def _config_signature(cfg: dict):
    return (tuple(f["path"] for f in cfg["folders"]), bool(cfg["options"].get("include_videos", True)))


# ── Index persistence (SQLite) + EXIF extraction ─────────────────────────────


def _db_path() -> Path:
    return _library().db_path


def _db_connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a media index database, creating the schema if needed."""
    target = Path(path) if path is not None else _db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=15.0)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS media (
               path     TEXT PRIMARY KEY,
               name     TEXT NOT NULL,
               folder   TEXT NOT NULL,
               size     INTEGER NOT NULL,
               mtime    REAL NOT NULL,
               type     TEXT NOT NULL,
               taken_at REAL
           )"""
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    # Migration for pre-existing databases: per-item privacy flag.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(media)")}
    if "private" not in cols:
        conn.execute("ALTER TABLE media ADD COLUMN private INTEGER NOT NULL DEFAULT 0")
    # Migration for pre-existing databases: per-item favorite flag.
    if "favorite" not in cols:
        conn.execute("ALTER TABLE media ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
    # Migration for pre-existing databases: camera make/model (search, P0.9).
    if "camera" not in cols:
        conn.execute("ALTER TABLE media ADD COLUMN camera TEXT NOT NULL DEFAULT ''")
    # Migration: GPS coordinates (EXIF GPS IFD, captured at scan time).
    if "lat" not in cols:
        conn.execute("ALTER TABLE media ADD COLUMN lat REAL")
    if "lon" not in cols:
        conn.execute("ALTER TABLE media ADD COLUMN lon REAL")
    # Migration: video duration in seconds (ffprobe at scan time).
    if "duration" not in cols:
        conn.execute("ALTER TABLE media ADD COLUMN duration REAL")
    return conn


def _load_library_index(lib: _Library) -> None:
    """Best-effort load of one account's persisted index."""
    try:
        if lib.db_path.exists():
            conn = _db_connect(lib.db_path)
            try:
                rows = conn.execute(
                    "SELECT path,name,folder,size,mtime,type,taken_at,private,favorite,camera,"
                    "lat,lon,duration FROM media"
                ).fetchall()
                meta = conn.execute(
                    "SELECT value FROM meta WHERE key='scanned_at'").fetchone()
            finally:
                conn.close()
            lib.state["items"] = [
                {"path": p, "name": n, "folder": f, "size": s,
                 "modified": m, "type": t, "taken": ta, "private": bool(pv),
                 "favorite": bool(fv), "camera": cam or "",
                 "lat": lat, "lon": lon, "duration": dur}
                for (p, n, f, s, m, t, ta, pv, fv, cam, lat, lon, dur) in rows
            ]
            if meta:
                lib.state["scanned_at"] = float(meta[0])
    except Exception as exc:
        logger.warning("Gallery index unreadable for %s (%s) — starting empty",
                       lib.username, exc)
    lib.loaded_config_signature = _config_signature(_load_config_from(lib.config_store))


def _gps_decimal(gps_ifd):
    """GPS IFD → (lat, lon) in decimal degrees; None when absent/unreadable."""
    def _rat(v):
        """Fraction | tuple(n, d) | int/float → float."""
        if isinstance(v, (int, float)):
            return float(v)
        if hasattr(v, 'numerator'):          # Fraction / IFDRational
            return float(v)
        if isinstance(v, tuple) and len(v) == 2:
            return float(v[0]) / float(v[1]) if v[1] else 0.0
        return None

    def conv(val):
        try:
            d, m, s = _rat(val[0]), _rat(val[1]), _rat(val[2])
            if d is None or m is None or s is None:
                return None
            return d + m / 60.0 + s / 3600.0
        except Exception:
            return None

    lat = lon = None
    try:
        lv, lr = gps_ifd.get(2), str(gps_ifd.get(1) or 'N')
        if lv is not None:
            v = conv(lv)
            if v is not None and lr.upper().startswith('S'): v = -v
            lat = v
        vv, vr = gps_ifd.get(4), str(gps_ifd.get(3) or 'E')
        if vv is not None:
            v = conv(vv)
            if v is not None and vr.upper().startswith('W'): v = -v
            lon = v
    except Exception:
        pass  # intentional: a failure here must not break the caller
    return lat, lon


# EXIF date tags: DateTimeOriginal (36867), then plain DateTime (306).
_EXIF_DATE_TAGS = (36867, 306)


def _parse_exif_datetime(raw: str) -> Optional[float]:
    """'YYYY:MM:DD HH:MM:SS' camera-local time → epoch seconds."""
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw.strip(), fmt).timestamp()
        except ValueError:
            continue
    return None


def _extract_camera(exif) -> Optional[str]:
    """'Make Model' from EXIF tags 271/272; None when absent.

    Some cameras pad these strings with NUL bytes — strip those and collapse
    whitespace so search matches cleanly.
    """
    try:
        parts = []
        for p in (exif.get(271), exif.get(272)):
            if not isinstance(p, str):
                continue
            clean = " ".join(p.replace("\x00", " ").split())
            if clean:
                parts.append(clean)
        return " ".join(parts) or None
    except Exception:
        return None


def _extract_exif_meta(path: str):
    """EXIF capture date + camera string from a single open.

    Returns (taken_at|None, camera|None). Only called for new/changed files
    during a scan, so the cost is paid once per file lifetime — the same
    trade-off the timeline UI makes.
    """
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            taken = None
            sources: list = [exif]  # Exif IFDs (main + Exif sub-IFD)
            try:  # DateTimeOriginal lives in the Exif sub-IFD on most cameras
                sub = exif.get_ifd(0x8769)
                if sub:
                    sources.append(sub)
            except Exception:
                pass  # intentional: a failure here must not break the caller
            for src in sources:
                for tag in _EXIF_DATE_TAGS:
                    try:
                        raw = src.get(tag)
                    except Exception:
                        raw = None
                    if isinstance(raw, str):
                        ts = _parse_exif_datetime(raw)
                        if ts is not None:
                            taken = ts
                            break
                if taken is not None:
                    break
            gps = None
            try:  # GPS lives in the GPS sub-IFD (0x8825)
                gps_ifd = exif.get_ifd(0x8825)
                if gps_ifd is not None:
                    gps = _gps_decimal(gps_ifd)
            except Exception:
                pass  # intentional: a failure here must not break the caller
            return taken, _extract_camera(exif), gps
    except Exception:
        pass  # unreadable/corrupt EXIF → caller falls back to mtime
    return None, None, None


def _extract_taken_at(path: str) -> Optional[float]:
    """EXIF capture date of an image → epoch (local time); None if absent."""
    taken, _cam, _gps = _extract_exif_meta(path)
    return taken


_DURATION_CACHE: dict = {}  # (path, mtime) → seconds; ffprobe is expensive


def _probe_duration(path: str, mtime: float) -> Optional[float]:
    """Video duration via ffprobe (best-effort, cached per path+mtime)."""
    key = (path, mtime)
    if key in _DURATION_CACHE:
        return _DURATION_CACHE[key]
    dur = None
    try:
        import subprocess
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0 and out.stdout.strip():
            dur = round(float(out.stdout.strip()), 2)
    except Exception:
        pass  # intentional: a failure here must not break the caller
    _DURATION_CACHE[key] = dur
    if len(_DURATION_CACHE) > 5000:
        _DURATION_CACHE.clear()
        _DURATION_CACHE[key] = dur
    return dur


def _scan_root(conn: sqlite3.Connection, root: str, progress: dict) -> set:
    """Incrementally index one configured folder (thread-pool safe).

    New/changed files are upserted (images get their EXIF taken date);
    unchanged files keep their existing row untouched. Returns the set of
    media paths still present under root, used for orphan cleanup.
    """
    cur = conn.cursor()
    prefix = root.rstrip("/") + "/"
    existing = {}
    try:
        for path, size, mtime, priv in cur.execute(
                "SELECT path,size,mtime,private FROM media WHERE path=? OR path LIKE ?",
                (root, prefix + "%")):
            existing[path] = (size, mtime, bool(priv))
    except sqlite3.Error as exc:
        logger.warning("Gallery index read failed for %s: %s", root, exc)

    seen = set()
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            progress["dirs"] += 1
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and not os.path.islink(os.path.join(dirpath, d))]
            for fname in filenames:
                if fname.startswith("."):
                    continue
                # Encrypted private items live as <name>.ethos.enc — never index
                # them as media, but keep their (original-path) row alive so the
                # orphan cleanup below doesn't drop it on every rescan.
                if fname.endswith(ENC_SUFFIX):
                    base = fname[: -len(ENC_SUFFIX)]
                    bext = "." + base.rsplit(".", 1)[-1].lower() if "." in base else ""
                    if bext not in MEDIA_EXTS:
                        continue
                    enc_path = os.path.join(dirpath, fname)
                    orig = os.path.join(dirpath, base)
                    seen.add(orig)
                    # Self-heal: an encrypted file must be a private item.
                    # If its index row was lost (older indexer version / folder
                    # reconfiguration) restore it; if the row exists but lost
                    # its flag, set it — otherwise the photo is invisible in
                    # the Private section and broken in the normal gallery.
                    entry = existing.get(orig)
                    if entry is None:
                        try:
                            st = os.stat(enc_path)
                        except OSError:
                            continue
                        cur.execute(
                            """INSERT INTO media(path,name,folder,size,mtime,type,taken_at,camera,private)
                               VALUES(?,?,?,?,?,?,?, ?,1)""",
                            (orig, base, dirpath, int(st.st_size),
                             round(float(st.st_mtime), 3),
                             "video" if bext in VIDEO_EXTS else "image", None, ""))
                        progress["added"] += 1
                    elif not entry[2]:
                        cur.execute("UPDATE media SET private=1 WHERE path=?", (orig,))
                        progress["changed"] += 1
                    continue
                ext = "." + fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                if ext not in MEDIA_EXTS:
                    continue
                full_path = os.path.join(dirpath, fname)
                try:
                    st = os.stat(full_path)
                except OSError:
                    continue
                seen.add(full_path)
                size = int(st.st_size)
                mtime = round(float(st.st_mtime), 3)
                entry = existing.get(full_path)
                if entry is not None and entry[0] == size and entry[1] == mtime:
                    continue  # unchanged — keep indexed row (and its EXIF date)
                if ext in IMAGE_EXTS:
                    taken, camera, gps = _extract_exif_meta(full_path)
                    lat, lon = gps if gps else (None, None)
                else:
                    taken, camera, lat, lon = None, None, None, None
                dur = _probe_duration(full_path, mtime) if ext in VIDEO_EXTS else None
                cur.execute(
                    """INSERT INTO media(path,name,folder,size,mtime,type,taken_at,camera,lat,lon,duration)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(path) DO UPDATE SET
                         name=excluded.name, folder=excluded.folder,
                         size=excluded.size, mtime=excluded.mtime,
                         type=excluded.type, taken_at=excluded.taken_at,
                         camera=excluded.camera, lat=excluded.lat,
                         lon=excluded.lon, duration=excluded.duration""",
                    (full_path, fname, dirpath, size, mtime,
                     "video" if ext in VIDEO_EXTS else "image", taken,
                     camera or "", lat, lon, dur))
                progress["added" if full_path not in existing else "changed"] += 1
    except PermissionError as exc:
        logger.warning("Gallery scan permission denied on %s: %s", root, exc)
    return seen


def _do_scan(cfg: dict, username: str, role: str) -> None:
    """Incremental scan of one account's folders (runs in a worker thread).

    Incremental: upsert new/changed files, drop deleted ones, then
    re-materialise that account's in-memory index from its SQLite catalog.
    The worker carries the owner's identity so the path scope matches the
    folders that account configured.
    """
    with path_scope.identity_scope(username, role):
        _do_scan_scoped(cfg)


def _do_scan_scoped(cfg: dict) -> None:
    progress = _index_state["progress"]
    conn = _db_connect()
    try:
        seen_all = set()
        for folder in cfg["folders"]:
            root = folder["path"]
            if not _is_allowed(root) or not os.path.isdir(root):
                continue
            seen_all |= _scan_root(conn, root, progress)
        # Drop rows that vanished from disk. Only consider rows under roots
        # that were actually walked this pass — a root that is missing from
        # disk this scan (e.g. transient NFS/USB glitch) must not cause every
        # row it previously contributed to be treated as deleted.
        walked = [f["path"].rstrip("/") + "/" for f in cfg["folders"]
                  if _is_allowed(f["path"]) and os.path.isdir(f["path"])]
        if walked:
            cur = conn.cursor()
            like = " OR ".join(["path LIKE ?"] * len(walked))
            cand = cur.execute(
                f"SELECT path FROM media WHERE {like}", walked).fetchall()
            stale = [p for (p,) in cand if p not in seen_all]
            if stale:
                cur.executemany("DELETE FROM media WHERE path=?",
                                [(p,) for p in stale])
                progress["removed"] += len(stale)
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('scanned_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(time.time()),))
        conn.commit()
        # conn (not the `cur` cursor above) — `cur` is only created when at
        # least one folder was walked; a rescan with zero walkable folders
        # (none configured, or all missing from disk) must still complete.
        rows = conn.execute(
            """SELECT path,name,folder,size,mtime,type,taken_at,private,favorite,camera,
                      lat,lon,duration
               FROM media ORDER BY COALESCE(taken_at, mtime) DESC"""
        ).fetchall()
    finally:
        conn.close()
    _index_state["items"] = [
        {"path": p, "name": n, "folder": f, "size": s,
         "modified": m, "type": t, "taken": ta, "private": bool(pv),
         "favorite": bool(fv), "camera": cam or "",
         "lat": lat, "lon": lon, "duration": dur}
        for (p, n, f, s, m, t, ta, pv, fv, cam, lat, lon, dur) in rows
    ]
    _index_state["scanned_at"] = time.time()


async def _run_rescan() -> None:
    """Background incremental rescan of the caller's configured folders."""
    username, role = path_scope.current_identity()
    name = user_data.safe_username(username)
    lib = _library(name)
    cfg = _load_config_from(lib.config_store)
    sig = _config_signature(cfg)
    async with _INDEX_LOCK:
        if lib.state["scanning"]:
            return
        lib.state["scanning"] = True
        lib.state["progress"] = {"dirs": 0, "added": 0, "changed": 0, "removed": 0}
    try:
        await asyncio.get_running_loop().run_in_executor(None, _do_scan, cfg, name, role)
        lib.loaded_config_signature = sig
        logger.info("Gallery rescan complete for %s: %d items", name, len(lib.state["items"]))
    except Exception as exc:
        logger.error("Gallery rescan failed for %s: %s", name, exc)
    finally:
        async with _INDEX_LOCK:
            lib.state["scanning"] = False


def _ensure_fresh_index(force: bool = False) -> None:
    """Kick a background rescan if the index is missing/stale (fire and forget)."""
    lib = _library()
    cfg = _load_config_from(lib.config_store)
    sig = _config_signature(cfg)
    config_changed = lib.loaded_config_signature != sig
    empty_library = len(lib.state["items"]) == 0 and bool(cfg["folders"])
    if not (config_changed or empty_library or force):
        return
    if not cfg["folders"]:
        return
    if lib.state["scanning"]:
        return
    # An empty library would otherwise re-trigger a full walk on every /items
    # request — throttle auto-rescans to at most one per interval.
    now = time.time()
    if not force and not config_changed \
            and (now - lib.last_auto_rescan_at) < AUTO_RESCAN_MIN_INTERVAL:
        return
    lib.last_auto_rescan_at = now
    try:
        asyncio.get_running_loop().create_task(_run_rescan())
    except RuntimeError:
        pass  # no event loop context — caller triggered an explicit scan


# ── Items data API helpers ───────────────────────────────────────────────────

def _norm_dir(path: str) -> str:
    """Absolute path without a trailing separator ('/' stays '/')."""
    ap = os.path.abspath(os.path.expanduser(path or "/"))
    return ap if ap == "/" else ap.rstrip(os.sep)


def _top_folders(items: List[dict], roots: List[str], base: str = "") -> List[dict]:
    """Immediate child folders that contain media (facet chips).

    `base` empty → children of the configured roots (top-level library view).
    `base` set → children of that folder, so facets drill down correctly at
    any folder depth (previously every view re-listed the roots' children,
    which made sub-folder navigation point back at itself).  A '.' entry means
    media sits directly in `base`.

    Single O(n × len(bases)) pass — keeps the endpoint fast on big libraries.
    """
    bases = [_norm_dir(base)] if base else [_norm_dir(r) for r in roots]
    counts = {}   # (base_idx, child_lower) -> [count, display_name]
    for it in items:
        # Index paths are already absolute — avoid os.path.abspath per item on
        # 10k+ libraries (it dominated the endpoint's request time).
        folder = it["folder"].rstrip(os.sep) or os.sep
        for i, root in enumerate(bases):
            root_key = root.rstrip(os.sep) or os.sep
            if folder == root_key:
                child = "."
            else:
                prefix = root_key if root_key.endswith(os.sep) else root_key + os.sep
                if not folder.startswith(prefix):
                    continue
                # Slice instead of os.path.relpath — relpath normalises the
                # whole path on every call and dominated request time.
                child = folder[len(prefix):].split(os.sep)[0]
            key = (i, child.lower())
            entry = counts.get(key)
            if entry is None:
                counts[key] = [1, child]
            else:
                entry[0] += 1
            break
    out: List[dict] = []
    for i, root in enumerate(bases):
        kids = {cl: v for (ri, cl), v in counts.items() if ri == i}
        for cl in sorted(kids, key=str.lower):
            count, display = kids[cl]
            full = os.path.join(root, display) if display != "." else root
            out.append({"path": full, "name": display, "count": count})
    return out


# ── Endpoints ─────────────────────────────────────────────────────────────────

def _public_config(cfg: dict) -> dict:
    """Config for API responses — never expose the PIN salt/hash to clients."""
    out = {**cfg}
    pin = cfg.get("private_pin") or {}
    out["private_pin"] = {"set": bool(pin.get("hash"))}
    return out


@router.get("/config")
async def gallery_get_config():
    """Return the saved gallery configuration (folders + display options)."""
    return _public_config(_load_config())


@router.post("/config")
async def gallery_save_config(payload: dict = Body(...)):
    """Save gallery folders and/or display options.

    Either key may be omitted to keep the current value. Folder paths are
    validated against the caller's path scope; invalid entries are dropped.
    Saving a changed folder set marks the index stale → next data call rescans.
    """
    cfg = _load_config()

    if "folders" in payload and isinstance(payload["folders"], list):
        clean = []
        seen = set()
        for entry in payload["folders"]:
            raw = entry.get("path") if isinstance(entry, dict) else (entry if isinstance(entry, str) else None)
            if not raw:
                continue
            try:
                abs_path = path_scope.sanitize(str(raw))
            except HTTPException:
                logger.warning("Gallery config dropped out-of-scope folder: %r", raw)
                continue
            if not os.path.isdir(abs_path):
                logger.warning("Gallery config dropped invalid folder: %r", raw)
                continue
            if abs_path in seen:
                continue
            seen.add(abs_path)
            clean.append({"path": abs_path, "name": os.path.basename(abs_path.rstrip("/")) or "/"})
        cfg["folders"] = clean

    if "options" in payload and isinstance(payload["options"], dict):
        for key in DEFAULT_CONFIG["options"]:
            if key in payload["options"]:
                cfg["options"][key] = payload["options"][key]

    _save_config(cfg)
    # Folder set or media scope changed → force rescan so the index matches.
    # Fire-and-forget: a full walk can take minutes on big libraries and must
    # not hold this request open (_run_rescan is idempotent under its lock).
    if ("folders" in payload and isinstance(payload["folders"], list)
            or "include_videos" in (payload.get("options") or {})):
        asyncio.get_running_loop().create_task(_run_rescan())
    return {"ok": True, **_public_config(cfg)}


@router.get("/mounts")
async def gallery_mounts():
    """List top-level directories under / (mount points) for the sidebar.

    Returns [{path, name}] — system/pseudo filesystems are excluded.
    """
    skip = {"proc", "sys", "dev", "run", "boot", "efi", "snap", "lost+found"}
    username, role = path_scope.current_identity()

    def _mounts() -> List[dict]:
        out: List[dict] = []
        if role != "admin":
            # Non-admins only see the roots they can actually browse.
            for root in path_scope.allowed_roots(username, role):
                if os.path.isdir(root):
                    out.append({"path": root, "name": os.path.basename(root) or root})
            return out
        try:
            entries = sorted(os.listdir("/"), key=str.lower)
        except PermissionError:
            return out
        for name in entries:
            if name.startswith(".") or name in skip:
                continue
            full = f"/{name}"
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode):
                continue
            if not _is_allowed(full):
                continue
            out.append({"path": full, "name": name})
        return out

    # /proc and /sys make os.listdir + stat on "/" slow enough to be visible.
    return await asyncio.to_thread(_mounts)


@router.get("/browse")
async def gallery_browse(
    path: str = Query(default="/", description="Directory to browse for subfolders"),
):
    """List immediate subdirectories of a given path (public — no file contents)."""
    abs_path = os.path.abspath(os.path.expanduser(path or "/"))
    _username, role = path_scope.current_identity()

    if abs_path == "/" and role != "admin":
        return []  # non-admins start at their own roots, never the filesystem root
    if not _is_allowed(abs_path) and abs_path != "/":
        return []
    if not os.path.isdir(abs_path):
        return []

    def _subdirs() -> List[dict]:
        try:
            entries = sorted(os.listdir(abs_path), key=str.lower)
        except PermissionError:
            return []
        out = []
        for name in entries:
            if name.startswith("."):
                continue
            full = os.path.join(abs_path, name)
            if not os.path.isdir(full):
                continue
            if abs_path == "/" and not _is_allowed(full):
                continue
            out.append({"name": name, "path": full})
        return out

    return await asyncio.to_thread(_subdirs)


@router.get("/rescan-status")
async def gallery_rescan_status():
    """Index size / scanning flag — lightweight UI status indicator."""
    cfg = _load_config()
    items = _index_state["items"] or []
    return {
        "items": len(items),
        "private_items": sum(1 for it in items if it.get("private")),
        "favorites": sum(1 for it in items if it.get("favorite")),
        "scanning": bool(_index_state["scanning"]),
        "dirs_walked": _index_state["progress"]["dirs"],
        "added": _index_state["progress"].get("added", 0),
        "changed": _index_state["progress"].get("changed", 0),
        "removed": _index_state["progress"].get("removed", 0),
        "scanned_at": _index_state["scanned_at"],
        "folders": [f["path"] for f in cfg["folders"]],
    }


@router.post("/rescan")
async def gallery_rescan():
    """Start (or report existing) background rescan of all configured folders."""
    async with _INDEX_LOCK:
        if not _index_state["scanning"]:
            asyncio.get_running_loop().create_task(_run_rescan())
    return {"ok": True, "accepted": len(_load_config()["folders"])}


class GalleryTrashRequest(BaseModel):
    paths: list[str] = []


@router.post("/trash")
async def gallery_trash(body: GalleryTrashRequest, request: Request) -> dict:
    """Move selected media files into the caller's trash (restorable from Files).

    Only regular files with an image/video extension that live inside a
    configured library folder are accepted — this is deliberately stricter
    than the generic /api/files/trash endpoint. Ownership comes from the JWT so
    Photos deletions land in the deleting account's own recycle bin.
    """
    cfg = _load_config()
    roots = [f["path"] for f in cfg.get("folders", [])]
    if not roots:
        raise HTTPException(status_code=400, detail="No library folders configured")
    allowed_exts = IMAGE_EXTS | VIDEO_EXTS
    payload = getattr(request.state, "current_user", None) or {}
    owner = str(payload.get("username") or "default")
    removed: list[str] = []
    failed: list[dict] = []
    for p in body.paths[:500]:
        if not isinstance(p, str) or not p:
            continue
        ap = os.path.abspath(os.path.expanduser(p))
        if not any(ap == r or ap.startswith(r + os.sep) for r in roots):
            failed.append({"path": p, "error": "outside library"})
            continue
        ext = os.path.splitext(ap)[1].lower()
        if ext not in allowed_exts:
            failed.append({"path": p, "error": "not a media file"})
            continue
        src = ap
        try:
            st = os.stat(ap)
        except OSError:
            # Private item: the on-disk file is <path>.ethos.enc — trash that.
            if os.path.isfile(ap + ENC_SUFFIX):
                src = ap + ENC_SUFFIX
                try:
                    st = os.stat(src)
                except OSError:
                    failed.append({"path": ap, "error": "not found"})
                    continue
            else:
                failed.append({"path": ap, "error": "not found"})
                continue
        if not stat.S_ISREG(st.st_mode):
            failed.append({"path": ap, "error": "only regular files can be deleted"})
            continue
        # Private items are trashed as <name>.ethos.enc.  The metadata must
        # point at that encrypted filename — restoring the ciphertext under the
        # plain .jpg name would corrupt the photo and orphan it from the index.
        meta_original = ap + ENC_SUFFIX if src.endswith(ENC_SUFFIX) else ap
        try:
            # Photos/videos can be large and the recycle bin may live on another disk.
            await asyncio.to_thread(
                trash_service.move_to_trash, src, owner, meta_original)
        except (OSError, ValueError) as e:
            failed.append({"path": ap, "error": str(e)})
            continue
        removed.append(ap)
    if removed:
        rm = set(removed)
        _index_state["items"] = [it for it in _index_state["items"]
                                 if it["path"] not in rm]
        conn = sqlite3.connect(_db_path(), timeout=15.0)
        try:
            cur = conn.cursor()
            for ap in removed:
                cur.execute("DELETE FROM media WHERE path=?", (ap,))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "removed": len(removed), "failed": failed}


class GalleryPrivateRequest(BaseModel):
    paths: list[str] = []
    private: bool = True
    pin: str = ""     # the PIN itself (alternative to an active Private session)
    ps: str = ""      # Private session token from /private-pin/verify


class GalleryFavoriteRequest(BaseModel):
    paths: list[str] = []
    favorite: bool = True


class AlbumCreateRequest(BaseModel):
    name: str = ""


class AlbumPathsRequest(BaseModel):
    paths: list[str] = []


def _apply_privacy_files(paths: list, make_private: bool, key: bytes) -> tuple:
    """Encrypt (make private) or decrypt+restore (make public) files in place.

    Runs in a worker thread.  Returns (done_paths, failed_entries).
    """
    done: list[str] = []
    failed: list[dict] = []
    for ap in paths:
        enc = ap + ENC_SUFFIX
        try:
            if make_private:
                if not os.path.isfile(enc):
                    _encrypt_file(ap, key)   # already encrypted → flag only
            elif os.path.isfile(enc):
                _decrypt_file(enc, key)      # legacy unencrypted private: nothing to do
        except Exception as exc:
            failed.append({"path": ap, "error": str(exc)})
            continue
        done.append(ap)
    return done, failed


@router.post("/private")
async def gallery_set_private(body: GalleryPrivateRequest, request: Request) -> dict:
    """Move items to/from the Private section.

    Making private encrypts each file at rest (<path>.ethos.enc); making public
    decrypts and restores the original.  Both need the AES key, supplied via an
    active Private session (``ps``) or the PIN itself (``pin``). Only regular
    media files inside a configured library folder are accepted; unknown paths
    come back in `failed` with an error.
    """
    cfg = _load_config()
    roots = [f["path"] for f in cfg.get("folders", [])]
    if not roots:
        raise HTTPException(status_code=400, detail="No library folders configured")
    allowed_exts = IMAGE_EXTS | VIDEO_EXTS
    wanted: set[str] = set()
    failed: list[dict] = []
    for p in body.paths[:500]:
        if not isinstance(p, str) or not p:
            continue
        ap = os.path.abspath(os.path.expanduser(p))
        if not any(ap == r or ap.startswith(r + os.sep) for r in roots):
            failed.append({"path": p, "error": "outside library"})
            continue
        ext = os.path.splitext(ap)[1].lower()
        if ext not in allowed_exts:
            failed.append({"path": p, "error": "not a media file"})
            continue
        wanted.add(ap)
    updated = 0
    indexed: set[str] = set()
    rows: dict[str, int] = {}
    if wanted:
        conn = sqlite3.connect(_db_path(), timeout=15.0)
        try:
            cur = conn.cursor()
            marks = ",".join("?" * len(wanted))
            rows = {row[0]: row[1] for row in cur.execute(
                f"SELECT path, private FROM media WHERE path IN ({marks})", tuple(wanted))}
        finally:
            conn.close()
    indexed = set(rows)

    # Do we actually need the key? (encrypting new files / decrypting existing ones)
    if body.private:
        need_key = any(os.path.isfile(p) and not os.path.isfile(p + ENC_SUFFIX)
                       for p in indexed)
    else:
        need_key = any(rows[p] and os.path.isfile(p + ENC_SUFFIX) for p in indexed)

    key: Optional[bytes] = None
    if need_key:
        key, err = _resolve_priv_key(request, body.pin, body.ps)
        if key is None:
            status = 409 if err == "pin_not_set" else 403
            raise HTTPException(status_code=status, detail=err)

    done: list[str] = []
    if indexed:
        loop = asyncio.get_running_loop()
        done, ffailed = await loop.run_in_executor(
            None, _apply_privacy_files, sorted(indexed), body.private, key or b"")
        failed.extend(ffailed)

    if done:
        flag = 1 if body.private else 0
        conn = sqlite3.connect(_db_path(), timeout=15.0)
        try:
            cur = conn.cursor()
            for ap in sorted(done):
                cur.execute("UPDATE media SET private=? WHERE path=?", (flag, ap))
            conn.commit()
        finally:
            conn.close()
    failed.extend({"path": p, "error": "not indexed yet"} for p in sorted(wanted - indexed))

    flag = bool(body.private)
    done_set = set(done)
    for it in _index_state["items"]:
        if it["path"] in done_set:
            it["private"] = flag
    return {"ok": True, "updated": len(done), "failed": failed}

@router.post("/favorite")
async def gallery_set_favorite(body: GalleryFavoriteRequest) -> dict:
    """Star/unstar items (Favorites view). Flag only — no file changes.

    Paths must already be in the index; unknown paths come back in `failed`
    with 'not indexed yet' so the UI can retry after a rescan.
    """
    wanted: set[str] = set()
    for p in body.paths[:500]:
        if isinstance(p, str) and p:
            wanted.add(os.path.abspath(os.path.expanduser(p)))
    flag = bool(body.favorite)
    done: list[str] = []
    if wanted:
        conn = sqlite3.connect(_db_path(), timeout=15.0)
        try:
            cur = conn.cursor()
            marks = ",".join("?" * len(wanted))
            indexed = {row[0] for row in cur.execute(
                f"SELECT path FROM media WHERE path IN ({marks})", tuple(wanted))}
            if indexed:
                cur.executemany(
                    "UPDATE media SET favorite=? WHERE path=?",
                    [(1 if flag else 0, p) for p in sorted(indexed)])
                conn.commit()
            done = sorted(indexed)
        finally:
            conn.close()
    failed = [{"path": p, "error": "not indexed yet"} for p in sorted(wanted - set(done))]
    done_set = set(done)
    for it in _index_state["items"]:
        if it["path"] in done_set:
            it["favorite"] = flag
    return {"ok": True, "updated": len(done), "failed": failed}


# ── Private PIN management + session-gated media endpoints ───────────────────

class PinSetRequest(BaseModel):
    action: str = "set"      # 'set' (first time) | 'change'
    new_pin: str = ""
    old_pin: str = ""


class PinVerifyRequest(BaseModel):
    pin: str = ""


@router.get("/private-pin/status")
async def gallery_private_pin_status() -> dict:
    """Whether a Private PIN is configured (drives create-vs-enter UI)."""
    cfg = _load_config()
    return {"set": bool((cfg.get("private_pin") or {}).get("hash"))}


@router.post("/private-pin/verify")
async def gallery_private_pin_verify(body: PinVerifyRequest, request: Request) -> dict:
    """Check the PIN and issue a short-lived Private session token.

    The token (ps_token) lives only in server memory for SESSION_TTL seconds
    and is bound to the logged-in user.  It unlocks view=private listings,
    private media/thumbnails, and privacy toggles without re-entering the PIN.
    """
    user = _request_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="unauthenticated")
    pin_cfg = _load_config().get("private_pin") or {}
    if not pin_cfg.get("hash"):
        raise HTTPException(status_code=409, detail="pin_not_set")
    if not hmac.compare_digest(_pin_hash(body.pin, pin_cfg["salt"]), pin_cfg["hash"]):
        raise HTTPException(status_code=403, detail="wrong_pin")
    key = _enc_key(body.pin, pin_cfg["enc_salt"])
    return {"ok": True, "ps_token": _issue_session(user, key)}


@router.post("/private-pin")
async def gallery_private_pin_set(body: PinSetRequest, request: Request) -> dict:
    """Set the Private PIN (first time) or change it.

    'set': stores PBKDF2 hash + encryption salt, then encrypts any items that
    were already flagged private but still sit on disk in plaintext.
    'change': verifies old_pin, re-encrypts every encrypted private file with a
    key derived from the new PIN (same enc_salt), then updates the stored hash.
    Both return a fresh ps_token so the UI can continue without another prompt.
    """
    user = _request_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="unauthenticated")
    cfg = _load_config()
    pin_cfg = dict(cfg.get("private_pin") or {})

    if body.action == "set":
        if pin_cfg.get("hash"):
            raise HTTPException(status_code=409, detail="pin_already_set")
        if len(body.new_pin) < PIN_MIN_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"PIN must be at least {PIN_MIN_LEN} characters")
        salt = secrets.token_hex(16)
        enc_salt = secrets.token_hex(16)
        key = _enc_key(body.new_pin, enc_salt)
        # Persist the PIN first so it survives a crash mid-migration.
        pin_cfg.update(salt=salt, hash=_pin_hash(body.new_pin, salt),
                       enc_salt=enc_salt)
        cfg["private_pin"] = pin_cfg
        _save_config(cfg)
        migrated = 0
        failed_paths: list[str] = []
        for it in list(_index_state["items"]):
            if not it.get("private"):
                continue
            ap, enc = it["path"], it["path"] + ENC_SUFFIX
            try:
                if os.path.isfile(ap) and not os.path.isfile(enc):
                    await asyncio.to_thread(_encrypt_file, ap, key)
                    migrated += 1
            except Exception as exc:
                logger.warning("PIN set: migration failed for %s: %s", ap, exc)
                failed_paths.append(ap)
        if failed_paths:
            # A plaintext file must never stay flagged private (it would be
            # served unencrypted via /api/files/raw) — revert those flags.
            conn = sqlite3.connect(_db_path(), timeout=15.0)
            try:
                for ap in failed_paths:
                    conn.execute("UPDATE media SET private=0 WHERE path=?", (ap,))
                conn.commit()
            finally:
                conn.close()
            for it in _index_state["items"]:
                if it["path"] in set(failed_paths):
                    it["private"] = False
        return {"ok": True, "ps_token": _issue_session(user, key),
                "migrated": migrated,
                "failed": failed_paths}

    if body.action == "change":
        if not pin_cfg.get("hash"):
            raise HTTPException(status_code=409, detail="pin_not_set")
        if not hmac.compare_digest(_pin_hash(body.old_pin, pin_cfg["salt"]),
                                  pin_cfg["hash"]):
            raise HTTPException(status_code=403, detail="wrong_pin")
        if len(body.new_pin) < PIN_MIN_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"PIN must be at least {PIN_MIN_LEN} characters")
        old_key = _enc_key(body.old_pin, pin_cfg["enc_salt"])
        new_key = _enc_key(body.new_pin, pin_cfg["enc_salt"])
        # Re-key every encrypted private file BEFORE updating the stored hash,
        # so a crash mid-way leaves the old PIN still valid and files intact.
        reencrypted = 0
        done_enc: list[str] = []
        failed_paths: list[str] = []
        for it in list(_index_state["items"]):
            if not it.get("private"):
                continue
            enc, ap = it["path"] + ENC_SUFFIX, it["path"]
            try:
                if os.path.isfile(enc):
                    await asyncio.to_thread(_reencrypt_file, enc, old_key, new_key)
                    done_enc.append(enc)
                    reencrypted += 1
            except Exception as exc:
                logger.warning("PIN change: re-encryption failed for %s: %s", ap, exc)
                failed_paths.append(ap)
        if failed_paths:
            # Roll the already re-keyed files back to the old key so the whole
            # set stays consistent with the (still stored) old PIN.
            for enc in done_enc:
                try:
                    await asyncio.to_thread(_reencrypt_file, enc, new_key, old_key)
                except Exception as exc:
                    logger.error("PIN change: rollback failed for %s: %s", enc, exc)
            # Aborting keeps the stored hash on the old PIN; any file that was
            # already re-keyed is recoverable with the new PIN only — refuse to
            # switch until every file has been converted.
            raise HTTPException(
                status_code=500,
                detail=f"re-encryption failed for {len(failed_paths)} file(s): "
                       f"{', '.join(os.path.basename(p) for p in failed_paths[:3])}")
        salt = secrets.token_hex(16)
        pin_cfg.update(salt=salt, hash=_pin_hash(body.new_pin, salt))
        cfg["private_pin"] = pin_cfg
        _save_config(cfg)
        # Old sessions still hold the old AES key — drop them all.
        _priv_sessions.clear()
        return {"ok": True, "ps_token": _issue_session(user, new_key),
                "reencrypted": reencrypted}

    raise HTTPException(status_code=400, detail="unknown action")


@router.get("/private-media")
async def gallery_private_media(
        request: Request,
        path: str = Query(...),
        ps: str = Query(default="")):
    """Decrypted bytes of one private item (session-gated).

    Decrypts to a 0600 temp file so Range requests work for videos, then the
    temp file is deleted as soon as the response finishes.  No plaintext copy
    is ever persisted.
    """
    _user, key = _require_priv_session(request, ps)
    ap = _private_item_path(path)
    enc = ap + ENC_SUFFIX
    if not os.path.isfile(enc):
        raise HTTPException(status_code=404, detail="Encrypted file missing")
    loop = asyncio.get_running_loop()
    try:
        tmp = await loop.run_in_executor(None, _decrypt_to_tmp, enc, key, ap)
    except (InvalidTag, ValueError) as exc:
        raise HTTPException(status_code=409, detail=f"decryption_failed: {exc}")
    media_type = mimetypes.guess_type(ap)[0] or "application/octet-stream"
    return FileResponse(tmp, media_type=media_type,
                        background=BackgroundTask(_rm_quiet, tmp))


@router.get("/private-thumb")
async def gallery_private_thumb(
        request: Request,
        path: str = Query(...),
        size: int = Query(150, ge=32, le=640),
        ps: str = Query(default="")):
    """On-the-fly WebP thumbnail for a private item (session-gated).

    Cached in memory only — plaintext derivatives of private media are never
    written to disk.
    """
    _user, key = _require_priv_session(request, ps)
    ap = _private_item_path(path)
    enc = ap + ENC_SUFFIX
    if not os.path.isfile(enc):
        raise HTTPException(status_code=404, detail="Encrypted file missing")
    st = os.stat(enc)
    ck = (ap, size)
    hit = _priv_thumb_cache.get(ck)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return Response(content=hit[2], media_type="image/webp")

    loop = asyncio.get_running_loop()
    ext = os.path.splitext(ap)[1].lower()
    try:
        if ext in IMAGE_EXTS:
            webp: Optional[bytes] = await loop.run_in_executor(
                None, _private_image_thumb, enc, key, size)
        else:
            webp = await asyncio.wait_for(
                loop.run_in_executor(None, _private_video_thumb, enc, key, ap, size),
                timeout=90)
    except (InvalidTag, ValueError) as exc:
        raise HTTPException(status_code=409, detail=f"decryption_failed: {exc}")

    if webp is None:
        return _video_fallback_svg(size)
    if len(_priv_thumb_cache) > 128:
        _priv_thumb_cache.pop(next(iter(_priv_thumb_cache)))   # evict oldest
    _priv_thumb_cache[ck] = (st.st_mtime, st.st_size, webp)
    return Response(content=webp, media_type="image/webp")


@router.get("/private-download")
async def gallery_private_download(
        request: Request,
        path: str = Query(...),
        ps: str = Query(default="")):
    """Download a private item — serves the ENCRYPTED blob as an attachment.

    A copied/downloaded file is therefore useless without the PIN; in-app
    viewing (private-media) is the only way to see it decrypted.
    """
    _user, _key = _require_priv_session(request, ps)
    ap = _private_item_path(path)
    enc = ap + ENC_SUFFIX
    if not os.path.isfile(enc):
        raise HTTPException(status_code=404, detail="Encrypted file missing")
    return FileResponse(
        enc, media_type="application/octet-stream",
        filename=os.path.basename(ap) + ENC_SUFFIX)


def _filter_items(items: List[dict], folder: str, q: str) -> List[dict]:
    """Scope by folder and match the query against name, folder path and
    camera make/model (P0.9 — 'nikon' finds every NIKON shot)."""
    out = []
    ql = (q or "").lower()
    if folder:
        folder_prefix = folder.rstrip("/") + os.sep
        for it in items:
            f = it["folder"]
            if not (f == folder or f.startswith(folder_prefix)):
                continue
            if ql and not (ql in it["name"].lower()
                           or ql in f.lower()
                           or ql in (it.get("camera") or "").lower()):
                continue
            out.append(it)
    else:
        for it in items:
            if ql and not (ql in it["name"].lower()
                           or ql in it["folder"].lower()
                           or ql in (it.get("camera") or "").lower()):
                continue
            out.append(it)
    return out


def _eff_date(it: dict) -> float:
    """Effective date for ordering: EXIF taken date when known, else mtime.

    Always returns a float (0.0 when unknown) — values are coerced so a
    malformed entry can never poison the date comparisons/sorting."""
    for v in (it.get("taken"), it.get("modified")):
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _sort_items(items: List[dict], sort: str) -> List[dict]:
    spec = {
        "date_desc": (lambda x: _eff_date(x), True),
        "date_asc":  (lambda x: _eff_date(x), False),
        "name_asc":  (lambda x: x["name"].lower(), False),
        "size_desc": (lambda x: x["size"], True),
    }.get(sort, (lambda x: x["modified"], True))
    key, rev = spec
    # Stable two-pass: tie-break by name first so equal keys stay neat.
    items = sorted(items, key=lambda x: x["name"].lower())
    return sorted(items, key=key, reverse=rev)

def _scoped_items(folder: str, view_private: bool, include_videos: bool) -> List[dict]:
    """Items visible under a given scope (before search) — shared by /items and /months.

    Mirrors the scoping rules of /items: private items only in the private
    view, recursive folder filter, video toggle. Callers apply `q` on top.
    """
    items = _index_state["items"] or []
    if view_private:
        # Private view honours the same recursive folder filter as normal views,
        # so folder navigation works inside the Private section too.
        scoped = _filter_items([it for it in items if it.get("private")], folder=folder, q="")
    else:
        scoped = _filter_items([it for it in items if not it.get("private")], folder=folder, q="")
    if include_videos is False:
        scoped = [it for it in scoped if it["type"] == "image"]
    return scoped

def _month_key_local(epoch: float) -> str:
    """'YYYY-MM' of an epoch timestamp in *local* time (matches UI date labels)."""
    return datetime.fromtimestamp(epoch or 0.0).strftime("%Y-%m")


def _apply_view_filters(items: List[dict], q: str = "", camera: str = "",
                        fav: bool = False, album_id: str = "") -> List[dict]:
    """Search + camera + album + favourites filters shared by the data endpoints.

    Camera is an exact, case-insensitive match on the indexed EXIF make/model
    (the UI sends the facet value verbatim).  It is a separate parameter so it
    composes with free-text search — previously the frontend glued both into a
    single ``q`` string, which could never match anything.
    """
    out = _filter_items(items, "", q)
    cam = (camera or "").strip().lower()
    if cam:
        out = [it for it in out if (it.get("camera") or "").strip().lower() == cam]
    if album_id:
        if _find_album(album_id) is None:
            raise HTTPException(status_code=404, detail="unknown album")
        aset = _album_path_set(album_id)
        out = [it for it in out if it["path"] in aset]
    if fav:
        out = [it for it in out if it.get("favorite")]
    return out


# ── Albums (P0.8) — JSON store of named path collections ─────────────────────
# Shape: [{"id": "a1b2c3d4e5f6", "name": "Wedding", "paths": [abs paths], "created": epoch}]

ALBUMS_LOCK = asyncio.Lock()


def _load_albums() -> list:
    data = _library().albums_store.load()
    return [a for a in data if isinstance(a, dict) and a.get("id")] if isinstance(data, list) else []


def _save_albums(albums: list) -> None:
    _library().albums_store.save(albums)


def _find_album(album_id: str) -> Optional[dict]:
    for a in _load_albums():
        if a.get("id") == album_id:
            return a
    return None


def _album_path_set(album_id: str) -> set:
    a = _find_album(album_id)
    return {p for p in (a or {}).get("paths", []) if isinstance(p, str)}


def _albums_for(username: str) -> list:
    """Albums from an explicit account's store (read-only sharing view)."""
    data = _library(username).albums_store.load()
    return [a for a in data if isinstance(a, dict) and a.get("id")] if isinstance(data, list) else []


def _album_path_set_for(username: str, album_id: str) -> set:
    for album in _albums_for(username):
        if album.get("id") == album_id:
            return {p for p in (album.get("paths") or []) if isinstance(p, str)}
    return set()


@router.get("/albums")
async def gallery_list_albums(owner: str = Query(default="")) -> dict:
    """Album list with live counts.

    `owner` switches to a read-only shared view: only albums explicitly granted
    to the caller by that account are returned, each tagged `shared: true`.
    """
    caller = user_data.current_username()
    if owner and owner != caller:
        grants = [g for g in sharing_service.list_incoming(caller)
                  if g.get("kind") == "album" and g.get("owner") == owner]
        if not grants:
            raise HTTPException(status_code=403, detail="No albums shared by this user")
        granted = {str(g.get("resource") or "") for g in grants}
        indexed = {
            it["path"] for it in (_library(owner).state["items"] or [])
            if not it.get("private") and not str(it.get("path") or "").endswith(ENC_SUFFIX)
        }
        out = []
        for album in _albums_for(owner):
            if album["id"] not in granted:
                continue
            paths = [p for p in album.get("paths", []) if isinstance(p, str)]
            out.append({"id": album["id"], "name": album.get("name", ""),
                        "count": sum(1 for p in paths if p in indexed),
                        "created": float(album.get("created") or 0),
                        "owner": owner, "shared": True})
        return {"albums": out, "owner": owner}

    indexed = {it["path"] for it in (_index_state["items"] or [])}
    out = []
    for a in _load_albums():
        paths = [p for p in a.get("paths", []) if isinstance(p, str)]
        out.append({"id": a["id"], "name": a.get("name", ""),
                    "count": sum(1 for p in paths if p in indexed),
                    "created": float(a.get("created") or 0)})
    return {"albums": out}


@router.post("/albums")
async def gallery_create_album(body: AlbumCreateRequest) -> dict:
    name = (body.name or "").strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    async with ALBUMS_LOCK:
        albums = _load_albums()
        if any((a.get("name") or "").lower() == name.lower() for a in albums):
            raise HTTPException(status_code=409, detail="an album with this name exists")
        album = {"id": secrets.token_hex(6), "name": name, "paths": [],
                 "created": time.time()}
        albums.append(album)
        _save_albums(albums)
    return {"ok": True, "album": {"id": album["id"], "name": album["name"],
                                  "count": 0, "created": album["created"]}}


@router.delete("/albums/{album_id}")
async def gallery_delete_album(album_id: str) -> dict:
    async with ALBUMS_LOCK:
        albums = _load_albums()
        kept = [a for a in albums if a.get("id") != album_id]
        if len(kept) == len(albums):
            raise HTTPException(status_code=404, detail="unknown album")
        _save_albums(kept)
    return {"ok": True}


@router.post("/albums/{album_id}/add")
async def gallery_album_add(album_id: str, body: AlbumPathsRequest) -> dict:
    """Add items to an album. Only indexed paths are accepted; the rest come
    back in `failed` so the UI can retry after a rescan."""
    async with ALBUMS_LOCK:
        albums = _load_albums()
        album = next((a for a in albums if a.get("id") == album_id), None)
        if album is None:
            raise HTTPException(status_code=404, detail="unknown album")
        indexed = {it["path"] for it in (_index_state["items"] or [])}
        have = set(album.get("paths", []))
        added = 0
        failed: list[str] = []
        for p in body.paths[:500]:
            if not isinstance(p, str) or not p:
                continue
            ap = os.path.abspath(os.path.expanduser(p))
            if ap not in indexed:
                failed.append(ap)
                continue
            if ap not in have:
                have.add(ap)
                added += 1
        album["paths"] = sorted(have)
        _save_albums(albums)
    return {"ok": True, "added": added, "failed": failed}


@router.post("/albums/{album_id}/remove")
async def gallery_album_remove(album_id: str, body: AlbumPathsRequest) -> dict:
    async with ALBUMS_LOCK:
        albums = _load_albums()
        album = next((a for a in albums if a.get("id") == album_id), None)
        if album is None:
            raise HTTPException(status_code=404, detail="unknown album")
        drop = {os.path.abspath(os.path.expanduser(p)) for p in body.paths[:500]
                if isinstance(p, str) and p}
        before = len(album.get("paths", []))
        album["paths"] = [p for p in album.get("paths", []) if p not in drop]
        _save_albums(albums)
    return {"ok": True, "removed": before - len(album["paths"])}


def _day_range_local(day: str):
    """'YYYY-MM-DD' → [start, end) epoch range of that local calendar day.

    Returns (None, None) when the argument is empty/unparseable so callers can
    treat it as 'no filter'.
    """
    if not day:
        return None, None
    try:
        first = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return None, None
    return first.timestamp(), (first + timedelta(days=1)).timestamp()


@router.get("/items")
async def gallery_items(
    request: Request,
    folder: str = Query(default="", description="Restrict to this folder (recursive). Empty = whole library."),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=60, ge=1, le=200),
    sort: str = Query(default="", description="date_desc|date_asc|name_asc|size_desc (empty → config default)"),
    q: str = Query(default="", description="Case-insensitive filename/folder/camera search."),
    camera: str = Query(default="", description="Exact camera make/model (composes with q, unlike a merged q string)"),
    view: str = Query(default="", description="''=normal views (private items hidden) | 'private'=only private items"),
    day: str = Query(default="", description="'YYYY-MM-DD' — restrict to one local calendar day (effective date). Index-backed scopes only."),
    fav: str = Query(default="", description="'1' — only favorited items (within the current scope; private items still hidden without a session)"),
    album: str = Query(default="", description="Album id — restrict to that album's items (private members stay hidden without a Private session)"),
    owner: str = Query(default="", description="Read-only shared album: the album owner's username"),
):
    """Paginated media items straight from the index.

    Returns {total, offset, limit, items:[...], folders:[{path,name,count}]} —
    `folders` are facet children of the current selection (roots or folder).
    Lazy triggers a background rescan when the index is empty/stale and tells
    the client via the `stale` flag while one is in flight.

    view=private additionally requires an active Private session: pass the
    ps_token from /private-pin/verify as ?ps=… (a plain login is not enough).

    `owner` requires an album grant, ignores `folder`, forbids the private view
    and reads the owner's index; private/encrypted items are never returned.
    """
    caller = user_data.current_username()
    if owner and owner != caller:
        if not album:
            raise HTTPException(status_code=400, detail="album required for a shared view")
        if view == "private":
            raise HTTPException(status_code=403, detail="private items are not shared")
        if not sharing_service.allows_resource(caller, owner, "album", album):
            raise HTTPException(status_code=404, detail="unknown album")
        aset = _album_path_set_for(owner, album)
        items = [
            it for it in (_library(owner).state["items"] or [])
            if it.get("path") in aset
            and not it.get("private")
            and not str(it.get("path") or "").endswith(ENC_SUFFIX)
        ]
        items = _filter_items(items, "", q)
        cam = (camera or "").strip().lower()
        if cam:
            items = [it for it in items if (it.get("camera") or "").strip().lower() == cam]
        if fav == "1":
            items = [it for it in items if it.get("favorite")]
        dstart, dend = _day_range_local(day)
        if dstart is not None and dend is not None:
            items = [it for it in items if dstart <= _eff_date(it) < dend]
        total = len(items)
        ordered = _sort_items(items, sort or "date_desc")
        page = ordered[offset:offset + limit]
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": [dict(it) for it in page],
            "folders": [],
            "stale": False,
            "owner": owner,
            "album": album,
        }

    if view == "private":
        user = _request_user(request)
        if not _session_for(request.query_params.get("ps", ""), user):
            raise HTTPException(status_code=403, detail="pin_required")

    cfg = _load_config()
    roots = [f["path"] for f in cfg["folders"]]

    # Lazy: kick a background rescan if the index is empty/stale.
    _ensure_fresh_index()

    if not sort:
        sort = str(cfg["options"].get("sort", "date_desc"))
    include_videos = bool(cfg["options"].get("include_videos", True))

    # Ad-hoc folder (e.g. "Open in Gallery" from File Manager) that is not part
    # of the configured library → live scan instead of the index.
    if folder:
        abs_folder = os.path.abspath(os.path.expanduser(folder))
        under_root = any(abs_folder == r or abs_folder.startswith(r + os.sep) for r in roots)
        if not under_root and _is_allowed_read(abs_folder):
            loop = asyncio.get_running_loop()
            found = await loop.run_in_executor(
                None, _walk_folder_sync, abs_folder, include_videos)
            if not include_videos:
                found = [f for f in found if f["file_type"] != "video"]
            if q:
                ql = q.lower()
                found = [f for f in found if ql in f["name"].lower()]
            total = len(found)
            ordered = _sort_items(
                [{**f, "type": f["file_type"]} for f in found], sort)
            page = ordered[offset:offset + limit]
            return {
                "total": total,
                "offset": offset,
                "limit": limit,
                "items": [dict(it) for it in page],
                "folders": [],
                "stale": False,
            }

    items = _index_state["items"] or []
    if folder and (not roots):
        return {"total": 0, "offset": offset, "limit": limit, "items": [], "folders": []}

    # Privacy scope: private items are hidden from normal views and only
    # visible in the dedicated Private section (view=private).
    scoped = _scoped_items(folder, view == "private", include_videos)
    filtered = _apply_view_filters(scoped, q=q, camera=camera,
                                   fav=(fav == "1"), album_id=album)
    dstart, dend = _day_range_local(day)
    if dstart is not None and dend is not None:
        filtered = [it for it in filtered if dstart <= _eff_date(it) < dend]
    total = len(filtered)
    ordered = _sort_items(filtered, sort)
    page = ordered[offset:offset + limit]

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [dict(it) for it in page],
        # Facets are children of the current folder (or of the library roots
        # at the top level) and reflect the active search/filters.
        "folders": _top_folders(filtered, roots, folder),
        "stale": bool(_index_state["scanning"]),
    }


@router.get("/months")
async def gallery_months(
    request: Request,
    folder: str = Query(default="", description="Restrict to this folder (recursive)."),
    q: str = Query(default="", description="Case-insensitive filename/folder/camera search."),
    camera: str = Query(default="", description="Exact camera make/model filter."),
    view: str = Query(default="", description="''=normal | 'private' (requires ps session)"),
    fav: str = Query(default="", description="'1' — only favorited items."),
    album: str = Query(default="", description="Album id filter."),
):
    """Per-month item counts for the Timeline view.

    Same scoping as /items (search, camera, favourites, album); months are
    keyed in local time so labels match what the grid shows. Newest month
    first.
    """
    if view == "private":
        user = _request_user(request)
        if not _session_for(request.query_params.get("ps", ""), user):
            raise HTTPException(status_code=403, detail="pin_required")

    cfg = _load_config()
    include_videos = bool(cfg["options"].get("include_videos", True))
    scoped = _apply_view_filters(_scoped_items(folder, view == "private", include_videos),
                                 q=q, camera=camera, fav=(fav == "1"), album_id=album)

    buckets: dict[str, list] = {}
    for it in scoped:
        key = _month_key_local(_eff_date(it))
        b = buckets.get(key)
        if b is None:
            buckets[key] = [1, 0]
        else:
            b[0] += 1
        if it["type"] == "video":
            buckets[key][1] += 1

    months = []
    for key in sorted(buckets, reverse=True):
        count, videos = buckets[key]
        y, m = key.split("-")
        label = datetime(int(y), int(m), 1).strftime("%B %Y")
        months.append({"month": key, "label": label, "count": count,
                       "videos": videos, "images": count - videos})
    return {"months": months}

@router.get("/cameras")
async def gallery_cameras(
    request: Request,
    folder: str = Query(default="", description="Restrict to this folder (recursive)."),
    q: str = Query(default="", description="Case-insensitive filename/camera search."),
    view: str = Query(default="", description="''=normal | 'private' (requires ps session)"),
    fav: str = Query(default="", description="'1' — only favorited items."),
    album: str = Query(default="", description="Album id filter."),
):
    """Distinct camera make/model strings with counts, for the Camera facet.

    Returns [{camera, count}] sorted by count desc, then name asc.
    """
    if view == "private":
        user = _request_user(request)
        if not _session_for(request.query_params.get("ps", ""), user):
            raise HTTPException(status_code=403, detail="pin_required")

    cfg = _load_config()
    include_videos = bool(cfg["options"].get("include_videos", True))
    scoped = _apply_view_filters(_scoped_items(folder, view == "private", include_videos),
                                 q=q, fav=(fav == "1"), album_id=album)

    counts: dict[str, int] = {}
    for it in scoped:
        cam = (it.get("camera") or "").strip()
        if cam:
            counts[cam] = counts.get(cam, 0) + 1
    out = [{"camera": c, "count": n} for c, n in counts.items()]
    out.sort(key=lambda x: (-x["count"], x["camera"].lower()))
    return {"cameras": out}


@router.get("/calendar")
async def gallery_calendar(
    request: Request,
    month: str = Query(..., description="'YYYY-MM' — the month to render"),
    folder: str = Query(default="", description="Restrict to this folder (recursive)."),
    view: str = Query(default="", description="''=normal | 'private' (requires ps session)"),
    q: str = Query(default="", description="Case-insensitive name/folder/camera search."),
    camera: str = Query(default="", description="Exact camera make/model filter."),
    fav: str = Query(default="", description="'1' — only favorited items."),
    album: str = Query(default="", description="Album id filter."),
):
    """Per-day item counts for the Calendar view.

    Same scoping as /items (search, camera, favourites, album). Days are keyed
    in local time; each day carries up to three sample items so the UI can show
    mini-thumbnails without extra requests. Newest activity first within a day
    is irrelevant — samples keep date order (oldest first).
    """
    if view == "private":
        user = _request_user(request)
        if not _session_for(request.query_params.get("ps", ""), user):
            raise HTTPException(status_code=403, detail="pin_required")

    try:
        first = datetime.strptime(month, "%Y-%m").replace(day=1)
    except ValueError:
        raise HTTPException(status_code=400, detail="month must be 'YYYY-MM'")
    if first.month == 12:
        nxt_month = datetime(first.year + 1, 1, 1)
    else:
        nxt_month = datetime(first.year, first.month + 1, 1)

    cfg = _load_config()
    include_videos = bool(cfg["options"].get("include_videos", True))
    scoped = _apply_view_filters(_scoped_items(folder, view == "private", include_videos),
                                 q=q, camera=camera, fav=(fav == "1"), album_id=album)

    days: dict[str, list] = {}
    for it in scoped:
        d = datetime.fromtimestamp(_eff_date(it))
        if d < first or d >= nxt_month:
            continue
        key = d.strftime("%Y-%m-%d")
        b = days.get(key)
        if b is None:
            b = []
            days[key] = b
        b.append(it)

    out_days = []
    for key in sorted(days):
        items = sorted(days[key], key=_eff_date)
        videos = sum(1 for it in items if it["type"] == "video")
        samples = [{"path": it["path"], "name": it["name"], "type": it["type"],
                    "private": bool(it.get("private"))} for it in items[:3]]
        out_days.append({"date": key, "count": len(items), "videos": videos,
                         "images": len(items) - videos, "samples": samples})
    return {"month": month, "days": out_days}

# ── Per-file metadata (EXIF) — on demand, not indexed ───────────────────────

def _read_meta(src: str, orig: str) -> dict:
    """File + EXIF metadata for one media file (best-effort; never raises).

    `src` is the readable file (decrypted temp for private items), `orig` the
    on-disk path used for size/date and MIME detection.
    """
    meta: dict = {"path": orig}
    try:
        st = os.stat(orig) if os.path.exists(orig) else os.stat(src)
        meta["size"] = int(st.st_size)
        meta["mtime"] = int(st.st_mtime)
    except OSError:
        pass  # probe failure means 'not present'
    mime = mimetypes.guess_type(orig)[0] or ""
    if not mime.startswith("image/"):
        return meta   # videos: basic file info only
    try:
        with Image.open(src) as img:
            w, h = img.size
            exif = img.getexif()
            meta["width"] = int(w)
            meta["height"] = int(h)

            def s(tag):
                v = exif.get(tag)
                return str(v).strip() if v is not None else None

            try:
                eifd = exif.get_ifd(0x8769)   # ExifIFD
            except Exception:
                eifd = None

            def es(tag):
                if eifd is None: return None
                v = eifd.get(tag)
                return str(v).strip() if v is not None else None

            def num(tag, where=0):
                try:
                    src_ifd = exif if where == 0 else eifd
                    if src_ifd is None: return None
                    v = src_ifd.get(tag)
                    return float(v) if v is not None else None
                except Exception:
                    return None

            make, model, software = s(0x010F), s(0x0110), s(0x0131)
            lens_make, lens_model = es(0xA432), es(0xA433)
            if make: meta["make"] = make
            if model: meta["model"] = model
            if software and (not make or software.lower() != make.lower()):
                meta["software"] = software
            lens = " ".join(x for x in (lens_make, lens_model) if x).strip()
            if lens: meta["lens"] = lens
            aperture = num(0x829D, 1)   # FNumber
            shutter = num(0x829A, 1)    # ExposureTime (s)
            iso = num(0x8827, 1)        # ISOSpeedRatings
            focal = num(0x920A, 1)      # FocalLength (mm)
            if aperture: meta["aperture"] = round(aperture, 2)
            if shutter and shutter > 0: meta["shutter_s"] = float(shutter)
            if iso: meta["iso"] = int(iso)
            if focal: meta["focal_length_mm"] = round(focal, 1)

            try:
                gps_ifd = exif.get_ifd(0x8825)   # GPSInfo
                if gps_ifd is not None:
                    lat, lon = _gps_decimal(gps_ifd)
                    if lat is not None and lon is not None:
                        meta["lat"] = round(lat, 6)
                        meta["lon"] = round(lon, 6)
            except Exception:
                pass  # intentional: a failure here must not break the caller
    except Exception:
        pass   # unreadable/corrupt image → basic file info only
    return meta

@router.get("/meta")
async def gallery_meta(
        request: Request,
        path: str = Query(...),
        ps: str = Query(default="")):
    """File + EXIF metadata for one item (public, or session-gated private).

    Private items are decrypted to a 0600 temp file just long enough to read
    the EXIF header; no plaintext copy is persisted. Items in a shared album
    are readable through the normal read scope.
    """
    ap = path_scope.sanitize_read(path or "")
    loop = asyncio.get_running_loop()
    tmp = None
    try:
        is_priv = any(it.get("private") and it["path"] == ap for it in _index_state["items"])
        if is_priv:
            _user, key = _require_priv_session(request, ps)
            enc = ap + ENC_SUFFIX
            if not os.path.isfile(enc):
                raise HTTPException(status_code=404, detail="Encrypted file missing")
            tmp = await loop.run_in_executor(None, _decrypt_to_tmp, enc, key, ap)
            src = tmp
        else:
            if not os.path.isfile(ap):
                raise HTTPException(status_code=404, detail="File not found")
            src = ap
        return await loop.run_in_executor(None, _read_meta, src, ap)
    finally:
        if tmp:
            _rm_quiet(tmp)

@router.get("/scan")
async def gallery_scan(
    paths: str = Query(default="", description="Comma-separated directories; empty → use saved config folders"),
):
    """Legacy one-shot recursive scan (used by the v1 UI and File Manager)."""
    cfg = _load_config()
    include_videos = bool(cfg["options"].get("include_videos", True))

    if paths.strip():
        raw_paths = [p.strip() for p in paths.split(",") if p.strip()]
    else:
        raw_paths = [f["path"] for f in cfg["folders"]]

    all_files = []
    for raw_path in raw_paths:
        try:
            sanitized = _sanitize(raw_path)
        except HTTPException:
            continue
        all_files.extend(_walk_folder_sync(sanitized, include_videos))

    seen = set()
    unique = []
    for f in sorted(all_files, key=lambda x: x["modified"], reverse=True):
        if f["path"] in seen:
            continue
        seen.add(f["path"])
        unique.append(f)
    # Keep the legacy flat array shape (v1 UI expects a bare JSON list).
    return unique


def _walk_folder_sync(base_path: str, include_videos: bool) -> List[dict]:
    """Small standalone walker for the legacy /scan endpoint (no state writes)."""
    results = []
    abs_path = os.path.abspath(os.path.expanduser(base_path))
    if not _is_allowed(abs_path) or not os.path.isdir(abs_path):
        return results
    try:
        for dirpath, dirnames, filenames in os.walk(abs_path, followlinks=False):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and not os.path.islink(os.path.join(dirpath, d))]
            for fname in filenames:
                if fname.startswith("."):
                    continue
                ext = "." + fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                if ext not in MEDIA_EXTS or (ext in VIDEO_EXTS and not include_videos):
                    continue
                full_path = os.path.join(dirpath, fname)
                try:
                    st = os.stat(full_path)
                except OSError:
                    continue
                results.append({
                    "name": fname,
                    "path": full_path,
                    "size": int(st.st_size),
                    "modified": round(float(st.st_mtime), 3),
                    "is_dir": False,
                    "file_type": "image" if ext in IMAGE_EXTS else "video",
                    "folder": dirpath,
                })
    except PermissionError:
        pass  # probe failure means 'not present'
    return results
