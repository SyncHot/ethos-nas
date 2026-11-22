"""Retention for caches, trash and abandoned uploads (TODO.md P0-11).

Everything EthOS writes under `settings.DATA_DIR` that can grow without bound:

  temp_chunks/   — abandoned chunked uploads (TTL)
  transcodes/    — temporary playback transcodes (TTL)
  trash/         — per-user recycle bins (age policy applied per trashed item)
  thumbnails/    — image thumbnail cache (LRU file cap)
  video_thumbs/  — video frame cache (LRU file cap)

The same loop keeps the state archives fresh: once a day it asks
`services/config_archive` for a snapshot of every JSON store + the gallery index
(kept in `<DATA_DIR>/snapshots/`, newest `CONFIG_SNAPSHOT_KEEP`).

The sweep runs in a lifespan-managed background loop (default every 6 h) and is
safe to call manually. It only ever touches the directories listed above, never
follows symlinks, and logs a single INFO line per run plus WARNINGs per failure.
"""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from backend.config import settings

logger = logging.getLogger("ethos-nas.services.retention_service")

_sweeper_task: asyncio.Task | None = None


def _entry_mtime(entry: Path) -> float:
    try:
        return entry.stat().st_mtime
    except OSError:
        return 0.0


def _entry_size(entry: Path) -> int:
    if entry.is_symlink():
        return 0
    if entry.is_file():
        try:
            return entry.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in _walk(entry):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _walk(path: Path):
    try:
        yield from os.walk(path)
    except OSError:
        return


def _areas() -> list[tuple[str, Path, float | None, int | None]]:
    """(name, path, max_age_seconds, max_files) — None means "no limit"."""
    return [
        ("temp_chunks", settings.DATA_DIR / "temp_chunks",
         settings.TEMP_CHUNK_TTL_HOURS * 3600, None),
        ("transcodes", settings.DATA_DIR / "transcodes",
         settings.TEMP_CHUNK_TTL_HOURS * 3600, None),
        ("trash", settings.DATA_DIR / "trash",
         settings.TRASH_RETENTION_DAYS * 86400, None),
        ("thumbnails", settings.DATA_DIR / "thumbnails",
         None, settings.THUMB_CACHE_MAX_FILES),
        ("video_thumbs", settings.DATA_DIR / "video_thumbs",
         None, settings.VIDEO_THUMB_CACHE_MAX_FILES),
        # Offline downloads prepared by Video Station. `video_render.RenderManager`
        # owns both the files and the job rows (TTL / byte cap / job cap), so here
        # the area is only measured — never swept — to avoid orphaning job rows.
        ("video_exports", settings.DATA_DIR / "video_exports" / "files",
         None, None),
    ]


def _remove(entry: Path) -> tuple[bool, int]:
    """Delete one entry; returns (removed, bytes) and never raises."""
    size = _entry_size(entry)
    try:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    except OSError:
        logger.warning("Retention: could not remove %s", entry, exc_info=True)
        return False, 0
    return True, size


def sweep() -> dict:
    """Apply the retention policy once. Returns a summary (never raises)."""
    now = time.time()
    summary: dict = {"removed_files": 0, "freed_bytes": 0, "by_area": {}}

    for name, path, max_age, max_files in _areas():
        removed = freed = 0
        if name == "trash":
            # Per-user namespaces: the age policy applies to each trashed item,
            # never to the top-level user directory (that would erase a whole
            # recycle bin just because the user was idle).
            try:
                from backend.services import trash_service
                result = (trash_service.sweep_retention(max_age)
                          if max_age is not None
                          else {"removed_files": 0, "freed_bytes": 0})
                removed = int(result.get("removed_files", 0))
                freed = int(result.get("freed_bytes", 0))
            except Exception:
                logger.warning("Retention: trash sweep failed", exc_info=True)
            if removed:
                summary["by_area"][name] = {"files": removed, "bytes": freed}
                summary["removed_files"] += removed
                summary["freed_bytes"] += freed
            continue
        try:
            if not path.is_dir():
                continue
            entries = [e for e in path.iterdir() if not e.is_symlink()]

            if max_age is not None:
                cutoff = now - max_age
                for entry in entries:
                    if _entry_mtime(entry) < cutoff:
                        ok, size = _remove(entry)
                        if ok:
                            removed += 1
                            freed += size
                entries = [e for e in entries if e.exists()]

            if max_files is not None and len(entries) > max_files:
                entries.sort(key=_entry_mtime, reverse=True)
                for entry in entries[max_files:]:
                    ok, size = _remove(entry)
                    if ok:
                        removed += 1
                        freed += size
        except OSError:
            logger.warning("Retention: scanning %s failed", path, exc_info=True)

        if removed:
            summary["by_area"][name] = {"files": removed, "bytes": freed}
            summary["removed_files"] += removed
            summary["freed_bytes"] += freed

    if summary["removed_files"]:
        logger.info("Retention: removed %d item(s), freed %.1f MB",
                    summary["removed_files"], summary["freed_bytes"] / 1_048_576)
    else:
        logger.debug("Retention: nothing to remove")
    return summary


def usage() -> dict:
    """Per-area file count and byte size, for the Settings/storage view."""
    out: dict = {"areas": {}, "total_bytes": 0, "total_files": 0}
    for name, path, _age, _max in _areas():
        files = size = 0
        if name == "trash":
            try:
                from backend.services import trash_service
                area = trash_service.usage()
            except Exception:
                logger.warning("Retention: trash usage failed", exc_info=True)
                area = {"path": str(path), "files": 0, "bytes": 0}
            out["areas"][name] = area
            out["total_files"] += int(area.get("files", 0))
            out["total_bytes"] += int(area.get("bytes", 0))
            continue
        if path.is_dir():
            for root, _dirs, names in _walk(path):
                for fname in names:
                    files += 1
                    try:
                        size += (Path(root) / fname).stat().st_size
                    except OSError:
                        continue
        out["areas"][name] = {"path": str(path), "files": files, "bytes": size}
        out["total_files"] += files
        out["total_bytes"] += size
    return out


async def _loop() -> None:
    # Let startup finish before touching the disk.
    try:
        await asyncio.sleep(60)
        while True:
            try:
                await asyncio.to_thread(sweep)
            except Exception:
                logger.error("Retention sweep failed", exc_info=True)
            try:
                from backend.services import video_render

                await asyncio.to_thread(video_render.MANAGER.sweep)
            except Exception:
                logger.error("Video export sweep failed", exc_info=True)
            try:
                from backend.services import config_archive

                await asyncio.to_thread(config_archive.maybe_snapshot)
            except Exception:
                logger.error("Daily config snapshot failed", exc_info=True)
            await asyncio.sleep(max(300, settings.RETENTION_INTERVAL))
    except asyncio.CancelledError:
        raise


def start_retention_sweeper() -> None:
    global _sweeper_task
    if _sweeper_task and not _sweeper_task.done():
        return
    _sweeper_task = asyncio.create_task(_loop(), name="retention-sweeper")
    logger.info("Retention sweeper started (every %ds)", settings.RETENTION_INTERVAL)


async def stop_retention_sweeper() -> None:
    global _sweeper_task
    task, _sweeper_task = _sweeper_task, None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass  # intentional: a failure here must not break the caller
