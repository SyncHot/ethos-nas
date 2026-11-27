"""Cameras — recording storage, quota and retention.

Layout under the configured storage root (default `<DATA_DIR>/surveillance`):

    recordings/<camera>/<YYYY-MM-DD>/<HH-MM-SS>/index.m3u8 + *.m4s
    live/<camera>/index.m3u8 + *.m4s        (rolling window, never purged)

One `HH-MM-SS` directory per ffmpeg run, so a camera restart can never
overwrite an older playlist (ffmpeg's `append_list` restarts segment numbering
at zero). Retention works on whole run directories, which keeps every playlist
self-consistent.
"""

import logging
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.config import settings
from backend.services import camera_settings, path_scope

logger = logging.getLogger("ethos-nas.services.camera_storage")

CAMERA_ID_RE = re.compile(r"^cam_[a-f0-9]{12}$")
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RUN_DIR_RE = re.compile(r"^\d{2}-\d{2}-\d{2}$")

# A run directory is only purged when its newest file is at least this old —
# ffmpeg may be writing into the current one.
ACTIVE_GRACE_S = 300
MIN_FREE_PCT = 5


def _checked(root: Path, *parts: str) -> Path:
    path = root.joinpath(*parts)
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Path escapes the recordings root") from exc
    return path


def storage_root() -> Path:
    configured = camera_settings.storage_dir()
    root = configured if configured else settings.DATA_DIR / "surveillance"
    root.mkdir(parents=True, exist_ok=True)
    return root


def live_root() -> Path:
    path = storage_root() / "live"
    path.mkdir(parents=True, exist_ok=True)
    return path


def recordings_root() -> Path:
    path = storage_root() / "recordings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def camera_live_dir(cam_id: str) -> Path:
    if not CAMERA_ID_RE.match(cam_id or ""):
        raise ValueError("Invalid camera id")
    path = live_root() / cam_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def camera_recordings_dir(cam_id: str) -> Path:
    if not CAMERA_ID_RE.match(cam_id or ""):
        raise ValueError("Invalid camera id")
    path = recordings_root() / cam_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def camera_day_dir(cam_id: str, day: str) -> Path:
    if not DATE_DIR_RE.match(day or ""):
        raise ValueError("Invalid date")
    path = camera_recordings_dir(cam_id) / day
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_dir(cam_id: str, day: str, run: str) -> Path:
    if not RUN_DIR_RE.match(run or ""):
        raise ValueError("Invalid run directory")
    return _checked(camera_day_dir(cam_id, day), run)


# ── Configuration guard ────────────────────────────────────────────────────

def validate_storage_dir(raw: str | Path) -> str:
    """Resolve an admin-chosen recordings folder and prove it is writable.

    Uses the admin path scope (the caller is admin-only) and a real
    create/delete probe, so a share the service user cannot write to fails here
    with a message instead of an ffmpeg error later.
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("Storage folder must not be empty")
    resolved = ""
    try:
        with path_scope.system_scope():
            resolved = path_scope.sanitize(text)
    except Exception as exc:  # noqa: BLE001 — surface the scope denial verbatim
        raise ValueError(str(exc) or "Storage folder is not allowed") from exc
    if not resolved:
        # The scope is expected to raise, but a context manager that suppresses
        # instead would leave this unbound and Path("") is the CWD — refuse it.
        raise ValueError("Storage folder is not allowed")
    path = Path(resolved)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".ethos-write-test-{int(time.time())}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ValueError(
            f"Cannot write to {path}: {exc}. The service runs as the ethos-nas user — "
            "pick a folder it owns (or chown it) such as /media/<disk>/surveillance."
        ) from exc
    return str(path)


# ── Usage ──────────────────────────────────────────────────────────────────

def dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue  # a file removed mid-scan has no size to add
    return total


def _budget(root: Path, quota_pct: int) -> tuple[int, int, dict]:
    """(recordings bytes, allowed bytes, disk info) for the retention janitor.

    `quota_pct` is a cap on the *volume*, not a promise of free space: the tree
    may only grow into the space that keeps `100 - quota_pct` of the disk free,
    so unrelated data on a 75 %-full system disk is never overwritten.
    """
    recordings_size = dir_size(recordings_root())
    try:
        disk = shutil.disk_usage(root)
    except OSError:
        return recordings_size, recordings_size, {"total_bytes": 0, "free_bytes": 0,
                                                  "used_pct": 0}
    target_free = int(disk.total * max(0, 100 - quota_pct) / 100)
    allowed = recordings_size + max(0, disk.free - target_free)
    used_pct = round((disk.total - disk.free) / disk.total * 100, 1) if disk.total else 0
    return recordings_size, allowed, {"total_bytes": disk.total, "free_bytes": disk.free,
                                      "used_pct": used_pct}


def usage() -> dict:
    root = storage_root()
    by_camera: list[dict] = []
    total = 0
    rec_root = recordings_root()
    try:
        camera_dirs = sorted(p for p in rec_root.iterdir() if p.is_dir())
    except OSError:
        camera_dirs = []
    for cam_dir in camera_dirs:
        size = dir_size(cam_dir)
        total += size
        by_camera.append({"camera_id": cam_dir.name, "size_bytes": size})
    quota = camera_settings.load()["quota_pct"]
    recordings_size, allowed, disk_info = _budget(root, quota)
    live_size = dir_size(live_root())
    return {
        "storage_dir": str(root),
        "recordings_bytes": total,
        "live_bytes": live_size,
        "quota_bytes": max(0, allowed),
        **disk_info,
        "cameras": sorted(by_camera, key=lambda c: c["size_bytes"], reverse=True),
    }


# ── Retention ──────────────────────────────────────────────────────────────

def _run_dirs(cam_dir: Path) -> list[tuple[float, Path]]:
    """Every run directory with its newest mtime, oldest first."""
    out: list[tuple[float, Path]] = []
    try:
        day_dirs = [p for p in cam_dir.iterdir() if p.is_dir() and DATE_DIR_RE.match(p.name)]
    except OSError:
        return out
    for day in day_dirs:
        try:
            runs = [p for p in day.iterdir() if p.is_dir() and RUN_DIR_RE.match(p.name)]
        except OSError:
            continue
        for run in runs:
            try:
                newest = max((f.stat().st_mtime for f in run.iterdir()), default=0.0)
            except OSError:
                continue
            out.append((newest, run))
    out.sort(key=lambda item: item[0])
    return out


def _newest_run(cam_dir: Path) -> Path | None:
    items = _run_dirs(cam_dir)
    return items[-1][1] if items else None


def purge(retention_days: int | None = None, quota_pct: int | None = None) -> dict:
    """Delete expired runs, then oldest runs until the quota is met.

    Never touches a write in progress (ACTIVE_GRACE_S) and always keeps the
    newest run per camera, so the recording you are most likely to want does
    not disappear while you look at it.
    """
    cfg = camera_settings.load()
    days = cfg["default_retention_days"] if retention_days is None else int(retention_days)
    quota = cfg["quota_pct"] if quota_pct is None else int(quota_pct)
    removed_bytes = 0
    removed_dirs = 0
    root = storage_root()

    # A camera may override the global retention (`record.retention_days`).
    try:
        from backend.services import camera_service

        overrides = {c["id"]: (c.get("record") or {}).get("retention_days")
                     for c in camera_service._load_cameras()["cameras"]}
    except Exception:  # noqa: BLE001 — a bad store must not stop retention
        logger.error("Could not read per-camera retention overrides", exc_info=True)
        overrides = {}

    try:
        camera_dirs = [p for p in recordings_root().iterdir() if p.is_dir()]
    except OSError:
        camera_dirs = []

    for cam_dir in camera_dirs:
        override = overrides.get(cam_dir.name)
        try:
            cam_days = float(override) if override is not None else float(days)
        except (TypeError, ValueError):
            cam_days = float(days)
        cam_cutoff = time.time() - max(0.0, cam_days) * 86400
        newest = _newest_run(cam_dir)
        for newest_mtime, path in _run_dirs(cam_dir):
            if path == newest or (time.time() - newest_mtime) < ACTIVE_GRACE_S:
                continue
            if cam_days > 0 and newest_mtime >= cam_cutoff:
                break  # this and every later run is still inside retention
            try:
                removed_bytes += dir_size(path)
                shutil.rmtree(path)
                removed_dirs += 1
            except OSError as exc:
                logger.warning("Could not purge %s: %s", path, exc)
        # Drop empty day directories.
        try:
            for day in cam_dir.iterdir():
                if day.is_dir() and not any(day.iterdir()):
                    day.rmdir()
        except OSError:
            pass  # concurrent writes can make the directory non-empty again

    # Quota: keep the recordings tree inside the free-space budget computed
    # from the volume (see `_budget`), not a fixed share of its total size.
    try:
        recordings_size, allowed, _disk = _budget(root, quota)
        while recordings_size > allowed:
            candidates: list[tuple[float, Path, Path]] = []
            for cam_dir in camera_dirs:
                newest = _newest_run(cam_dir)
                for newest_mtime, path in _run_dirs(cam_dir):
                    if path == newest or (time.time() - newest_mtime) < ACTIVE_GRACE_S:
                        continue
                    candidates.append((newest_mtime, cam_dir, path))
            if not candidates:
                logger.warning("Recordings over quota but nothing safe to purge")
                break
            candidates.sort(key=lambda item: item[0])
            _, _, oldest = candidates[0]
            try:
                freed = dir_size(oldest)
                shutil.rmtree(oldest)
                recordings_size -= freed
                removed_bytes += freed
                removed_dirs += 1
                logger.info("Quota purge removed %s", oldest)
            except OSError as exc:
                logger.warning("Could not purge %s: %s", oldest, exc)
                break
    except OSError:
        pass  # disk probe failure must not break the janitor

    result = {"removed_dirs": removed_dirs, "removed_bytes": removed_bytes,
              "retention_days": days, "quota_pct": quota}
    if removed_dirs:
        logger.info("Camera retention removed %d run(s), %.1f MiB",
                    removed_dirs, removed_bytes / 1024 ** 2)
    return result


def delete_camera_data(cam_id: str) -> None:
    """Remove a deleted camera's live window and recordings."""
    for path in (live_root() / cam_id, recordings_root() / cam_id):
        try:
            if path.exists():
                shutil.rmtree(path)
        except OSError as exc:
            logger.warning("Could not remove camera data %s: %s", path, exc)


def free_days_estimate(daily_bytes: int) -> int | None:
    """Rough retention capacity, for the Storage tab."""
    if daily_bytes <= 0:
        return None
    try:
        disk = shutil.disk_usage(storage_root())
    except OSError:
        return None
    cfg = camera_settings.load()
    usable = max(0, int(disk.total * cfg["quota_pct"] / 100) - dir_size(recordings_root()))
    return int(usable // daily_bytes)


def day_start(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def days_back(count: int) -> list[str]:
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(count)]
