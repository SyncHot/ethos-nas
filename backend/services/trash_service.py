"""Per-user recycle bin shared by Files, Photos and the desktop shortcut.

Layout (all under ``settings.DATA_DIR``)::

    trash/<username>/<item>              # items deleted by that account
    trash/<username>/<item>.meta.json    # {original_path, owner, deleted_at, ...}
    trash/<username>/.namespace          # marker: "this directory is a user bin"
    trash/_legacy/...                    # pre-multi-user items (admin-only view)

The marker distinguishes a real namespace from a trashed *directory* that
happens to be called like a user. Every public function takes the owning
username explicitly — routers derive it from the JWT, never from the request
body — so one account can never list, restore or purge another account's items.
Admins may pass ``include_all`` to see every namespace (including ``_legacy``).

The old implementation kept one flat ``trash/`` directory with no owner in the
metadata. ``migrate_legacy()`` (startup, idempotent) moves those items into the
namespace of the user whose ``/home/<user>`` path they came from, or into
``_legacy`` when that cannot be determined.

Sidecar writes are atomic (temp file + ``os.replace``): a crash mid-write must
not leave an item that can no longer be restored.
"""

import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.config import settings

logger = logging.getLogger("ethos-nas.services.trash_service")

LEGACY_OWNER = "_legacy"
NAMESPACE_MARKER = ".namespace"
META_SUFFIX = ".meta.json"

_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}$")
_HOME_RE = re.compile(r"^/home/([^/]+)(?:/|$)")
_RESERVED_OWNERS = {LEGACY_OWNER, NAMESPACE_MARKER}

_MIGRATE_LOCK = threading.Lock()
_MIGRATED = False


# ── Paths & validation ───────────────────────────────────────────────────────

def root() -> Path:
    """`<DATA_DIR>/trash` — read at call time so tests can repoint DATA_DIR."""
    return settings.DATA_DIR / "trash"


def normalize_owner(username: str) -> str:
    """Validate a username before it becomes a path component."""
    owner = str(username or "").strip()
    if owner in _RESERVED_OWNERS or not _OWNER_RE.match(owner):
        raise ValueError(f"Invalid trash owner: {username!r}")
    return owner


def valid_item_name(name: str) -> str:
    """A trash entry name must be a single, non-bookkeeping path component."""
    value = str(name or "")
    if (not value or "/" in value or "\\" in value
            or value in (".", "..") or value == NAMESPACE_MARKER
            or value.endswith(META_SUFFIX)):
        raise ValueError("Invalid trash item name")
    return value


def _meta_path(item: Path) -> Path:
    return item.parent / f"{item.name}{META_SUFFIX}"


TRASH_ACTOR_FALLBACK = "default"


def actor_from_claims(claims: dict | None) -> tuple[str, bool]:
    """Derive ``(username, is_admin)`` from the JWT payload on the request."""
    payload = claims or {}
    username = str(payload.get("username") or payload.get("sub") or TRASH_ACTOR_FALLBACK)
    return username, str(payload.get("role") or "user") == "admin"


def target_owner(username: str, is_admin: bool, owner: str | None) -> tuple[str, bool]:
    """Resolve whose trash a request targets.

    Non-admins may only touch their own namespace. Raises ``PermissionError``
    so both routers translate it into a 403 without duplicating the policy.
    """
    if owner and owner != username:
        if not is_admin:
            raise PermissionError("Admin access required")
        return owner, True
    return username, is_admin


def _is_meta(entry: Path) -> bool:
    return entry.name.endswith(META_SUFFIX)


def _is_namespace(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    return (path / NAMESPACE_MARKER).is_file()


def _write_json_atomic(path: Path, payload) -> None:
    """Write JSON via a temp file in the same directory + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # best-effort cleanup — the original error is re-raised below
        raise


def _read_meta(item: Path) -> dict:
    meta_file = _meta_path(item)
    if not meta_file.is_file():
        return {}
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning("Trash: unreadable metadata for %s: %s", item.name, exc)
        return {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mtime_iso(entry: Path) -> str:
    try:
        stamp = entry.stat().st_mtime
    except OSError:
        stamp = time.time()
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")


def _item_size(entry: Path) -> int:
    """Regular file size, or -1 for directories (walking huge trees is not free)."""
    try:
        return entry.stat().st_size if entry.is_file() and not entry.is_symlink() else -1
    except OSError:
        return -1


def _entry_size(entry: Path) -> int:
    """Bytes for retention accounting; walks directories."""
    if entry.is_symlink():
        return 0
    try:
        if entry.is_file():
            return entry.stat().st_size
    except OSError:
        return 0
    total = 0
    for walk_root, _dirs, files in os.walk(entry):
        for name in files:
            try:
                total += (Path(walk_root) / name).stat().st_size
            except OSError:
                continue
    return total


# ── Namespaces ───────────────────────────────────────────────────────────────

def namespace_dir(owner: str, create: bool = False) -> Path:
    """Directory holding one user's trashed items."""
    owner = normalize_owner(owner)
    directory = root() / owner
    if create:
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / NAMESPACE_MARKER
        if not marker.exists():
            _write_json_atomic(marker, {"owner": owner, "kind": "user"})
    return directory


def _legacy_dir(create: bool = True) -> Path:
    directory = root() / LEGACY_OWNER
    if create:
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / NAMESPACE_MARKER
        if not marker.exists():
            _write_json_atomic(marker, {"owner": LEGACY_OWNER, "kind": "legacy"})
    return directory


def _owner_dir(owner: str) -> Path:
    """Namespace directory for per-item operations.

    `_legacy` is normally a reserved name (normalize_owner rejects it), but the
    admin all-users view surfaces those items as a pseudo-owner — restores and
    permanent deletes must work on them too. Callers are responsible for the
    admin check.
    """
    if owner == LEGACY_OWNER:
        return root() / LEGACY_OWNER
    return namespace_dir(owner)


def _namespace_dirs(include_legacy: bool = False) -> list[Path]:
    base = root()
    if not base.is_dir():
        return []
    out = []
    for entry in base.iterdir():
        if not _is_namespace(entry):
            continue
        if entry.name == LEGACY_OWNER and not include_legacy:
            continue
        out.append(entry)
    return out


# ── Operations ───────────────────────────────────────────────────────────────

def reserve_dest(src: str, owner: str) -> tuple[Path, dict]:
    """Reserve a collision-free trash name for *src* and build its metadata.

    The caller performs the move and then calls :func:`finalize_move`. Keeping
    the two apart lets the background trash worker copy with byte progress
    while naming and metadata stay in one place.
    """
    _ensure_migrated()
    owner = normalize_owner(owner)
    dest_dir = namespace_dir(owner, create=True)
    name = os.path.basename(src.rstrip(os.sep)) or "item"
    dest = dest_dir / name
    counter = 0
    while os.path.lexists(dest) or _meta_path(dest).exists():
        counter += 1
        dest = dest_dir / f"{name}_{counter}"
    meta = {
        "original_path": src,
        "trashed_name": dest.name,
        "owner": owner,
        "deleted_at": _now_iso(),
        # isdir() follows symlinks; a trashed link is not a directory.
        "is_dir": os.path.isdir(src) and not os.path.islink(src),
        "size": _item_size(Path(src)),
    }
    return dest, meta


def finalize_move(dest: Path, meta: dict) -> dict:
    """Write the sidecar metadata for an item that has been moved to trash."""
    try:
        _write_json_atomic(_meta_path(dest), meta)
    except OSError as exc:
        # The item is already in the trash; log loudly but do not lose it.
        logger.error("Trash: could not write metadata for %s: %s", dest, exc)
    return {"name": dest.name, "owner": meta["owner"],
            "original_path": meta["original_path"]}


def move_to_trash(src: str, owner: str, original_path: str | None = None) -> dict:
    """Move *src* into *owner*'s trash; returns {name, owner, original_path}.

    Callers must have validated the *source* path against the account's scope
    (routers call ``path_scope.sanitize``) — this function only owns storage.
    """
    dest, meta = reserve_dest(src, owner)
    if original_path:
        meta["original_path"] = original_path
    try:
        shutil.move(src, str(dest))
    except Exception:
        # Cross-device moves are copy+delete — drop the partial copy so the
        # trash never lists an item that is still half on the source disk.
        with contextlib.suppress(OSError):
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            else:
                dest.unlink(missing_ok=True)
        raise
    return finalize_move(dest, meta)


def resolve_item(owner: str, name: str) -> tuple[Path, Path, dict]:
    """(item, meta file, metadata) inside one owner's namespace.

    Raises ValueError for a malformed name and FileNotFoundError when the item
    does not belong to that owner (the two are indistinguishable from outside
    on purpose).
    """
    _ensure_migrated()
    name = valid_item_name(name)
    directory = _owner_dir(owner)
    src = directory / name
    if not src.exists() and not src.is_symlink():
        raise FileNotFoundError(name)
    return src, _meta_path(src), _read_meta(src)


def list_items(owner: str, include_all: bool = False, limit: int = 0, offset: int = 0) -> dict:
    """Trash contents for one owner, or every namespace when `include_all`."""
    _ensure_migrated()
    entries: list[tuple[str, Path]] = []
    if include_all:
        for directory in _namespace_dirs(include_legacy=True):
            entries.extend((directory.name, item) for item in _items_in(directory))
        entries.extend((LEGACY_OWNER, item) for item in _flat_legacy_items())
    else:
        directory = namespace_dir(owner)
        if directory.is_dir():
            entries.extend((normalize_owner(owner), item) for item in _items_in(directory))

    items = []
    for item_owner, entry in entries:
        meta = _read_meta(entry)
        items.append({
            "name": entry.name,
            "original_path": meta.get("original_path", ""),
            "is_dir": bool(meta.get("is_dir", entry.is_dir())),
            "owner": meta.get("owner", item_owner),
            "deleted_at": meta.get("deleted_at", ""),
            "size": meta.get("size", _item_size(entry)),
        })
    # Newest first; entries without metadata (legacy orphans) sort by name.
    items.sort(key=lambda it: (it["deleted_at"], it["name"]), reverse=True)
    total = len(items)
    window = items[offset:offset + limit] if limit else items[offset:]
    return {"items": window, "total": total, "limit": limit, "offset": offset}


def empty(owner: str | None, include_all: bool = False) -> tuple[int, int]:
    """Permanently delete trash items; returns (deleted_items, failed)."""
    _ensure_migrated()
    targets: list[Path] = []
    if include_all:
        for directory in _namespace_dirs(include_legacy=True):
            targets.extend(_items_in(directory))
        targets.extend(_flat_legacy_items())
    else:
        directory = namespace_dir(str(owner))
        if directory.is_dir():
            targets.extend(_items_in(directory))

    count = failed = 0
    for item in targets:
        try:
            _remove_item(item)
            count += 1
        except OSError as exc:
            logger.warning("Empty trash: failed to remove %s: %s", item, exc)
            failed += 1
    return count, failed


def delete_item(owner: str, name: str) -> str:
    """Permanently delete one item from one owner's trash."""
    _ensure_migrated()
    src, _meta, _data = resolve_item(owner, name)
    _remove_item(src)
    return name


def _items_in(directory: Path) -> list[Path]:
    out = []
    for entry in directory.iterdir():
        if entry.name == NAMESPACE_MARKER or _is_meta(entry):
            continue
        out.append(entry)
    return out


def _flat_legacy_items() -> list[Path]:
    """Un-migrated items still sitting directly in trash/ (old layout)."""
    base = root()
    if not base.is_dir():
        return []
    out = []
    for entry in base.iterdir():
        if _is_namespace(entry) or _is_meta(entry) or entry.name == NAMESPACE_MARKER:
            continue
        out.append(entry)
    return out


def _remove_item(item: Path) -> None:
    if item.is_dir() and not item.is_symlink():
        shutil.rmtree(item)
    else:
        item.unlink()
    _meta_path(item).unlink(missing_ok=True)


# ── Retention & usage ────────────────────────────────────────────────────────

def sweep_retention(max_age_seconds: float) -> dict:
    """Remove items older than the cutoff (per item, never per namespace)."""
    _ensure_migrated()
    cutoff = time.time() - max_age_seconds
    removed = failed = 0
    freed = 0
    candidates: list[Path] = []
    for directory in _namespace_dirs(include_legacy=True):
        candidates.extend(_items_in(directory))
    candidates.extend(_flat_legacy_items())
    for item in candidates:
        try:
            if item.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        size = _entry_size(item)
        try:
            _remove_item(item)
            removed += 1
            freed += size
        except OSError as exc:
            logger.warning("Retention: could not remove trash item %s: %s", item, exc)
            failed += 1
    return {"removed_files": removed, "freed_bytes": freed, "failed": failed}


def usage() -> dict:
    """Item count + bytes in the recycle bin (sidecars/markers excluded)."""
    _ensure_migrated()
    files = 0
    size = 0
    candidates: list[Path] = []
    for directory in _namespace_dirs(include_legacy=True):
        candidates.extend(_items_in(directory))
    candidates.extend(_flat_legacy_items())
    for item in candidates:
        files += 1
        size += _entry_size(item)
    return {"path": str(root()), "files": files, "bytes": size}


# ── Legacy migration ─────────────────────────────────────────────────────────

def _owner_from_original(original: str) -> str | None:
    match = _HOME_RE.match(str(original or ""))
    if not match:
        return None
    candidate = match.group(1)
    try:
        return normalize_owner(candidate)
    except ValueError:
        return None


def migrate_legacy() -> dict:
    """Move the old flat trash into per-user namespaces. Idempotent."""
    with _MIGRATE_LOCK:
        base = root()
        base.mkdir(parents=True, exist_ok=True)
        moved = failed = 0
        for entry in list(base.iterdir()):
            if _is_namespace(entry) or _is_meta(entry):
                continue  # already migrated (or an orphan sidecar, left in place)
            meta = _read_meta(entry)
            owner = _owner_from_original(meta.get("original_path", "")) or LEGACY_OWNER
            try:
                dest_dir = _legacy_dir() if owner == LEGACY_OWNER else namespace_dir(owner, create=True)
                dest = dest_dir / entry.name
                counter = 0
                while dest.exists() or _meta_path(dest).exists():
                    counter += 1
                    dest = dest_dir / f"{entry.name}_{counter}"
                shutil.move(str(entry), str(dest))
            except OSError as exc:
                logger.error("Trash migration: could not move %s: %s", entry, exc)
                failed += 1
                continue
            meta.update({
                "trashed_name": dest.name,
                "owner": owner,
                "deleted_at": meta.get("deleted_at") or _mtime_iso(dest),
                "is_dir": dest.is_dir(),
                "size": meta.get("size", _item_size(dest)),
            })
            try:
                _write_json_atomic(_meta_path(dest), meta)
            except OSError as exc:
                logger.error("Trash migration: metadata write failed for %s: %s", dest, exc)
                failed += 1
                continue
            moved += 1
        if moved or failed:
            logger.info("Trash migration: %d item(s) moved into per-user namespaces, %d failed",
                        moved, failed)
        return {"moved": moved, "failed": failed}


def _ensure_migrated() -> None:
    """Run the one-time migration lazily (startup also calls it explicitly)."""
    global _MIGRATED
    if _MIGRATED:
        return
    try:
        migrate_legacy()
    except Exception:
        logger.error("Trash migration failed", exc_info=True)
    finally:
        _MIGRATED = True
