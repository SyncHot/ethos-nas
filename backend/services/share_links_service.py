"""EthOS NAS — Public Share Links store

Persistent, thread-safe store of tokenized share links for files and folders.
Mirrors the notifications service pattern: single JSON file + threading.Lock +
atomic writes under settings.DATA_DIR.

A link lets anyone holding its URL download the target without an EthOS
account. Tokens are unguessable (secrets.token_urlsafe(16) ≈ 128 bits). Path
policy matches files.py: no allowlist by design — only paths an authenticated
EthOS user explicitly chose to share can ever be linked, and OS-level access
control still applies at serve time.

Store file: <DATA_DIR>/share_links.json  {"version":1, "links":[...]}
Link shape: {id, path, name, token, created_at (ISO Z), expires_at (ISO Z | null),
             max_downloads (int | null), downloads (int)}
Multi-file links (Photo Station multi-share) additionally carry
`files: [abs paths]`; their `path` is "" and the public endpoint serves a
self-contained gallery page instead of a raw download.
"""

import json
import logging
import os
import secrets
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.config import settings

logger = logging.getLogger("ethos-nas.services.share_links_service")

DATA_FILE = Path(settings.DATA_DIR) / "share_links.json"

_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _default_store() -> dict:
    return {"version": 1, "links": []}


def _load() -> dict:
    """Load the store; missing/corrupt file yields a fresh empty store."""
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("links"), list):
            return _default_store()
        data["links"] = [l for l in data["links"] if isinstance(l, dict) and l.get("id") and l.get("token")]
        return data
    except FileNotFoundError:
        return _default_store()
    except Exception as exc:  # noqa: BLE001 — corrupt store must not crash callers
        logger.warning("share_links store unreadable (%s) — starting fresh", exc)
        return _default_store()


def _save(store: dict) -> None:
    """Atomic write: tmp file in same dir + os.replace."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(DATA_FILE.parent), prefix=".share_links-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # best-effort cleanup — nothing to report
        raise


def _resolve_path(path: str) -> str:
    """Expand + absolutize a user-supplied path (same policy as files.py::_sanitize)."""
    if not path or not isinstance(path, str):
        raise ValueError("Path is required")
    return os.path.abspath(os.path.expanduser(path))


def link_status(link: dict) -> str:
    """Current effective status of a link: active | expired | limit_reached."""
    exp = _parse_iso(link.get("expires_at"))
    if exp and datetime.now(timezone.utc) >= exp:
        return "expired"
    max_dl = link.get("max_downloads")
    if isinstance(max_dl, int) and max_dl > 0 and link.get("downloads", 0) >= max_dl:
        return "limit_reached"
    return "active"


def with_status(link: dict) -> dict:
    """Copy of a link dict with the derived `status` field added."""
    d = dict(link)
    d["status"] = link_status(link)
    return d


# ── Public API (all thread-safe) ─────────────────────────────────────────────

def _validate_options(expires_in_days, max_downloads) -> tuple[str | None, int | None]:
    """Validate share options → (expires_at ISO Z | None, download cap | None)."""
    if expires_in_days is not None:
        try:
            days = float(expires_in_days)
        except (TypeError, ValueError):
            raise ValueError("expires_in_days must be a number")
        if days <= 0:
            raise ValueError("expires_in_days must be > 0")
    else:
        days = None

    if max_downloads is not None:
        try:
            limit = int(max_downloads)
        except (TypeError, ValueError):
            raise ValueError("max_downloads must be an integer")
        if limit <= 0:
            raise ValueError("max_downloads must be > 0 or omitted")
    else:
        limit = None

    expires_at = None
    if days is not None:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")
    return expires_at, limit

def create_link(
    path: str,
    name: str | None = None,
    expires_in_days: int | float | None = None,
    max_downloads: int | None = None,
    owner: str = "",
    owner_role: str = "user",
) -> dict:
    """Create a share link for an existing file or directory.

    Raises ValueError on invalid/missing path or bad options.
    Returns the stored link dict (without derived status).
    """
    abs_path = _resolve_path(path)
    if not os.path.exists(abs_path):
        raise ValueError(f"Path not found: {abs_path}")

    expires_at, limit = _validate_options(expires_in_days, max_downloads)

    label = (name or "").strip() or os.path.basename(abs_path.rstrip("/")) or abs_path
    if len(label) > 200:
        label = label[:200]

    link = {
        "id": uuid.uuid4().hex,
        "path": abs_path,
        "name": label,
        "token": secrets.token_urlsafe(16),
        "created_at": _now_iso(),
        "expires_at": expires_at,
        "max_downloads": limit,
        "downloads": 0,
        # Creator identity: the public route re-validates every stored path
        # against this scope, so a link can never outlive its creator's rights.
        "owner": owner,
        "owner_role": owner_role,
    }

    with _LOCK:
        store = _load()
        store["links"].append(link)
        _save(store)
    logger.info("Share link created for %s (expires=%s max_dl=%s)", abs_path, expires_at, limit)
    return dict(link)


MAX_MULTI_FILES = 500

def create_multi_link(
    paths: list[str],
    name: str | None = None,
    expires_in_days: int | float | None = None,
    max_downloads: int | None = None,
    owner: str = "",
    owner_role: str = "user",
) -> dict:
    """Create ONE share link covering several files (Photo Station multi-share).

    Every path must exist as a regular file. Paths are deduplicated in the
    given order and capped at MAX_MULTI_FILES. The stored link has `files`
    set and an empty `path`; the public endpoint renders a gallery page.
    """
    if not isinstance(paths, list) or not paths:
        raise ValueError("At least one file path is required")

    clean: list[str] = []
    seen: set[str] = set()
    for p in paths[:MAX_MULTI_FILES]:
        if not isinstance(p, str):
            continue
        ap = os.path.abspath(os.path.expanduser(p))
        if ap in seen:
            continue
        if not os.path.isfile(ap):
            raise ValueError(f"File not found: {ap}")
        seen.add(ap)
        clean.append(ap)
    if not clean:
        raise ValueError("At least one existing file is required")

    expires_at, limit = _validate_options(expires_in_days, max_downloads)
    label = (name or "").strip() or f"{len(clean)} photo{'s' if len(clean) != 1 else ''}"
    if len(label) > 200:
        label = label[:200]

    link = {
        "id": uuid.uuid4().hex,
        "path": "",
        "name": label,
        "files": clean,
        "token": secrets.token_urlsafe(16),
        "created_at": _now_iso(),
        "expires_at": expires_at,
        "max_downloads": limit,
        "downloads": 0,
        "owner": owner,
        "owner_role": owner_role,
    }

    with _LOCK:
        store = _load()
        store["links"].append(link)
        _save(store)
    logger.info("Multi share link created for %d files (expires=%s max_dl=%s)",
                len(clean), expires_at, limit)
    return dict(link)

def list_links() -> list[dict]:
    """All links, newest first, each with a derived `status` field."""
    store = _load()
    links = sorted(store["links"], key=lambda l: l.get("created_at", ""), reverse=True)
    return [with_status(l) for l in links]


def get_by_token(token: str) -> dict | None:
    """Look up a link by its public token (constant-time compare)."""
    if not token or not isinstance(token, str):
        return None
    store = _load()
    for link in store["links"]:
        stored = link.get("token", "")
        if stored and secrets.compare_digest(stored, token):
            return dict(link)
    return None


def delete_link(link_id: str) -> bool:
    """Remove a link by id. Returns True if something was deleted."""
    with _LOCK:
        store = _load()
        before = len(store["links"])
        store["links"] = [l for l in store["links"] if l.get("id") != link_id]
        if len(store["links"]) == before:
            return False
        _save(store)
    logger.info("Share link %s deleted", link_id)
    return True


def record_download(link_id: str) -> None:
    """Increment the download counter (best-effort; never raises)."""
    try:
        with _LOCK:
            store = _load()
            for link in store["links"]:
                if link.get("id") == link_id:
                    link["downloads"] = int(link.get("downloads", 0)) + 1
                    _save(store)
                    return
    except Exception as exc:  # noqa: BLE001 — counter must never break a download
        logger.warning("Could not record download for link %s: %s", link_id, exc)
