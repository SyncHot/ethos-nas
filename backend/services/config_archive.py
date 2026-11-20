"""Config archive — export, import and nightly snapshots of the NAS state (TODO.md P5-04).

The only irreplaceable state in EthOS is `<ETHOS_DATA_DIR>/`: ~40 JSON stores, the
`gallery_index.db` SQLite catalog and the files the UI references. This module owns
three operations on it:

  export_bytes()   — zip every `*.json` store + a consistent copy of the SQLite index
                     + `manifest.json` (app version, stamp, sizes)
  import_bytes()   — validated restore that snapshots the current stores first
  take_snapshot()  — the same archive written to `<DATA_DIR>/snapshots/`, pruned to
                     `CONFIG_SNAPSHOT_KEEP`; `maybe_snapshot()` is called by the
                     retention sweeper so a fresh archive exists every day

Security: an export contains secrets (`users.json` → password hashes and API keys),
so archives are written mode 0600 and the router endpoints are admin-only. Imports
accept flat `*.json` names only (no paths, no dotfiles), every JSON payload must
parse, and the SQLite payload must be a real database — a rejected archive leaves
the live stores untouched.
"""

import io
import json
import logging
import os
import pathlib
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from backend.config import APP_VERSION, settings

logger = logging.getLogger("ethos-nas.services.config_archive")

DB_NAME = "gallery_index.db"
MANIFEST_NAME = "manifest.json"
MAX_BYTES = 64 * 1024 * 1024
_SNAPSHOT_PREFIX = "ethos-config-"


class ArchiveError(Exception):
    """An archive the operator handed us is not usable (message is user-facing)."""


# --------------------------------------------------------------------------- paths
def snapshot_dir() -> Path:
    return settings.DATA_DIR / "snapshots"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def list_snapshots() -> list[dict]:
    """Newest first: `{name, bytes, created_at}`."""
    d = snapshot_dir()
    if not d.is_dir():
        return []
    out = []
    for path in sorted(d.glob(_SNAPSHOT_PREFIX + "*.zip"), reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append({"name": path.name, "bytes": stat.st_size, "created_at": stat.st_mtime})
    return out


# ------------------------------------------------------------------------- sqlite
def _sqlite_copy(src_path: Path, dest_path: Path) -> bool:
    """Consistent copy of a live database (safe while WAL writers run)."""
    if not src_path.exists():
        return False
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return True


def _sqlite_restore(payload: bytes, backup_path: Path | None = None,
                    target: Path | None = None) -> None:
    """Overwrite a live index through SQLite's backup API (keeps WAL mode)."""
    live = Path(target) if target is not None else settings.DATA_DIR / DB_NAME
    live.parent.mkdir(parents=True, exist_ok=True)
    if backup_path is not None and live.exists():
        try:
            _sqlite_copy(live, backup_path)
        except sqlite3.Error:
            logger.warning("Could not snapshot %s before import", live, exc_info=True)
    with tempfile.TemporaryDirectory() as tmp:
        candidate = pathlib.Path(tmp) / "imported.db"
        candidate.write_bytes(payload)
        try:
            src = sqlite3.connect(candidate)
        except sqlite3.Error as exc:
            raise ArchiveError(f"{DB_NAME} is not a SQLite database") from exc
        dst = sqlite3.connect(live)
        try:
            src.backup(dst)
        except sqlite3.DatabaseError as exc:
            raise ArchiveError(f"{DB_NAME} is not a SQLite database") from exc
        finally:
            dst.close()
            src.close()


# ------------------------------------------------------------------------- export
def export_bytes() -> bytes:
    """Zip the whole JSON state plus the gallery indexes. Raises OSError on disk errors."""
    buf = io.BytesIO()
    users_dir = settings.DATA_DIR / "users"
    files = sorted(p for p in settings.DATA_DIR.glob("*.json") if p.is_file())
    if users_dir.is_dir():
        # Per-user libraries live in DATA_DIR/users/<name>/ — include them.
        files += sorted(p for p in users_dir.rglob("*.json") if p.is_file())
    manifest = {
        "app_version": APP_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(settings.DATA_DIR),
        "files": [],
    }
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        skipped: list[dict] = []
        for path in files:
            try:
                payload = path.read_bytes()
            except OSError as exc:
                # One unreadable file (e.g. a 0600 snapshot left by a dev shell)
                # must not fail the whole scheduled backup.
                logger.warning("Config snapshot: skipping unreadable %s: %s", path.name, exc)
                skipped.append({"name": path.name, "error": str(exc)})
                continue
            name = path.relative_to(settings.DATA_DIR).as_posix()
            zf.writestr(name, payload)
            manifest["files"].append({"name": name, "bytes": len(payload)})
        packages_dir = settings.DATA_DIR / "packages"
        if packages_dir.is_dir():
            for path in sorted(packages_dir.glob("*.json")):
                if path.name not in ("installed.json", "sources.json"):
                    continue
                try:
                    payload = path.read_bytes()
                except OSError as exc:
                    logger.warning("Config snapshot: skipping unreadable packages/%s: %s", path.name, exc)
                    skipped.append({"name": f"packages/{path.name}", "error": str(exc)})
                    continue
                name = f"packages/{path.name}"
                zf.writestr(name, payload)
                manifest["files"].append({"name": name, "bytes": len(payload)})
        if skipped:
            manifest["skipped"] = skipped
        with tempfile.TemporaryDirectory() as tmp:
            db_paths = [settings.DATA_DIR / DB_NAME]
            if users_dir.is_dir():
                db_paths += sorted(users_dir.glob(f"*/{DB_NAME}"))
            for db_path in db_paths:
                snapshot = pathlib.Path(tmp) / (db_path.parent.name + "-" + DB_NAME)
                if not _sqlite_copy(db_path, snapshot):
                    continue
                name = db_path.relative_to(settings.DATA_DIR).as_posix()
                payload = snapshot.read_bytes()
                zf.writestr(name, payload)
                manifest["files"].append({"name": name, "bytes": len(payload)})
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
    return buf.getvalue()


# ------------------------------------------------------------------------- import
def import_bytes(raw: bytes) -> dict:
    """Restore stores from an export archive; snapshots the current state first."""
    from backend.services import json_store

    if len(raw) > MAX_BYTES:
        raise ArchiveError("Archive too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ArchiveError("Not a zip archive") from exc

    names = [n for n in zf.namelist() if n != MANIFEST_NAME]
    if not names:
        raise ArchiveError("Archive contains no stores")
    for name in names:
        if name == DB_NAME:
            continue
        # Per-user stores: users/<safe-name>/<file>.json or …/gallery_index.db
        if name.startswith("users/"):
            parts = name.split("/")
            if (len(parts) != 3
                    or not all(p and p not in (".", "..") and "\\" not in p for p in parts)
                    or not (parts[2] == DB_NAME or parts[2].endswith(".json"))):
                raise ArchiveError(f"Refusing unsafe entry: {name}")
            continue
        # Flat names plus the two package state stores — never arbitrary paths.
        if name.startswith("packages/"):
            if name not in ("packages/installed.json", "packages/sources.json"):
                raise ArchiveError(f"Refusing unsafe entry: {name}")
            continue
        if not name.endswith(".json") or "/" in name or "\\" in name or name.startswith("."):
            raise ArchiveError(f"Refusing unsafe entry: {name}")

    parsed: dict[str, object] = {}
    db_payloads: dict[str, bytes] = {}
    for name in names:
        if name == DB_NAME or name.endswith("/" + DB_NAME):
            db_payloads[name] = zf.read(name)
            continue
        try:
            parsed[name] = json.loads(zf.read(name))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArchiveError(f"{name} is not valid JSON") from exc

    backup_dir = settings.DATA_DIR / f"import-backup-{int(time.time())}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    imported: list[str] = []
    for name, value in parsed.items():
        target = settings.DATA_DIR / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            try:
                backup_target = backup_dir / name
                backup_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup_target)
            except OSError:
                logger.warning("Could not snapshot %s before import", target, exc_info=True)
        json_store.get(target, dict).save(value)
        imported.append(name)

    for name, payload in db_payloads.items():
        target = settings.DATA_DIR / name
        _sqlite_restore(payload, backup_dir / name, target=target)
        imported.append(name)

    logger.warning("Config import: restored %d item(s) into %s (snapshot: %s)",
                   len(imported), settings.DATA_DIR, backup_dir.name)
    return {"ok": True, "imported": imported, "count": len(imported),
            "backup_dir": str(backup_dir)}


# ---------------------------------------------------------------------- snapshots
def prune(keep: int | None = None) -> int:
    """Delete all but the newest `keep` snapshots. Returns how many were removed."""
    keep = settings.CONFIG_SNAPSHOT_KEEP if keep is None else keep
    entries = list_snapshots()
    removed = 0
    for entry in entries[keep:]:
        try:
            (snapshot_dir() / entry["name"]).unlink()
            removed += 1
        except OSError:
            logger.warning("Could not remove old snapshot %s", entry["name"], exc_info=True)
    return removed


def take_snapshot(reason: str = "manual") -> Path | None:
    """Write an archive into `<DATA_DIR>/snapshots/`. Never raises."""
    if not settings.CONFIG_SNAPSHOT_ENABLED:
        return None
    try:
        d = snapshot_dir()
        d.mkdir(parents=True, exist_ok=True)
        target = d / f"{_SNAPSHOT_PREFIX}{_stamp()}.zip"
        payload = export_bytes()
        tmp = target.with_suffix(".zip.tmp")
        tmp.write_bytes(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        removed = prune()
        logger.info("Config snapshot written (%s): %s (%.1f KB), pruned %d old",
                    reason, target.name, len(payload) / 1024, removed)
        return target
    except Exception:
        logger.error("Config snapshot (%s) failed", reason, exc_info=True)
        return None


def maybe_snapshot() -> Path | None:
    """Snapshot when the newest archive is older than `CONFIG_SNAPSHOT_INTERVAL`."""
    if not settings.CONFIG_SNAPSHOT_ENABLED:
        return None
    entries = list_snapshots()
    if entries:
        age = time.time() - entries[0]["created_at"]
        if age < settings.CONFIG_SNAPSHOT_INTERVAL:
            return None
    return take_snapshot("scheduled")
