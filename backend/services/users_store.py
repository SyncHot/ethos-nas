"""Single, locked, atomic accessor for the EthOS account store.

The store used to live in two places — `backend/users.json` (read by `auth.py`)
and `backend/routers/users.json` (written by `auth_pam.py`) — so a login could
seed one file while API-key registration wrote the other, and neither write was
atomic. Everything now goes through this module.

Location: `<ETHOS_DATA_DIR>/users.json` (mode 0600). On first run in the default
data dir the legacy files are migrated automatically; an isolated test instance
(`ETHOS_DATA_DIR` elsewhere) always starts empty so it can never inherit — or
overwrite — production accounts.
"""

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, TypeVar

from backend.config import settings

logger = logging.getLogger("ethos-nas.services.users_store")

T = TypeVar("T")

# Account lifecycle states. A missing `status` (records written before the
# lifecycle feature) means "active"; a deleted account keeps a tombstone so an
# already-issued JWT/API key cannot keep working after the Linux account is gone.
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUS_DELETED = "deleted"

_LOCK = threading.Lock()

# Middleware reads the store on every JWT request, so a tiny signature+TTL
# cache avoids re-parsing users.json for each thumbnail. A write changes the
# file signature and invalidates it immediately.
_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"signature": None, "at": 0.0, "users": []}
CACHE_TTL = 2.0

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_DATA_DIR = _BACKEND_DIR / "data"
# Old locations, most authoritative first (the routers file holds the real
# accounts; the backend one was a seed stub).
_LEGACY_PATHS = (_BACKEND_DIR / "routers" / "users.json", _BACKEND_DIR / "users.json")


def path() -> Path:
    """Current store path — resolved per call so ETHOS_DATA_DIR is honoured."""
    return settings.DATA_DIR / "users.json"


def _is_production_data_dir() -> bool:
    try:
        return settings.DATA_DIR.resolve() == _DEFAULT_DATA_DIR.resolve()
    except OSError:
        return False


def _normalise(data: object) -> list[dict]:
    if isinstance(data, dict):
        data = data.get("users", [])
    return [u for u in data if isinstance(u, dict)] if isinstance(data, list) else []


def _write_unlocked(target: Path, users: list[dict]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".users-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(users, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _invalidate_cache()


def _quarantine_unlocked(target: Path) -> None:
    dest = target.with_name(f"users.corrupt-{int(time.time())}.json")
    try:
        os.replace(target, dest)
        logger.error("Damaged user store quarantined to %s", dest)
    except OSError:
        logger.error("Could not quarantine damaged user store %s", target, exc_info=True)


def _read_unlocked(target: Path) -> list[dict]:
    if not target.exists():
        return []
    try:
        return _normalise(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        logger.error("User store %s is unreadable", target, exc_info=True)
        _quarantine_unlocked(target)
        return []


def _migrate_unlocked(target: Path) -> None:
    if target.exists() or not _is_production_data_dir():
        return
    for src in _LEGACY_PATHS:
        if not src.exists():
            continue
        try:
            users = _normalise(json.loads(src.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            logger.warning("Legacy user store %s is unreadable — skipping", src, exc_info=True)
            continue
        if users:
            _write_unlocked(target, users)
            logger.info("Migrated %d user(s) from %s to %s", len(users), src, target)
            return


def load() -> list[dict]:
    """Return all registered users (empty list when the store is absent)."""
    with _LOCK:
        target = path()
        _migrate_unlocked(target)
        return _read_unlocked(target)


def _signature(target: Path):
    try:
        stat = target.stat()
    except OSError:
        return None
    return (str(target), stat.st_mtime_ns, stat.st_size)


def _invalidate_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.update(signature=None, at=0.0, users=[])


def load_cached() -> list[dict]:
    """`load()` behind a short cache keyed by the file signature.

    Used by the auth middleware, which runs for every request. A concurrent
    write changes mtime/size and forces a reload on the next call; the TTL only
    bounds how long an unchanged file may keep being served from memory.
    """
    target = path()
    signature = _signature(target)
    now = time.monotonic()
    with _CACHE_LOCK:
        if (signature is not None and _CACHE["signature"] == signature
                and now - _CACHE["at"] < CACHE_TTL):
            return [dict(user) for user in _CACHE["users"]]

    users = load()
    with _CACHE_LOCK:
        _CACHE.update(signature=_signature(target), at=time.monotonic(),
                      users=[dict(user) for user in users])
    return [dict(user) for user in users]


def new_id() -> str:
    """Stable unique id for a new account record.

    The old `str(len(users) + 1)` scheme collided after any deletion/revival;
    ids are opaque strings, so legacy records keep their numeric ids.
    """
    return uuid.uuid4().hex


def status_of(user: dict) -> str:
    return str(user.get("status") or STATUS_ACTIVE)


def is_active(user: dict) -> bool:
    return status_of(user) == STATUS_ACTIVE


def find_username(username: str, users: list[dict] | None = None) -> dict | None:
    """Return the record for `username` from `users` (default: the cached store)."""
    if not username:
        return None
    for user in (users if users is not None else load_cached()):
        if str(user.get("username") or "") == username:
            return user
    return None


def save(users: list[dict]) -> None:
    """Replace the whole store atomically."""
    with _LOCK:
        _migrate_unlocked(path())
        _write_unlocked(path(), users)


def update(mutator: Callable[[list[dict]], T]) -> T:
    """Atomically read-modify-write the store under the lock.

    `mutator` receives the live list, mutates it in place and returns whatever
    the caller needs. Concurrent requests and worker threads cannot interleave
    a read-modify-write cycle.
    """
    with _LOCK:
        target = path()
        _migrate_unlocked(target)
        users = _read_unlocked(target)
        result = mutator(users)
        _write_unlocked(target, users)
        return result
