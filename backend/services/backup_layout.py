"""Backup Manager v2 — on-disk layout and legacy migration.

Every schedule owns a target folder:

    <destination>/<basename(source)>/                  folder
    <destination>/docker/<basename(source)>/           docker-compose

Inside it:

    snapshots/<YYYY-MM-DDTHH-MM-SS>/manifest.json      completed generations
    snapshots/<...>/data/…                             folder payload
    snapshots/<...>/projects/<name>/{config,volumes,images}/   docker payload
    .tmp-<op_id>/                                      in-flight workspace
    .ethos-backup.json                                 layout marker

A generation only appears under `snapshots/` after a successful run (the
workspace is renamed atomically). The pre-2.0 layout wrote straight into the
target root; `ensure_layout()` moves that payload into a `legacy` generation
once, without copying data (same filesystem, so renames are instant).
"""

import json
import logging
import os
import shutil
import stat
import threading
import time
from typing import Optional

from backend.services.backup_store import backup_target, now_iso, safe_name

logger = logging.getLogger("ethos-nas.services.backup_layout")

SNAPSHOTS_DIR = "snapshots"
MARKER_NAME = ".ethos-backup.json"
WORKSPACE_PREFIX = ".tmp-"
LAYOUT_VERSION = 2
_MIGRATE_LOCK = threading.Lock()


def snapshots_root(schedule: dict) -> str:
    return os.path.join(backup_target(schedule), SNAPSHOTS_DIR)


def generation_path(schedule: dict, generation: str) -> str:
    if not generation or "/" in generation or generation.startswith("."):
        raise ValueError("Invalid generation name")
    return os.path.join(snapshots_root(schedule), generation)


def marker_path(schedule: dict) -> str:
    return os.path.join(backup_target(schedule), MARKER_NAME)


def workspace_path(schedule: dict, op_id: str) -> str:
    return os.path.join(backup_target(schedule), f"{WORKSPACE_PREFIX}{safe_name(op_id)}")


def new_generation_name(when: Optional[float] = None) -> str:
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime(when or time.time()))
    return stamp


def unique_generation_name(schedule: dict, when: Optional[float] = None) -> str:
    base = new_generation_name(when)
    candidate = base
    index = 2
    while os.path.exists(os.path.join(snapshots_root(schedule), candidate)):
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _write_json_atomic(path: str, payload: dict) -> None:
    """Atomic write for backup artifacts (manifests/markers, not app state).

    `json_store` is for the app's own stores; manifests live inside backup data
    and must never be quarantined or trigger a settings notification.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        logger.error("Could not write backup metadata %s", path, exc_info=True)
        try:
            os.unlink(tmp)
        except OSError:  # intentional: temp file may not exist
            pass
        raise


def read_marker(schedule: dict) -> dict:
    try:
        with open(marker_path(schedule), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("Backup marker unreadable at %s", marker_path(schedule), exc_info=True)
        return {}


def write_marker(schedule: dict) -> None:
    _write_json_atomic(marker_path(schedule), {
        "version": LAYOUT_VERSION,
        "schedule_id": schedule.get("id"),
        "type": schedule.get("type"),
        "target": backup_target(schedule),
        "updated_at": now_iso(),
    })


def read_manifest(generation_dir: str) -> dict:
    path = os.path.join(generation_dir, "manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("Backup manifest unreadable at %s", path, exc_info=True)
        return {}


def write_manifest(generation_dir: str, manifest: dict) -> None:
    _write_json_atomic(os.path.join(generation_dir, "manifest.json"), manifest)


def measure_generation(generation_dir: str, btype: str) -> dict:
    """Size and regular-file count of a generation payload (no symlink follow).

    Used for generations that predate the manifest (legacy migration) and for
    manifests created before sizes were recorded. Walking 60 GB / 20k files is
    fast; the result is cached in the manifest, so it happens once.
    """
    payload = os.path.join(generation_dir, "data" if btype == "folder" else "projects")
    total = 0
    files = 0
    if not os.path.isdir(payload):
        return {"size_bytes": 0, "files_total": 0}
    for dirpath, _dirnames, filenames in os.walk(payload, followlinks=False):
        for name in filenames:
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                logger.debug("Could not stat %s/%s", dirpath, name, exc_info=True)
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                files += 1
    return {"size_bytes": total, "files_total": files}


def fill_missing_sizes(schedule: dict) -> bool:
    """Compute and persist sizes for generations whose manifest has none."""
    changed = False
    for entry in list_generations(schedule):
        if entry.get("size_bytes") is not None:
            continue
        generation_dir = generation_path(schedule, entry["generation"])
        stats = measure_generation(generation_dir, str(entry.get("type") or schedule.get("type")))
        manifest = read_manifest(generation_dir)
        manifest.update(stats)
        manifest["stats_indexed_at"] = now_iso()
        write_manifest(generation_dir, manifest)
        logger.info("Backup %s: indexed generation %s (%s, %d files)",
                    schedule.get("id"), entry["generation"], stats["size_bytes"], stats["files_total"])
        changed = True
    return changed


def _move_entries(entries: list[str], source_root: str, dest_root: str) -> list[str]:
    moved = []
    os.makedirs(dest_root, exist_ok=True)
    for name in entries:
        src = os.path.join(source_root, name)
        dst = os.path.join(dest_root, name)
        if os.path.exists(dst):
            logger.warning("Legacy migration: %s already exists, leaving %s in place", dst, src)
            continue
        try:
            shutil.move(src, dst)
            moved.append(name)
        except OSError:
            logger.error("Legacy migration: could not move %s -> %s", src, dst, exc_info=True)
    return moved


def ensure_layout(schedule: dict) -> dict:
    """Create the v2 layout and migrate pre-2.0 payload once. Idempotent.

    Returns `{ok, migrated, generation, error}`. A failure never deletes data:
    entries that could not be moved are left where they are and logged.
    """
    target = backup_target(schedule)
    root = os.path.join(target, SNAPSHOTS_DIR)
    with _MIGRATE_LOCK:
        try:
            os.makedirs(root, exist_ok=True)
            marker = read_marker(schedule)
            if marker.get("version") == LAYOUT_VERSION and os.path.isdir(root):
                return {"ok": True, "migrated": False, "generation": None, "error": ""}

            entries = []
            if os.path.isdir(target):
                entries = [name for name in os.listdir(target)
                           if name not in (SNAPSHOTS_DIR, MARKER_NAME, "pre-restore")
                           and not name.startswith(WORKSPACE_PREFIX)]
            if not entries:
                write_marker(schedule)
                return {"ok": True, "migrated": False, "generation": None, "error": ""}

            when = os.path.getmtime(target)
            generation = unique_generation_name(schedule, when)
            generation_dir = os.path.join(root, generation)
            os.makedirs(generation_dir, exist_ok=True)

            if schedule.get("type") == "docker-compose":
                payload = os.path.join(generation_dir, "projects", "legacy")
                known = [name for name in entries if name in ("config", "volumes", "images")]
                moved = _move_entries(known, target, payload)
                leftover = [name for name in entries if name not in known]
                if leftover:
                    logger.warning("Legacy docker backup %s: leaving unknown entries in place: %s",
                                   schedule.get("id"), ", ".join(sorted(leftover)[:10]))
                if not moved:
                    os.rmdir(payload) if os.path.isdir(payload) and not os.listdir(payload) else None
                    shutil.rmtree(generation_dir, ignore_errors=True)
                    write_marker(schedule)
                    return {"ok": True, "migrated": False, "generation": None, "error": ""}
            else:
                moved = _move_entries(entries, target, os.path.join(generation_dir, "data"))
                if not moved:
                    shutil.rmtree(generation_dir, ignore_errors=True)
                    write_marker(schedule)
                    return {"ok": True, "migrated": False, "generation": None, "error": ""}

            write_manifest(generation_dir, {
                "version": 2,
                "generation": generation,
                "schedule_id": schedule.get("id"),
                "type": schedule.get("type"),
                "status": "legacy",
                "legacy": True,
                "trigger": "migration",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)),
                "finished_at": now_iso(),
                **measure_generation(generation_dir, str(schedule.get("type"))),
                "note": "Migrated from the pre-2.0 layout — verify before pruning.",
            })
            write_marker(schedule)
            logger.info("Backup %s: migrated legacy layout into generation %s (%d entries)",
                        schedule.get("id"), generation, len(moved))
            return {"ok": True, "migrated": True, "generation": generation, "error": ""}
        except OSError as exc:
            logger.error("Backup %s: layout migration failed", schedule.get("id"), exc_info=True)
            return {"ok": False, "migrated": False, "generation": None, "error": str(exc)}


def list_generations(schedule: dict) -> list[dict]:
    """Return generations newest first, synthesizing entries for incomplete runs."""
    root = snapshots_root(schedule)
    try:
        names = sorted(os.listdir(root), reverse=True)
    except FileNotFoundError:
        return []
    except OSError:
        logger.warning("Could not list snapshots for %s", schedule.get("id"), exc_info=True)
        return []

    out = []
    for name in names:
        if name.startswith("."):
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        manifest = read_manifest(path)
        entry = {
            "generation": name,
            "created_at": manifest.get("created_at") or "",
            "finished_at": manifest.get("finished_at") or "",
            "status": manifest.get("status") or "incomplete",
            "type": manifest.get("type") or schedule.get("type"),
            "trigger": manifest.get("trigger") or "unknown",
            "legacy": bool(manifest.get("legacy")),
            "size_bytes": manifest.get("size_bytes"),
            "files_total": manifest.get("files_total") or 0,
            "duration_seconds": manifest.get("duration_seconds"),
            "verified": manifest.get("verify") or {},
            "remote": manifest.get("remote") or {},
            "error": manifest.get("error") or "",
        }
        out.append(entry)
    return out


def latest_generation(schedule: dict, complete_only: bool = True) -> Optional[dict]:
    for entry in list_generations(schedule):
        if complete_only and entry.get("status") not in ("complete", "legacy"):
            continue
        return entry
    return None


def backup_usage(schedule: dict) -> int:
    """Sum of completed generation sizes (from manifests, no tree walk)."""
    total = 0
    for entry in list_generations(schedule):
        size = entry.get("size_bytes")
        if isinstance(size, int) and size > 0:
            total += size
    return total


def _hardlink_probe(directory: str) -> bool:
    if not os.path.isdir(directory):
        return False
    probe = os.path.join(directory, f".ethos-link-probe-{os.getpid()}")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("probe")
        linked = os.path.join(directory, f".ethos-link-probe-{os.getpid()}.2")
        os.link(probe, linked)
        os.unlink(linked)
        return True
    except OSError:
        return False
    finally:
        try:
            os.unlink(probe)
        except OSError:  # intentional: probe may already be gone
            pass


def preflight(schedule: dict) -> dict:
    """Check destination availability/writability/free space before a run.

    Returns `{ok, skip, error, free_bytes, total_bytes, hardlinks, same_device}`.
    `skip` means the destination is unavailable and the schedule wants a clean
    skip (USB unplugged) instead of a failure.
    """
    target = backup_target(schedule)
    destination = schedule.get("destination") or ""
    result = {
        "ok": False, "skip": False, "error": "", "target": target,
        "free_bytes": 0, "total_bytes": 0, "hardlinks": False, "same_device": True,
    }
    if not os.path.isdir(destination):
        message = f"Destination is not available: {destination}"
        if schedule.get("skip_if_missing", True):
            result.update({"skip": True, "error": message})
        else:
            result["error"] = message
        return result

    try:
        stats = os.statvfs(destination)
        result["free_bytes"] = stats.f_bavail * stats.f_frsize
        result["total_bytes"] = stats.f_blocks * stats.f_frsize
    except OSError:
        logger.warning("Could not stat destination %s", destination, exc_info=True)

    min_free = int((schedule.get("options") or {}).get("min_free_gb") or 0) * 1024 ** 3
    if min_free and result["free_bytes"] and result["free_bytes"] < min_free:
        result["skip"] = True
        result["error"] = (f"Destination has only {result['free_bytes'] // 1024**3} GB free "
                           f"(minimum {min_free // 1024**3} GB)")

    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        result["skip"] = schedule.get("skip_if_missing", True)
        result["error"] = f"Cannot create backup folder: {exc}"
        return result

    if not os.access(target, os.W_OK):
        result["skip"] = schedule.get("skip_if_missing", True)
        result["error"] = f"Backup folder is not writable: {target}"
        return result

    result["hardlinks"] = _hardlink_probe(target)
    previous = latest_generation(schedule)
    if previous:
        try:
            result["same_device"] = os.stat(target).st_dev == os.stat(
                generation_path(schedule, previous["generation"])).st_dev
        except OSError:
            logger.debug("Could not compare devices for %s", schedule.get("id"), exc_info=True)
    result["ok"] = True
    return result
