"""Locked, atomic JSON state stores.

Every module that persists a JSON blob should use this instead of hand-rolling
`tempfile` + `os.replace` (that ritual existed in 14 copies) or a plain
`write_text` (three writers were neither atomic nor locked, so a crash or a
concurrent request truncated the file — and several stores silently reset to
defaults afterwards).

Usage:

    from backend.services import json_store

    _STORE = json_store.get(settings.DATA_DIR / "cron_jobs.json", dict, version=1)

    data = _STORE.load()          # default when missing/corrupt, never raises
    _STORE.save(data)             # atomic replace, mode 0600
    _STORE.update(lambda d: ...)  # atomic read-modify-write under one lock

Schema versions: pass `version=N` and, when the shape changes, a `migrate`
callable taking `(from_version, to_version, data)` and returning the upgraded
value (raise `ValueError` to abandon the upgrade). `load()` runs the migrator,
logs `store X migrated v1→v2` and writes the result back. A file written by a
newer build is never overwritten (downgrade guard) — `load()` returns the
default and logs an ERROR instead, so an accidental rollback cannot destroy data.
`store_registry_report()` exposes every registered store's declared version for
the `/api/system/doctor` check.

Corruption handling: the damaged file is renamed to `<name>.corrupt-<ts>.json`
(kept for inspection) and an ERROR is logged; callers get the default. A store
whose file was written by a *newer* version of the app is never overwritten —
`load()` returns the default and logs, so an accidental downgrade cannot destroy
data.
"""

import contextlib
import copy
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

logger = logging.getLogger("ethos-nas.services.json_store")

T = TypeVar("T")

# One Store per path per process, so the lock actually serialises all writers.
_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[Path, "Store"] = {}


class Store:
    """A single JSON file behind a lock with atomic writes."""

    def __init__(
        self,
        path: Path,
        default_factory: Callable[[], Any] = dict,
        *,
        version: int | None = None,
        migrate: Callable[[int, int, Any], Any] | None = None,
        indent: int = 2,
        mode: int = 0o600,
    ) -> None:
        self._path = Path(path)
        self._default_factory = default_factory
        self._version = version
        self._migrate = migrate
        self._indent = indent
        self._mode = mode
        self._lock = threading.RLock()

    # ── public API ────────────────────────────────────────────────────────

    def path(self) -> Path:
        return self._path

    def load(self) -> Any:
        """Return the stored value (deep copy), or the default.

        Never raises: unreadable/corrupt files are quarantined and logged.
        """
        with self._lock:
            if not self._path.exists():
                return self._default_factory()
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except PermissionError:
                # Unreadable *for this user* is not corruption. Quarantining here
                # renamed the service's stores whenever a dev shell / CI imported
                # backend modules with the production DATA_DIR — leave the bytes
                # alone and let the owning service read them.
                logger.error(
                    "Store %s is not readable by this user — leaving it untouched",
                    self._path, exc_info=True,
                )
                return self._default_factory()
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                logger.error("Store %s is unreadable — quarantining", self._path, exc_info=True)
                self._quarantine_unlocked()
                return self._default_factory()

            if self._version is not None and isinstance(data, dict):
                stored = data.get("version")
                if isinstance(stored, int) and stored > self._version:
                    logger.error(
                        "Store %s has version %s but this build understands %s — refusing to use it "
                        "(downgrade guard). Data left untouched.",
                        self._path, stored, self._version,
                    )
                    return self._default_factory()
                if isinstance(stored, int) and stored < self._version:
                    data = self._migrate_unlocked(data, stored)
                    if data is None:
                        return self._default_factory()
                data.setdefault("version", self._version)
            return copy.deepcopy(data)

    def save(self, data: Any) -> None:
        """Write atomically (temp file in the same dir + fsync + os.replace)."""
        with self._lock:
            self._write_unlocked(data)

    def update(self, mutator: Callable[[Any], T]) -> T:
        """Atomic read-modify-write; `mutator` mutates the value and returns the result."""
        with self._lock:
            data = self.load()
            result = mutator(data)
            self._write_unlocked(data)
            return result

    def quarantine(self, reason: str = "") -> Path | None:
        with self._lock:
            return self._quarantine_unlocked(reason)

    # ── internals ─────────────────────────────────────────────────────────

    def _migrate_unlocked(self, data: dict, stored: int) -> dict | None:
        """Upgrade an old payload in place; returns None when the upgrade is unsafe."""
        assert self._version is not None
        if self._migrate is None:
            logger.warning(
                "Store %s is at version %s but this build writes %s and no migrator is registered — "
                "using it as-is. Register `migrate=` before changing the shape.",
                self._path, stored, self._version,
            )
            return data
        backup = self._path.with_name(f"{self._path.stem}.v{stored}-{int(time.time())}{self._path.suffix}")
        try:
            shutil.copy2(self._path, backup)
        except OSError:
            logger.warning("Could not back up %s before migration", self._path, exc_info=True)
            backup = None
        try:
            upgraded = self._migrate(stored, self._version, data)
        except Exception:
            logger.error("Migration of %s v%s→v%s failed — data left untouched",
                         self._path, stored, self._version, exc_info=True)
            return None
        if not isinstance(upgraded, dict):
            logger.error("Migrator for %s returned %s, expected dict — data left untouched",
                         self._path, type(upgraded).__name__)
            return None
        upgraded["version"] = self._version
        try:
            self._write_unlocked(upgraded)
        except OSError:
            logger.error("Migrated %s in memory but could not persist it", self._path, exc_info=True)
        logger.info("Store %s migrated v%s→v%s (backup: %s)",
                    self._path.name, stored, self._version, backup.name if backup else "none")
        return upgraded

    def _write_unlocked(self, data: Any) -> None:
        if self._version is not None and isinstance(data, dict):
            data.setdefault("version", self._version)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self._path.stem}-", suffix=".tmp",
                                   dir=str(self._path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=self._indent)
                fh.flush()
                os.fsync(fh.fileno())
            with contextlib.suppress(OSError):
                os.chmod(tmp, self._mode)
            os.replace(tmp, self._path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            logger.error("Failed to write store %s", self._path, exc_info=True)
            raise

    def _quarantine_unlocked(self, reason: str = "") -> Path | None:
        if not self._path.exists():
            return None
        dest = self._path.with_name(f"{self._path.stem}.corrupt-{int(time.time())}{self._path.suffix}")
        try:
            os.replace(self._path, dest)
        except OSError:
            logger.error("Could not quarantine damaged store %s", self._path, exc_info=True)
            return None
        logger.error("Damaged store quarantined to %s%s", dest, f" ({reason})" if reason else "")
        try:
            from backend.services.notifications_service import emit_event
            emit_event("error", "Damaged settings file",
                       f"{self._path.name} was unreadable and has been reset (backup: {dest.name})",
                       source="storage")
        except Exception:
            logger.debug("Could not emit quarantine notification", exc_info=True)
        return dest


def get(path: Path, default_factory: Callable[[], Any] = dict,
        *, version: int | None = None, mode: int = 0o600,
        migrate: Callable[[int, int, Any], Any] | None = None) -> Store:
    """Return the process-wide Store for `path` (created on first use)."""
    key = Path(path)
    with _REGISTRY_LOCK:
        store = _REGISTRY.get(key)
        if store is None:
            store = Store(key, default_factory, version=version, mode=mode, migrate=migrate)
            _REGISTRY[key] = store
        return store


def verify_versions() -> dict:
    """Startup sweep: load every versioned store so migrations run and logs are complete.

    A downgrade (a file written by a newer build) does not abort startup — the NAS UI
    must stay reachable so the operator can see the problem — but it is reported here and
    by `/api/system/doctor`, and the affected store is never overwritten.
    """
    with _REGISTRY_LOCK:
        stores = list(_REGISTRY.values())
    checked = migrated = 0
    downgrades: list[dict] = []
    for store in stores:
        if store._version is None:
            continue
        checked += 1
        before = None
        try:
            payload = json.loads(store.path().read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                before = payload.get("version")
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            before = None
        try:
            store.load()
        except Exception:
            logger.error("Store %s failed its version check", store.path(), exc_info=True)
            continue
        if isinstance(before, int) and before > store._version:
            downgrades.append({"store": store.path().name, "on_disk": before,
                               "supported": store._version})
        elif isinstance(before, int) and before < store._version:
            migrated += 1
    if downgrades:
        logger.error("Schema downgrade detected for %d store(s): %s — data left untouched",
                     len(downgrades), ", ".join(d["store"] for d in downgrades))
    else:
        logger.info("Store schema check: %d versioned store(s) OK, %d migrated", checked, migrated)
    return {"checked": checked, "migrated": migrated, "downgrades": downgrades}


def store_registry_report() -> list[dict]:
    """Every registered store with its declared schema version (for `/system/doctor`)."""
    with _REGISTRY_LOCK:
        entries = list(_REGISTRY.values())
    out = []
    for store in entries:
        out.append({
            "path": str(store.path()),
            "version": store._version,
            "migrator": store._migrate is not None,
            "exists": store.path().exists(),
        })
    return sorted(out, key=lambda e: e["path"])
