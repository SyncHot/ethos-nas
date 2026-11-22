"""EthOS NAS — Disk Cleaner service.

Everything behind ``/api/disk-clean`` lives here; the router only validates the
request and delegates. The service owns:

  * bounded directory walks (folder tree and biggest files),
  * background scan jobs with live progress and cooperative cancel,
  * cached cleanup suggestions (trash, temp, caches, logs, journal),
  * safe deletion — user content goes to the per-user EthOS trash (recoverable),
    junk categories are deleted permanently — reusing the File Manager workers
    so byte progress, batching and the deletion audit already exist,
  * a capped history of reclaim actions for the Overview/Activity views.

Rules: admin-only (the router mounts ``require_admin``), every path is passed
through ``path_scope.sanitize()``, and paths owned by EthOS itself (state dir,
checkout, Docker root) can never be deleted through this API. Walks are capped
by entry count and wall-clock budget so a huge tree degrades into a partial
result instead of a hung request.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import HTTPException

from backend.config import settings
from backend.services import (
    files_service, json_store, path_scope, retention_service, task_registry, trash_service,
)

logger = logging.getLogger("ethos-nas.services.disk_clean_service")

# ── Limits ───────────────────────────────────────────────────────────────────

MAX_WALK_ENTRIES = 400_000      # hard cap on directory entries per scan
SCAN_TIMEOUT_S = 120            # backlog budget for a legacy synchronous scan
SUGGESTION_TTL_S = 120          # reuse a suggestion sweep for two minutes
SUGGESTION_WALK_BUDGET_S = 8.0  # per root within one suggestion sweep
SUGGESTION_TOTAL_BUDGET_S = float(
    os.getenv("ETHOS_DISK_CLEAN_SUGGESTION_BUDGET", "20"))
MIN_SUGGESTION_BYTES = 1 * 1024 * 1024
MAX_ITEMS_PER_CATEGORY = 50
MAX_DELETE_PATHS = 100
MAX_SCAN_JOBS = 2
SCAN_JOB_TTL_S = 600
HISTORY_LIMIT = 50

# Managed caches the cleaner may reclaim on request. `video_exports` is owned by
# the render manager (deleting it would orphan job rows) and is never included.
CLEANABLE_CACHES = ("temp_chunks", "transcodes", "trash", "thumbnails", "video_thumbs")

# Roots that must never be deleted through the UI (exact match — their children
# are normal user content, e.g. /home/alice is deletable, /home is not).
_EXACT_PROTECTED = {
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64",
    "/proc", "/root/.ssh", "/run", "/sbin", "/sys", "/usr",
}

# EthOS-owned trees: neither the root nor anything below it may be deleted with
# the generic filesystem action (caches are reclaimed through the retention API).
_SENSITIVE_EXTRA = ("/var/lib/docker", "/etc/ethos-nas")

# Pseudo-filesystems pruned from every walk.
_PRUNE_PATHS = {"/proc", "/sys", "/dev"}

_ROTATED_LOG_RE = re.compile(r"\.(gz|bz2|xz|zst|old|log\.\d+|\d+)$")

_SUGGESTION_CATEGORIES = ("trash", "temp", "cache", "logs", "var_cache")


class ScanCancelled(Exception):
    """Raised inside a scan worker when the user pressed Cancel."""


# ── Path policy ──────────────────────────────────────────────────────────────

def _sensitive_roots() -> list[str]:
    """EthOS-owned trees that must survive the generic delete action."""
    checkout = str(Path(__file__).resolve().parents[2])
    roots = [str(settings.DATA_DIR), checkout, *_SENSITIVE_EXTRA]
    return [os.path.abspath(root) for root in roots]


def protected_reason(abs_path: str) -> str | None:
    """Why ``abs_path`` may not be deleted, or None when it is user content.

    Refuses the exact roots in ``_EXACT_PROTECTED``, everything inside an
    EthOS-owned tree and any ancestor of a protected path (deleting ``/var``
    would take ``/var/lib/docker`` with it).
    """
    path = os.path.abspath(abs_path).rstrip(os.sep) or "/"
    real = os.path.realpath(path)
    if path in _EXACT_PROTECTED:
        return f"{path} is a protected system root"
    for root in _sensitive_roots():
        real_root = os.path.realpath(root)
        for candidate, owner in ((path, root), (real, real_root)):
            if candidate == owner or candidate.startswith(owner + os.sep):
                return f"{path} is managed by EthOS"
        if root.startswith(path + os.sep) or real_root.startswith(path + os.sep):
            return f"{path} contains EthOS-managed data"
    for root in _EXACT_PROTECTED:
        if root.rstrip(os.sep).startswith(path + os.sep):
            return f"{path} contains a protected system path"
    return None


def _prune(abs_path: str) -> bool:
    return any(abs_path == p or abs_path.startswith(p + "/") for p in _PRUNE_PATHS)


# ── Walk helpers (synchronous — always called via asyncio.to_thread) ─────────

def _walk_dir_sizes(
    root: str,
    *,
    progress: dict | None = None,
    cancel: threading.Event | None = None,
    budget_s: float = SCAN_TIMEOUT_S,
):
    """Single-pass walk returning ``({path: total_size}, entries, truncated)``.

    The aggregated dict contains the root and every directory below it with the
    size of the whole subtree. ``progress`` is mutated in place (entries/bytes/
    current) so a job poller can show live numbers; ``cancel`` aborts with
    ``ScanCancelled`` and ``budget_s`` turns a slow tree into a partial result.
    """
    direct: dict[str, int] = {}
    count = 0
    truncated = False
    started = time.monotonic()
    stack = [root]
    while stack:
        directory = stack.pop()
        if progress is not None:
            progress["current"] = directory
        try:
            iterator = os.scandir(directory)
        except OSError:
            continue
        with iterator:
            for entry in iterator:
                count += 1
                if count > MAX_WALK_ENTRIES:
                    truncated = True
                    break
                if progress is not None and (count & 0x7FF) == 0:
                    progress["entries"] = count
                    if cancel is not None and cancel.is_set():
                        raise ScanCancelled
                    if time.monotonic() - started > budget_s:
                        truncated = True
                        break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        child = os.path.abspath(entry.path)
                        if _prune(child):
                            continue
                        direct.setdefault(child, 0)
                        stack.append(child)
                    elif entry.is_file(follow_symlinks=False):
                        size = entry.stat(follow_symlinks=False).st_size
                        direct[directory] = direct.get(directory, 0) + size
                        if progress is not None:
                            progress["bytes"] += size
                except OSError:
                    continue
        if truncated:
            break

    # Aggregate bottom-up: children precede parents when sorted by path length.
    for path in sorted(direct.keys(), key=len, reverse=True):
        parent = os.path.dirname(path)
        if parent and parent != path:
            direct[parent] = direct.get(parent, 0) + direct[path]
    if progress is not None:
        progress["entries"] = count
    return direct, count, truncated


def _entry_sort_size(entry, sizes: dict) -> int:
    try:
        if entry.is_dir(follow_symlinks=False):
            return sizes.get(os.path.abspath(entry.path), 0)
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return 0


def _build_tree(root: str, sizes: dict, depth: int, max_children: int = 100):
    """Nested tree from aggregated sizes; ``depth`` levels below the root."""

    def node(path: str, levels_left: int) -> dict:
        base = {
            "name": os.path.basename(root.rstrip("/")) if path == root else (os.path.basename(path) or "/"),
            "path": path,
            "size": sizes.get(path, 0),
            "is_dir": True,
        }
        if levels_left <= 0:
            base["children"] = None
            return base
        children = []
        try:
            entries = sorted(os.scandir(path), key=lambda e: _entry_sort_size(e, sizes), reverse=True)
        except OSError:
            entries = []
        for entry in entries[:max_children]:
            child = os.path.abspath(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    if _prune(child):
                        continue
                    children.append(node(child, levels_left - 1))
                elif entry.is_file(follow_symlinks=False):
                    children.append({
                        "name": entry.name, "path": child,
                        "size": entry.stat(follow_symlinks=False).st_size,
                        "is_dir": False, "children": None,
                    })
            except OSError:
                continue
        base["count"] = len(children)
        base["children"] = children
        return base

    return node(root, depth)


def _top_files(
    root: str,
    limit: int,
    *,
    progress: dict | None = None,
    cancel: threading.Event | None = None,
    budget_s: float = SCAN_TIMEOUT_S,
):
    """The ``limit`` largest files under ``root`` (bounded heap)."""
    heap: list[tuple[int, str]] = []
    count = 0
    truncated = False
    started = time.monotonic()
    stack = [root]
    while stack:
        directory = stack.pop()
        if progress is not None:
            progress["current"] = directory
        try:
            iterator = os.scandir(directory)
        except OSError:
            continue
        with iterator:
            for entry in iterator:
                count += 1
                if count > MAX_WALK_ENTRIES:
                    truncated = True
                    break
                if progress is not None and (count & 0x7FF) == 0:
                    progress["entries"] = count
                    if cancel is not None and cancel.is_set():
                        raise ScanCancelled
                    if time.monotonic() - started > budget_s:
                        truncated = True
                        break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        child = os.path.abspath(entry.path)
                        if _prune(child):
                            continue
                        stack.append(child)
                    elif entry.is_file(follow_symlinks=False):
                        size = entry.stat(follow_symlinks=False).st_size
                        if len(heap) < limit:
                            heapq.heappush(heap, (size, entry.path))
                        elif size > heap[0][0]:
                            heapq.heapreplace(heap, (size, entry.path))
                except OSError:
                    continue
        if truncated:
            break
    files = [{"path": p, "name": os.path.basename(p), "size": s} for s, p in heap]
    files.sort(key=lambda item: item["size"], reverse=True)
    if progress is not None:
        progress["entries"] = count
    return files, count, truncated


def _tree_size_bounded(path: str, *, max_entries: int = MAX_WALK_ENTRIES,
                       deadline: float | None = None) -> int:
    """Size of one file or of a whole directory tree (bounded walk)."""
    try:
        if os.path.islink(path):
            return 0
        if os.path.isfile(path):
            return os.stat(path).st_size
        if not os.path.isdir(path):
            return 0
        total = 0
        seen = 0
        for root, dirs, names in os.walk(path):
            dirs[:] = [d for d in dirs if not _prune(os.path.join(root, d))]
            for name in names:
                seen += 1
                if seen > max_entries:
                    return total
                try:
                    total += os.stat(os.path.join(root, name), follow_symlinks=False).st_size
                except OSError:
                    continue
            if deadline is not None and time.monotonic() > deadline:
                return total
        return total
    except OSError:
        return 0


def _child_sizes(root: str, *, deadline: float) -> tuple[list[tuple[str, int]], bool]:
    """Sizes of the direct children of ``root`` in one bounded pass."""
    out: list[tuple[str, int]] = []
    truncated = False
    try:
        children = list(os.scandir(root))
    except OSError:
        return out, False
    for entry in children:
        if time.monotonic() > deadline:
            truncated = True
            break
        try:
            if entry.is_symlink():
                continue
            path = os.path.abspath(entry.path)
            if _prune(path):
                continue
            if entry.is_dir(follow_symlinks=False):
                size = _tree_size_bounded(path, deadline=deadline)
            elif entry.is_file(follow_symlinks=False):
                size = entry.stat(follow_symlinks=False).st_size
            else:
                continue
        except OSError:
            continue
        out.append((path, size))
    out.sort(key=lambda item: item[1], reverse=True)
    return out, truncated


# ── Scan jobs ────────────────────────────────────────────────────────────────

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _prune_jobs() -> None:
    """Drop finished jobs past their TTL; caller holds ``_JOBS_LOCK``."""
    now = time.time()
    finished = [
        (job["finished"] or job["started"], job_id)
        for job_id, job in _JOBS.items() if job["state"] != "running"
    ]
    for finished_at, job_id in sorted(finished):
        if now - finished_at > SCAN_JOB_TTL_S:
            _JOBS.pop(job_id, None)
    if len(_JOBS) <= MAX_SCAN_JOBS * 10:
        return
    for _finished_at, job_id in sorted(finished)[:len(_JOBS) - MAX_SCAN_JOBS * 10]:
        _JOBS.pop(job_id, None)


def _scan_worker(job: dict) -> None:
    """Blocking scan body — runs in a worker thread."""
    progress = job["progress"]
    if job["mode"] == "files":
        files, count, truncated = _top_files(
            job["path"], job["limit"], progress=progress, cancel=job["cancel"])
        job["result"] = {
            "path": job["path"], "files": files,
            "scanned_entries": count, "truncated": truncated,
        }
    else:
        sizes, count, truncated = _walk_dir_sizes(
            job["path"], progress=progress, cancel=job["cancel"])
        job["result"] = {
            "path": job["path"],
            "total_size": sizes.get(job["path"], 0),
            "file_count": count,
            "truncated": truncated,
            "tree": _build_tree(job["path"], sizes, job["depth"]),
        }
    job["state"] = "done"


async def _run_scan(job: dict) -> None:
    try:
        await asyncio.to_thread(_scan_worker, job)
    except ScanCancelled:
        job["state"] = "cancelled"
        logger.info("Scan cancelled by the user (path=%s)", job["path"])
    except asyncio.CancelledError:
        job["state"] = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 — surface any walk failure to the UI
        logger.error("Scan failed (path=%s)", job["path"], exc_info=True)
        job["state"] = "failed"
        job["error"] = str(exc)
    finally:
        job["finished"] = time.time()
        job["progress"]["current"] = None


def start_scan(path: str, depth: int = 2, mode: str = "tree", limit: int = 25) -> dict:
    """Validate the request and start a background scan job."""
    root = path_scope.sanitize(path)
    if not os.path.isdir(root):
        raise HTTPException(status_code=404, detail=f"Not a directory: {root}")
    if mode not in ("tree", "files"):
        raise HTTPException(status_code=400, detail="mode must be 'tree' or 'files'")
    with _JOBS_LOCK:
        _prune_jobs()
        running = sum(1 for job in _JOBS.values() if job["state"] == "running")
        if running >= MAX_SCAN_JOBS:
            raise HTTPException(
                status_code=429,
                detail="Two scans are already running — wait for one to finish or cancel it",
            )
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id, "path": root, "depth": depth, "mode": mode, "limit": limit,
            "state": "running", "started": time.time(), "finished": None,
            "progress": {"entries": 0, "bytes": 0, "current": root},
            "cancel": threading.Event(), "result": None, "error": None,
        }
        _JOBS[job_id] = job
    task_registry.spawn(_run_scan(job), name=f"disk-scan-{job_id}")
    logger.info("Scan started (job=%s path=%s depth=%s mode=%s)", job_id, root, depth, mode)
    return {"job_id": job_id, "state": "running", "path": root, "depth": depth, "mode": mode}


def get_scan(job_id: str) -> dict:
    with _JOBS_LOCK:
        _prune_jobs()
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown scan job")
        snapshot = {
            "job_id": job["id"],
            "state": job["state"],
            "path": job["path"],
            "depth": job["depth"],
            "mode": job["mode"],
            "started": job["started"],
            "finished": job["finished"],
            "progress": dict(job["progress"]),
            "error": job["error"],
            "result": job["result"],
        }
    return snapshot


def cancel_scan(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown scan job")
        job["cancel"].set()
        state = job["state"]
    logger.info("Scan cancel requested (job=%s)", job_id)
    return {"job_id": job_id, "state": state if state != "running" else "cancelling"}


# ── Legacy synchronous endpoints ─────────────────────────────────────────────

async def scan_sync(path: str, depth: int = 2) -> dict:
    """Folder-size tree for one request (kept for older app versions)."""
    root = path_scope.sanitize(path)
    if not os.path.isdir(root):
        raise HTTPException(status_code=404, detail=f"Not a directory: {root}")

    def work():
        sizes, count, truncated = _walk_dir_sizes(root)
        return {
            "path": root, "total_size": sizes.get(root, 0), "file_count": count,
            "truncated": truncated, "tree": _build_tree(root, sizes, depth),
        }

    try:
        return await asyncio.wait_for(asyncio.to_thread(work), timeout=SCAN_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Scan timed out — try a smaller path or lower depth",
        ) from None


async def top_files_sync(path: str, limit: int = 25) -> dict:
    """Largest files for one request (kept for older app versions)."""
    root = path_scope.sanitize(path)
    if not os.path.isdir(root):
        raise HTTPException(status_code=404, detail=f"Not a directory: {root}")

    def work():
        files, count, truncated = _top_files(root, limit)
        return {"path": root, "files": files, "scanned_entries": count, "truncated": truncated}

    try:
        return await asyncio.wait_for(asyncio.to_thread(work), timeout=SCAN_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504, detail="Scan timed out — try a smaller path") from None


# ── Cleanup suggestions ──────────────────────────────────────────────────────

_SUG_CACHE: dict | None = None
_SUG_CACHE_AT = 0.0
_SUG_LOCK = threading.Lock()


def invalidate_suggestions() -> None:
    global _SUG_CACHE, _SUG_CACHE_AT
    with _SUG_LOCK:
        _SUG_CACHE = None
        _SUG_CACHE_AT = 0.0


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _journal_usage() -> dict:
    """{size, human, available} for the systemd journal."""
    try:
        out = subprocess.run(
            ["journalctl", "--disk-usage"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:  # noqa: BLE001 — journald may be unavailable in a container
        logger.debug("journalctl --disk-usage failed", exc_info=True)
        return {"size": 0, "human": "", "available": False}
    match = re.search(r"([0-9.]+)\s*([KMGT]i?B?)\b", out)
    if not match:
        return {"size": 0, "human": "", "available": False}
    value, unit = float(match.group(1)), match.group(2).upper().rstrip("IB")
    multiplier = {"B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}.get(unit, 1)
    return {"size": int(value * multiplier), "human": f"{match.group(1)} {unit}", "available": True}


def _user_homes() -> list[str]:
    homes = []
    try:
        for name in sorted(os.listdir("/home")):
            path = os.path.join("/home", name)
            if os.path.isdir(path):
                homes.append(path)
    except OSError:
        pass  # no /home entries — nothing to suggest
    if os.path.isdir("/root"):
        homes.append("/root")
    return homes


def _collect_suggestions() -> dict:
    """One bounded sweep of the usual junk locations."""
    deadline = time.monotonic() + SUGGESTION_TOTAL_BUDGET_S
    categories: dict[str, list[dict]] = {name: [] for name in _SUGGESTION_CATEGORIES}
    truncated = False

    def add(category: str, path: str, safety: str, description: str,
            size: int | None = None) -> None:
        if _prune(path) or not os.path.exists(path):
            return
        if size is None:
            size = _tree_size_bounded(path, deadline=deadline)
        if size < MIN_SUGGESTION_BYTES:
            return
        categories[category].append({
            "path": path,
            "name": os.path.basename(path.rstrip("/")) or "/",
            "size": int(size),
            "safety": safety,          # safe | caution
            "description": description,
        })

    for home in _user_homes():
        if time.monotonic() > deadline:
            truncated = True
            break
        add("trash", f"{home}/.local/share/Trash", "safe",
            "User trash — permanently deleted when emptied")
        add("cache", f"{home}/.cache", "safe",
            "Application cache (thumbnails, editor/browser caches)")

    for root, category in (("/tmp", "temp"), ("/var/tmp", "temp"), ("/var/cache", "var_cache")):
        if time.monotonic() > deadline:
            truncated = True
            break
        items, walk_truncated = _child_sizes(root, deadline=min(deadline, time.monotonic() + SUGGESTION_WALK_BUDGET_S))
        truncated = truncated or walk_truncated
        for path, size in items[:MAX_ITEMS_PER_CATEGORY]:
            safety = "caution" if category == "var_cache" else "safe"
            description = ("System package cache" if category == "var_cache"
                           else f"Temporary entry in {root}")
            add(category, path, safety, description, size=size)

    log_items, log_truncated = _child_sizes(
        "/var/log", deadline=min(deadline, time.monotonic() + SUGGESTION_WALK_BUDGET_S))
    truncated = truncated or log_truncated
    for path, size in log_items[:MAX_ITEMS_PER_CATEGORY]:
        name = os.path.basename(path)
        rotated = bool(_ROTATED_LOG_RE.search(name)) or (
            name.startswith(("syslog.", "kern.")) and "." in name[6:])
        add("logs", path, "safe" if rotated else "caution",
            "Rotated log file" if rotated else "Active system log", size=size)

    for items in categories.values():
        items.sort(key=lambda item: item["size"], reverse=True)
        del items[MAX_ITEMS_PER_CATEGORY:]

    summary = {"reclaimable_bytes": 0, "total_bytes": 0, "by_category": {}}
    for category, items in categories.items():
        total = sum(item["size"] for item in items)
        safe = sum(item["size"] for item in items if item["safety"] == "safe")
        summary["by_category"][category] = {
            "count": len(items), "bytes": total, "safe_bytes": safe,
        }
        summary["total_bytes"] += total
        summary["reclaimable_bytes"] += safe

    return {
        "generated_at": _now_iso(),
        "cached": False,
        "truncated": truncated,
        "categories": categories,
        "journal": _journal_usage(),
        "summary": summary,
    }


def get_suggestions(refresh: bool = False) -> dict:
    """Cleanup candidates, cached for two minutes unless ``refresh`` is set."""
    global _SUG_CACHE, _SUG_CACHE_AT
    if not refresh:
        with _SUG_LOCK:
            if _SUG_CACHE is not None and time.time() - _SUG_CACHE_AT < SUGGESTION_TTL_S:
                return {**_SUG_CACHE, "cached": True}
    payload = _collect_suggestions()
    with _SUG_LOCK:
        _SUG_CACHE = payload
        _SUG_CACHE_AT = time.time()
    logger.debug("Suggestion sweep finished (truncated=%s)", payload["truncated"])
    return payload


# ── Delete ───────────────────────────────────────────────────────────────────

def _delete_sync(paths: list[str]) -> dict:
    """Legacy synchronous permanent delete (older app versions)."""
    deleted: list[dict] = []
    errors: list[dict] = []
    freed_total = 0
    for path in paths:
        try:
            size = _tree_size_bounded(path)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            deleted.append({"path": path, "freed_bytes": size})
            freed_total += size
        except Exception as exc:  # noqa: BLE001 — report per-path errors, keep going
            logger.warning("disk-clean delete failed for %s: %s", path, exc)
            errors.append({"path": path, "error": str(exc)})
    return {"deleted": deleted, "errors": errors, "freed_bytes": freed_total}


async def _watch_file_op(op_id: str, action: str, requested: int, estimated_bytes: int) -> None:
    """Record history once a File Manager operation finishes."""
    deadline = time.monotonic() + 6 * 3600
    while time.monotonic() < deadline:
        snapshot = files_service.get_file_op(op_id)
        if snapshot is None:
            break
        if snapshot.get("status") == "done":
            record_history(
                action,
                items=int(snapshot.get("succeeded") or requested),
                failed=int(snapshot.get("failed") or 0),
                freed_bytes=estimated_bytes,
                scope="cleaner",
            )
            invalidate_suggestions()
            return
        await asyncio.sleep(0.5)
    record_history(action, items=requested, failed=0, freed_bytes=estimated_bytes, scope="cleaner")
    invalidate_suggestions()


async def delete_paths(paths: list[str], *, mode: str = "permanent",
                       run_async: bool = False, username: str = "") -> dict:
    """Delete files/directories; ``mode="trash"`` is recoverable.

    ``mode="trash"`` and ``mode="permanent"`` + ``run_async`` hand the work to
    the File Manager workers and return ``{op_id}`` (poll ``/api/files/ops``).
    The legacy call returns the per-path result synchronously.
    """
    if not paths:
        raise HTTPException(status_code=400, detail="No paths provided")
    if len(paths) > MAX_DELETE_PATHS:
        raise HTTPException(
            status_code=400, detail=f"At most {MAX_DELETE_PATHS} paths per request")
    if mode not in ("permanent", "trash"):
        raise HTTPException(status_code=400, detail="mode must be 'permanent' or 'trash'")

    existing: list[str] = []
    for raw in paths:
        abs_path = path_scope.sanitize(raw)
        reason = protected_reason(abs_path)
        if reason:
            raise HTTPException(
                status_code=403, detail=f"Refusing to delete {abs_path}: {reason}")
        if os.path.lexists(abs_path):
            existing.append(abs_path)
    if not existing:
        return {"deleted": [], "errors": [], "freed_bytes": 0, "skipped": len(paths)}

    estimated_bytes = await asyncio.to_thread(
        lambda: sum(_tree_size_bounded(path, deadline=time.monotonic() + 10) for path in existing)
    )

    if mode == "trash" and not username:
        raise HTTPException(status_code=400, detail="Trash mode needs an authenticated account")
    if mode == "trash":
        op_id = await files_service.start_trash_operation(existing, username)
        task_registry.spawn(
            _watch_file_op(op_id, "trash", len(existing), estimated_bytes),
            name=f"disk-clean-watch-{op_id}")
        logger.info("Cleaner moved %d path(s) to trash (op=%s)", len(existing), op_id)
        return {"ok": True, "mode": "trash", "op_id": op_id,
                "estimated_bytes": estimated_bytes, "paths": existing}

    if run_async:
        op_id = await files_service.start_hard_delete_operation(existing)
        task_registry.spawn(
            _watch_file_op(op_id, "delete", len(existing), estimated_bytes),
            name=f"disk-clean-watch-{op_id}")
        logger.info("Cleaner started permanent delete of %d path(s) (op=%s)", len(existing), op_id)
        return {"ok": True, "mode": "permanent", "op_id": op_id,
                "estimated_bytes": estimated_bytes, "paths": existing}

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_delete_sync, existing), timeout=SCAN_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Delete timed out — retry with fewer paths") from None
    record_history("delete", items=len(result["deleted"]), failed=len(result["errors"]),
                   freed_bytes=int(result["freed_bytes"]), scope="cleaner")
    invalidate_suggestions()
    logger.info("Cleaner deleted %d path(s), %d error(s)",
                len(result["deleted"]), len(result["errors"]))
    return result


# ── Journal vacuum ───────────────────────────────────────────────────────────

async def vacuum_journal(days: int = 7) -> dict:
    """Remove journal entries older than ``days`` and report freed bytes."""
    before = (await asyncio.to_thread(_journal_usage)).get("size", 0)

    def work():
        return subprocess.run(
            ["journalctl", f"--vacuum-time={days}d"],
            capture_output=True, text=True, timeout=120,
        )

    try:
        completed = await asyncio.to_thread(work)
    except Exception as exc:  # noqa: BLE001 — journalctl is host tooling
        logger.error("journalctl vacuum failed", exc_info=True)
        raise HTTPException(status_code=500, detail=f"journalctl failed: {exc}") from exc
    output = (completed.stdout or "") + (f"\n{completed.stderr}" if completed.returncode else "")
    after = (await asyncio.to_thread(_journal_usage)).get("size", 0)
    freed = max(0, before - after)
    invalidate_suggestions()
    record_history("vacuum", items=1, failed=0 if completed.returncode == 0 else 1,
                   freed_bytes=freed, scope=f"journal>{days}d")
    try:
        from backend.services.notifications_service import emit_event
        if completed.returncode == 0:
            emit_event("success", "Journal cleaned",
                       f"Removed journal entries older than {days} days "
                       f"({freed / 1_048_576:.1f} MB freed)", source="storage")
        else:
            emit_event("warning", "Journal vacuum failed",
                       (completed.stderr or "journalctl returned an error").strip()[:200],
                       source="storage")
    except Exception:  # noqa: BLE001 — a notification must never break the action
        logger.debug("Journal notification failed", exc_info=True)
    logger.info("Journal vacuumed (> %dd), %d byte(s) freed", days, freed)
    return {
        "output": output.strip() or f"Journal vacuumed (older than {days} days).",
        "freed_bytes": freed,
    }


# ── Managed caches ───────────────────────────────────────────────────────────

def _purge_dir(directory: Path) -> tuple[int, int, int]:
    """Delete every entry of one managed cache directory.

    Returns ``(items, bytes, errors)``. Thumbnail caches are disposable by
    design (they regenerate on the next view), so a full purge is safe.
    """
    items = freed = errors = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0, 0, 0
    for entry in entries:
        try:
            size = _tree_size_bounded(str(entry), max_entries=200_000)
            if entry.is_symlink() or not entry.is_dir():
                entry.unlink()
            else:
                shutil.rmtree(entry)
            items += 1
            freed += size
        except OSError:
            logger.warning("Cache purge could not remove %s", entry, exc_info=True)
            errors += 1
    return items, freed, errors


async def clean_caches(areas: list[str]) -> dict:
    """Reclaim EthOS-managed caches (thumbnails, transcodes, uploads, trash)."""
    if not areas:
        raise HTTPException(status_code=400, detail="No cache areas selected")
    unknown = sorted(set(areas) - set(CLEANABLE_CACHES))
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"Unknown cache area(s): {', '.join(unknown)}")

    result: dict = {"areas": {}, "total_bytes": 0, "items": 0, "failed": 0}
    for area in dict.fromkeys(areas):
        items = freed = failed = 0
        if area in ("thumbnails", "video_thumbs"):
            items, freed, failed = await asyncio.to_thread(
                _purge_dir, settings.DATA_DIR / area)
        elif area == "trash":
            usage = await asyncio.to_thread(trash_service.usage)
            freed = int(usage.get("bytes") or 0)
            deleted, failed = await asyncio.to_thread(trash_service.empty, None, True)
            items = int(deleted)
        else:  # temp_chunks / transcodes — TTL policy, active work is preserved
            summary = await asyncio.to_thread(retention_service.sweep, [area])
            items = int(summary.get("removed_files") or 0)
            freed = int(summary.get("freed_bytes") or 0)
        result["areas"][area] = {"items": items, "bytes": freed, "failed": failed}
        result["items"] += items
        result["total_bytes"] += freed
        result["failed"] += failed

    record_history("caches", items=result["items"], failed=result["failed"],
                   freed_bytes=result["total_bytes"], scope=",".join(dict.fromkeys(areas)))
    invalidate_suggestions()
    try:
        from backend.services.notifications_service import emit_event
        emit_event("success", "Caches cleaned",
                   f"Removed {result['items']} item(s), freed "
                   f"{result['total_bytes'] / 1_048_576:.1f} MB", source="storage")
    except Exception:  # noqa: BLE001 — a notification must never break the action
        logger.debug("Cache notification failed", exc_info=True)
    logger.info("Cleaned cache area(s) %s: %d item(s), %d byte(s)",
                ",".join(areas), result["items"], result["total_bytes"])
    return result


# ── History ──────────────────────────────────────────────────────────────────

def _history_store():
    return json_store.get(settings.DATA_DIR / "disk_clean_history.json", dict, version=1)


def record_history(action: str, *, items: int, failed: int, freed_bytes: int,
                   scope: str = "") -> None:
    """Append one reclaim action to the capped history (newest last on disk)."""
    entry = {
        "at": _now_iso(),
        "action": action,
        "items": int(items),
        "failed": int(failed),
        "bytes": int(freed_bytes),
        "scope": scope,
    }

    def mutate(data):
        rows = data.get("items")
        if not isinstance(rows, list):
            rows = []
        rows.append(entry)
        data["items"] = rows[-HISTORY_LIMIT:]
        return entry

    try:
        _history_store().update(mutate)
    except Exception:  # noqa: BLE001 — history must never break the action
        logger.error("Could not record disk-clean history entry", exc_info=True)


def history(limit: int = HISTORY_LIMIT) -> dict:
    """Recent reclaim actions, newest first."""
    data = _history_store().load()
    rows = data.get("items") if isinstance(data, dict) else []
    if not isinstance(rows, list):
        rows = []
    capped = max(1, min(int(limit or HISTORY_LIMIT), HISTORY_LIMIT))
    return {"items": list(reversed(rows))[:capped]}


__all__ = [
    "CLEANABLE_CACHES", "MAX_DELETE_PATHS", "HISTORY_LIMIT", "ScanCancelled",
    "cancel_scan", "clean_caches", "delete_paths", "get_scan", "get_suggestions",
    "history", "invalidate_suggestions", "protected_reason", "record_history",
    "scan_sync", "start_scan", "top_files_sync", "vacuum_journal",
]
