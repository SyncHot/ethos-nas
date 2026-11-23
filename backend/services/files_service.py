"""EthOS NAS — File Operations Service

Handles copy, move/cut, delete (soft/hard), zip archive generation, and chunked upload.
Broadcasts mutation events to connected WebSocket clients via registered callbacks.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable

from backend.config import settings

logger = logging.getLogger("ethos-nas.services.files_service")


# ── Callback chain for event broadcasting ────────────────────────────────────

_broadcast_callbacks: list[Callable] = []


def on_broadcast(callback: Callable) -> None:
    """Register a function to be called after each mutation event."""
    _broadcast_callbacks.append(callback)


class _EventStore:
    """In-memory event store for file mutations.

    Stores events keyed by directory path so the File Manager can filter
    which directories to refetch when a mutation occurs.
    """

    def __init__(self):
        self._events: list[dict] = []  # {action, path, items, timestamp}

    def add(self, event: dict) -> None:
        import time as _time
        self._events.append({**event, "timestamp": _time.time()})
        if len(self._events) > 100:
            self._events = self._events[-50:]

    def get_for_path(self, dir_path: str) -> list[dict]:
        """Return mutations that affected this directory or its children."""
        abs_dir = _sanitize(dir_path) if dir_path else "/"
        results = []
        for ev in reversed(self._events):
            ev_path = ev.get("path", "")
            if ev_path == abs_dir or ev_path.startswith(abs_dir + "/"):
                results.append(ev)
        return results

    def clear(self) -> None:
        self._events.clear()


_file_events = _EventStore()


def broadcast_mutation(action: str, path: str, items: list[dict] | None = None) -> None:
    """Broadcast a file mutation event. Called after every successful mutation."""
    _file_events.add({"action": action, "path": path, "items": items or []})
    for cb in _broadcast_callbacks:
        try:
            cb({"action": action, "path": path, "items": items or []})
        except Exception as e:
            logger.error("Broadcast callback failed: %s", e)


def get_recent_events_for(path: str) -> list[dict]:
    """Return recent events for a directory (used by reactive sync)."""
    return _file_events.get_for_path(path)


# ── Path sanitization helper ────────────────────────────────────────────────

def _sanitize(path: str) -> str:
    """Resolve and return an absolute filesystem path."""
    if not path or not isinstance(path, str):
        raise ValueError("Path is required")
    return os.path.abspath(os.path.expanduser(path))


# ── File operation progress registry ────────────────────────────────────────
#
# Copy/move of large files takes minutes. The blocking work runs in worker
# threads (never on the event loop) and clients poll get_file_op() for live
# byte-level progress.

_ops: dict[str, dict] = {}
_OPS_MAX = 64
_COPY_CHUNK_SIZE = 8 * 1024 * 1024


def _prune_ops() -> None:
    """Keep the op registry bounded — drop oldest finished ops first."""
    for op in sorted(_ops.values(), key=lambda o: o["started"]):
        if len(_ops) <= _OPS_MAX:
            return
        if op["status"] != "running":
            del _ops[op["op_id"]]


def _path_total_size(path: str) -> int:
    """Total size in bytes of a file, or all files under a directory."""
    if os.path.isdir(path):
        total = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                with contextlib.suppress(OSError):
                    total += os.path.getsize(os.path.join(root, f))
        return total
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _copy_file_with_progress(src: str, dst: str, op: dict) -> None:
    """Copy a single file in chunks, updating the op's byte progress.

    Lands on ``<dst>.part`` first and only renames it into place once the copy
    finished, so a full disk or an unplugged drive can never leave a truncated
    file sitting under the real name.
    """
    part = dst + ".part"
    try:
        with open(src, "rb") as fsrc, open(part, "wb") as fdst:
            while True:
                buf = fsrc.read(_COPY_CHUNK_SIZE)
                if not buf:
                    break
                fdst.write(buf)
                op["bytes_done"] += len(buf)
            fdst.flush()
            os.fsync(fdst.fileno())
        shutil.copystat(src, part)
        os.replace(part, dst)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.remove(part)
        logger.error("Copy failed %s → %s: %s", src, dst, exc)
        raise


def _progress_copy_function(op: dict):
    """shutil.copytree copy_function that reports byte progress."""
    def _cf(src: str, dst: str) -> str:
        _copy_file_with_progress(src, dst, op)
        return dst
    return _cf


def _unique_dest(dest_dir: str, name: str) -> str:
    """First free path for `name` in `dest_dir` (auto-suffix _1, _2…).

    The old check-then-use loop was a TOCTOU race: two concurrent copies could
    both be handed the same name and the second ``os.rename`` silently replaced
    the first. Probing with O_CREAT|O_EXCL makes the reservation atomic.
    """
    base, ext = os.path.splitext(name)
    for counter in range(0, 500):
        candidate = os.path.join(dest_dir, name if counter == 0 else f"{base}_{counter}{ext}")
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            continue
        except OSError:
            # Can't reserve here (read-only dir, odd filesystem) — fall back to
            # a plain existence check rather than failing the whole operation.
            if not os.path.exists(candidate):
                return candidate
            continue
        os.close(fd)
        os.unlink(candidate)   # release the reservation, the caller writes it
        return candidate
    raise OSError(f"Could not find a free name for {name!r} in {dest_dir}")


def unique_dest(dest_dir: str, name: str) -> str:
    """Public wrapper for routers that need a non-colliding destination."""
    return _unique_dest(dest_dir, name)


def _op_percent(op: dict) -> float:
    """Overall progress percent — byte-based when sizes are known."""
    if op["bytes_total"] > 0:
        return min(100.0, round(op["bytes_done"] / op["bytes_total"] * 100, 1))
    if op["total"] > 0:
        done_items = op["index"] + (1 if op["status"] != "running" else 0)
        return min(100.0, round(done_items / op["total"] * 100, 1))
    return 100.0 if op["status"] == "done" else 0.0


def _finish_op(op: dict) -> None:
    """Mark an op done, broadcast the mutation event, emit a notification."""
    op["status"] = "done"
    op["current"] = None
    ok_items = [d for d in op["details"] if d["status"] == "ok"]
    broadcast_mutation(
        op["action"], op["dest_dir"],
        [{"name": d["name"], "size": _path_total_size(os.path.join(op["dest_dir"], d["name"]))}
         for d in ok_items]
    )
    # Persistent notification (bell drawer) — must never raise.
    try:
        from backend.services.notifications_service import emit_event
        verb = "Copied" if op["action"] == "copy" else "Moved"
        if op["failed"]:
            emit_event("warning", f"{verb} with errors",
                       f'{op["succeeded"]} item(s) {op["action"]}ed to {op["dest_dir"]}, '
                       f'{op["failed"]} failed', source="files")
        else:
            emit_event("success", f"{verb} {len(ok_items)} item(s)",
                       f'To {op["dest_dir"]}', source="files")
    except Exception as exc:  # noqa: BLE001 — notification must not break the op
        logger.debug("Notification emit failed: %s", exc)


def _copy_worker(op: dict) -> None:
    """Blocking copy loop — runs in a worker thread."""
    dest_dir = op["dest_dir"]
    for i, src in enumerate(op["sources"]):
        op["index"] = i
        name = os.path.basename(src.rstrip("/")) or src
        op["current"] = name
        dst = _unique_dest(dest_dir, name)
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, copy_function=_progress_copy_function(op))
            else:
                _copy_file_with_progress(src, dst, op)
            op["details"].append({"name": os.path.basename(dst), "status": "ok"})
            op["succeeded"] += 1
        except PermissionError:
            op["details"].append({"name": name, "status": "permission_denied"})
            op["failed"] += 1
        except Exception as exc:  # noqa: BLE001 — keep going with remaining items
            logger.error("Copy failed %s → %s: %s", src, dst, exc)
            op["details"].append({"name": name, "status": f"error: {exc}"})
            op["failed"] += 1
    _finish_op(op)


def _move_worker(op: dict) -> None:
    """Blocking move loop — runs in a worker thread.

    Same-filesystem moves use atomic os.rename; cross-device falls back to
    chunked copy + delete (with progress).
    """
    dest_dir = op["dest_dir"]
    for i, src in enumerate(op["sources"]):
        op["index"] = i
        name = os.path.basename(src.rstrip("/")) or src
        op["current"] = name
        dst = _unique_dest(dest_dir, name)
        try:
            if os.path.isdir(src):
                try:
                    os.rename(src, dst)
                except OSError:
                    shutil.copytree(src, dst, copy_function=_progress_copy_function(op))
                    shutil.rmtree(src)
            else:
                try:
                    os.rename(src, dst)
                except OSError:
                    _copy_file_with_progress(src, dst, op)
                    os.remove(src)
            op["details"].append({"name": os.path.basename(dst), "status": "ok"})
            op["succeeded"] += 1
        except PermissionError:
            op["details"].append({"name": name, "status": "permission_denied"})
            op["failed"] += 1
        except Exception as exc:  # noqa: BLE001 — keep going with remaining items
            logger.error("Move failed %s → %s: %s", src, dst, exc)
            op["details"].append({"name": name, "status": f"error: {exc}"})
            op["failed"] += 1
    _finish_op(op)


async def start_copy_operation(sources: list[str], dest_dir: str) -> str:
    """Start a background copy. Returns the op_id immediately.

    Handles name collisions by appending _1, _2, etc.
    Progress is available via get_file_op(op_id).
    """
    return await _start_operation("copy", sources, dest_dir, _copy_worker)


async def start_move_operation(sources: list[str], dest_dir: str) -> str:
    """Start a background move/cut. Returns the op_id immediately."""
    return await _start_operation("move", sources, dest_dir, _move_worker)


async def _start_operation(action: str, sources: list[str], dest_dir: str,
                           worker) -> str:
    src_dir = _sanitize(dest_dir)
    if not os.path.isdir(src_dir):
        raise ValueError(f"Destination is not a directory: {dest_dir}")

    abs_sources = [_sanitize(s) for s in sources]
    # Moving (or copying) a directory into its own subtree is unrecoverable:
    # os.rename fails with EINVAL, so _move_worker falls back to copytree+rmtree
    # — and rmtree then deletes both the original and the only copy. Cut →
    # navigate into the folder → paste reached this path; reject it up front.
    dest_real = os.path.realpath(src_dir)
    for s in abs_sources:
        s_real = os.path.realpath(s)
        if dest_real == s_real or dest_real.startswith(s_real + os.sep):
            label = os.path.basename(s.rstrip(os.sep)) or s
            raise ValueError(
                f"Cannot {action} '{label}' into itself: "
                f"the destination is inside the source folder")
    # Pre-compute total size (in a thread — dir walks can be slow) so the
    # first progress poll already has a denominator.
    bytes_total = await asyncio.to_thread(
        lambda: sum(_path_total_size(p) for p in abs_sources)
    )

    op_id = uuid.uuid4().hex[:12]
    op = {
        "op_id": op_id, "action": action, "status": "running",
        "dest_dir": src_dir, "sources": abs_sources,
        "total": len(abs_sources), "index": 0, "current": None,
        "bytes_total": bytes_total, "bytes_done": 0,
        "succeeded": 0, "failed": 0, "details": [],
        "started": time.time(),
    }
    _prune_ops()
    _ops[op_id] = op
    op["_task"] = asyncio.create_task(asyncio.to_thread(worker, op))
    return op_id


def get_file_op(op_id: str) -> dict | None:
    """Return a progress snapshot for an in-flight or recent file operation."""
    op = _ops.get(op_id)
    if not op:
        return None
    snap = {k: v for k, v in op.items() if k != "_task"}
    snap["percent"] = _op_percent(op)
    return snap





# ── Delete (Soft & Hard) ─────────────────────────────────────────────────────

async def soft_delete_items(paths: list[str], owner: str) -> dict:
    """Move files/directories into *owner*'s trash.

    Thin wrapper over `services/trash_service.py` — the recycle bin is per user,
    so the owner comes from the authenticated request (routers derive it from
    the JWT, never from the request body). Returns records of trashed items for
    event broadcasting.

    Args:
        paths: List of absolute paths to move to trash.
        owner: Username whose trash namespace receives the items.

    Returns:
        {trashed: N, failed: N, details: [...]}
    """
    from backend.services import trash_service  # local: keeps module import graph acyclic

    trashed = 0
    failed = 0
    details = []
    parents: set[str] = set()

    for path in paths:
        abs_path = _sanitize(path)
        if not os.path.lexists(abs_path):
            details.append({"name": os.path.basename(abs_path), "status": "not_found", "path": abs_path})
            failed += 1
            continue

        item_size = os.path.getsize(abs_path) if os.path.isfile(abs_path) else -1
        try:
            result = await asyncio.to_thread(
                trash_service.move_to_trash, abs_path, owner)
            details.append({"name": result["name"], "status": "ok", "size": item_size})
            parents.add(os.path.dirname(abs_path))
            trashed += 1
        except (OSError, ValueError) as exc:
            logger.error("Soft delete failed %s: %s", abs_path, exc)
            details.append({"name": os.path.basename(abs_path), "status": f"error: {exc}",
                            "path": abs_path, "reason": str(exc)})
            failed += 1

    for parent in sorted(parents):
        broadcast_mutation("delete_soft", parent, details)

    return {"trashed": trashed, "failed": failed, "details": details}


# ── Background trash / purge (progress-tracked) ─────────────────────────────


def _move_into_trash(src: str, dest, op: dict) -> None:
    """Move one item into its reserved trash name, reporting byte progress.

    Same-filesystem moves are atomic renames. Cross-device moves (e.g. /mnt →
    the disk holding the recycle bin) become a copy with progress; a failed
    copy is removed again so the trash never shows a half-transferred item.

    Symlinks are never followed: only the link itself is moved.
    """
    try:
        os.rename(src, dest)
        return
    except OSError:
        pass  # EXDEV / EINVAL — fall back to copy + delete

    try:
        if os.path.islink(src):
            os.symlink(os.readlink(src), dest)
            os.unlink(src)
        elif os.path.isdir(src):
            shutil.copytree(src, dest, symlinks=True,
                            copy_function=_progress_copy_function(op))
            shutil.rmtree(src)
        else:
            _copy_file_with_progress(src, str(dest), op)
            os.remove(src)
    except Exception:
        # Never leave a partial copy behind in the trash namespace.
        with contextlib.suppress(Exception):
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            else:
                os.unlink(dest)
        raise


def _trash_worker(op: dict) -> None:
    """Blocking move-to-trash loop — runs in a worker thread."""
    from backend.services import trash_service  # local: keeps the import graph acyclic

    parents: dict[str, list[dict]] = {}
    for i, p in enumerate(op["paths"]):
        op["index"] = i
        name = os.path.basename(p.rstrip("/")) or p
        op["current"] = name
        try:
            if not os.path.lexists(p):
                op["details"].append({"name": name, "status": "not_found", "path": p})
                op["failed"] += 1
                continue
            dest, meta = trash_service.reserve_dest(p, op["owner"])
            _move_into_trash(p, dest, op)
            trash_service.finalize_move(dest, meta)
            op["details"].append({"name": meta["trashed_name"], "status": "ok", "path": p})
            parents.setdefault(os.path.dirname(p), []).append({"name": name})
            op["succeeded"] += 1
        except PermissionError as exc:
            op["details"].append({"name": name, "status": "permission_denied",
                                  "path": p, "reason": str(exc)})
            op["failed"] += 1
        except (OSError, ValueError) as exc:
            logger.error("Move to trash failed for %s: %s", p, exc)
            op["details"].append({"name": name, "status": f"error: {exc}",
                                  "path": p, "reason": str(exc)})
            op["failed"] += 1

    op["status"] = "done"
    op["current"] = None
    for parent in sorted(parents):
        broadcast_mutation("delete_soft", parent, parents[parent])
    try:
        from backend.services.notifications_service import emit_event
        if op["failed"]:
            emit_event("warning", "Moved to Trash with errors",
                       f'{op["succeeded"]} item(s) moved, {op["failed"]} failed', source="files")
        else:
            emit_event("success", f'Moved {op["succeeded"]} item(s) to Trash', "", source="files")
    except Exception as exc:  # noqa: BLE001 — notification must not break the op
        logger.debug("Notification emit failed: %s", exc)


def _purge_worker(op: dict) -> None:
    """Blocking permanent-delete of trash entries — runs in a worker thread."""
    from backend.services import trash_service  # local: keeps the import graph acyclic

    for i, item in enumerate(op["items"]):
        op["index"] = i
        op["current"] = item["name"]
        try:
            trash_service.delete_item(item["owner"], item["name"])
            op["details"].append({"name": item["name"], "status": "ok"})
            op["succeeded"] += 1
        except FileNotFoundError:
            op["details"].append({"name": item["name"], "status": "not_found"})
            op["failed"] += 1
        except PermissionError as exc:
            op["details"].append({"name": item["name"], "status": "permission_denied",
                                  "reason": str(exc)})
            op["failed"] += 1
        except (OSError, ValueError) as exc:
            logger.error("Trash purge failed for %s: %s", item["name"], exc)
            op["details"].append({"name": item["name"], "status": f"error: {exc}",
                                  "reason": str(exc)})
            op["failed"] += 1

    op["status"] = "done"
    op["current"] = None
    try:
        from backend.services.notifications_service import emit_event
        if op["failed"]:
            emit_event("warning", "Trash purge with errors",
                       f'{op["succeeded"]} item(s) deleted, {op["failed"]} failed', source="files")
        else:
            emit_event("success", f'Removed {op["succeeded"]} item(s) from Trash', "", source="files")
    except Exception as exc:  # noqa: BLE001 — notification must not break the op
        logger.debug("Notification emit failed: %s", exc)


async def start_trash_operation(paths: list[str], owner: str) -> str:
    """Start a background move-to-trash. Returns the op_id immediately."""
    abs_paths = [_sanitize(p) for p in paths]
    if not abs_paths:
        raise ValueError("paths must be a non-empty array")
    # Byte totals give real progress for cross-device moves; the walk runs in a
    # thread so a huge tree never blocks the event loop. A delete that renames
    # into the item's own volume bin (trash_service.volume_bin_for) moves no
    # bytes, so the pre-walk is pure latency and is skipped.
    from backend.services import trash_service  # local: keeps the import graph acyclic
    if all(trash_service.volume_bin_for(p) is not None for p in abs_paths):
        bytes_total = 0
    else:
        bytes_total = await asyncio.to_thread(
            lambda: sum(_path_total_size(p) for p in abs_paths)
        )
    op_id = uuid.uuid4().hex[:12]
    op = {
        "op_id": op_id, "action": "trash", "status": "running",
        "paths": abs_paths, "owner": owner,
        "total": len(abs_paths), "index": 0, "current": None,
        "bytes_total": bytes_total, "bytes_done": 0,
        "succeeded": 0, "failed": 0, "details": [],
        "started": time.time(),
    }
    _prune_ops()
    _ops[op_id] = op
    op["_task"] = asyncio.create_task(asyncio.to_thread(_trash_worker, op))
    return op_id


async def start_purge_operation(items: list[dict]) -> str:
    """Start a background permanent delete of trash entries.

    ``items`` is ``[{"name": "<trash name>", "owner": "<username>"}, …]`` —
    ownership is resolved by the router from the JWT before this call.
    """
    if not items:
        raise ValueError("No trash items selected")
    op_id = uuid.uuid4().hex[:12]
    op = {
        "op_id": op_id, "action": "purge", "status": "running",
        "items": items, "total": len(items), "index": 0, "current": None,
        "bytes_total": 0, "bytes_done": 0,
        "succeeded": 0, "failed": 0, "details": [],
        "started": time.time(),
    }
    _prune_ops()
    _ops[op_id] = op
    op["_task"] = asyncio.create_task(asyncio.to_thread(_purge_worker, op))
    return op_id


def _rmtree_collect(path: str) -> list[str]:
    """Remove a tree, collecting per-entry failures instead of dying on the first.

    ``shutil.rmtree`` aborts on the first error and leaves a half-deleted tree
    with only an opaque errno. With an ``onexc`` handler the remaining entries
    are still removed and the caller learns exactly what survived (read-only
    files, immutable dirs, I/O errors) instead of a silent partial success.
    """
    failures: list[str] = []

    def _onexc(func, target, exc):
        failures.append(f"{target}: {exc}")
        logger.error("Hard delete: could not remove %s: %s", target, exc)

    shutil.rmtree(path, onexc=_onexc)
    return failures


def _delete_worker(op: dict) -> None:
    """Blocking hard-delete loop — runs in a worker thread.

    No pre-walk for sizes: stat-ing every file of a huge tree on the event
    loop (or even just before deletion) is what made large deletes freeze
    the whole server. Progress is item-based instead.
    """
    audit = []
    for i, p in enumerate(op["paths"]):
        op["index"] = i
        name = os.path.basename(p.rstrip("/")) or p
        op["current"] = name
        try:
            # lexists: a dangling symlink must still be removable; a symlink to a
            # directory is unlinked, never followed into the target tree.
            if not os.path.lexists(p):
                op["details"].append({"name": name, "status": "not_found", "path": p})
                op["failed"] += 1
                continue
            if os.path.islink(p):
                os.unlink(p)
                problems: list[str] = []
            elif os.path.isdir(p):
                problems = _rmtree_collect(p)
            else:
                os.remove(p)
                problems = []
            if problems:
                # Some entries survived (permissions/immutable/I-O): report the
                # item as failed instead of pretending the tree is gone.
                op["details"].append({
                    "name": name, "path": p, "reason": problems[0],
                    "status": f"partial: {len(problems)} entrie(s) could not be removed",
                })
                op["failed"] += 1
                logger.error("Hard delete left %d entrie(s) under %s (first: %s)",
                             len(problems), p, problems[0])
                continue
            # Record deletion for audit trail + remember parent dir so the
            # finish step can broadcast per-directory mutation events.
            audit.append({"path": p, "name": name, "deleted_at": time.time()})
            op["details"].append({"name": name, "status": "ok", "parent": os.path.dirname(p), "path": p})
            op["succeeded"] += 1
        except PermissionError as exc:
            op["details"].append({"name": name, "status": "permission_denied",
                                  "path": p, "reason": str(exc)})
            op["failed"] += 1
        except OSError as exc:
            logger.error("Hard delete failed %s: %s", p, exc)
            op["details"].append({"name": name, "status": f"error: {exc}",
                                  "path": p, "reason": str(exc)})
            op["failed"] += 1

    # Persist deletion log (keep last 50 entries) — safe here, we are in a thread.
    try:
        log_file = settings.DATA_DIR / "deletion_log.json"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        entries = json.loads(log_file.read_text()) if log_file.exists() else []
        if not isinstance(entries, list):
            entries = []
        log_file.write_text(json.dumps((entries + audit)[-50:]))
    except (OSError, ValueError):
        logger.error("Could not persist the deletion audit log — %d deletion(s) not recorded",
                     len(audit), exc_info=True)

    _finish_delete_op(op)

def _finish_delete_op(op: dict) -> None:
    """Mark a delete op done, broadcast per-parent mutation events, notify."""
    op["status"] = "done"
    op["current"] = None
    by_parent: dict[str, list[dict]] = {}
    for d in op["details"]:
        if d["status"] == "ok":
            by_parent.setdefault(d.get("parent", "/"), []).append({"name": d["name"]})
    for parent, items in by_parent.items():
        broadcast_mutation("delete_hard", parent, items)

    try:
        from backend.services.notifications_service import emit_event
        if op["failed"]:
            emit_event("warning", f"Deleted {op['succeeded']} item(s) with errors",
                       f"{op['failed']} failed to delete", source="files")
        else:
            emit_event("success", f"Permanently deleted {op['succeeded']} item(s)", "", source="files")
    except Exception as exc:
        logger.debug("Notification emit failed: %s", exc)

async def start_hard_delete_operation(paths: list[str]) -> str:
    """Start a background hard delete. Returns the op_id immediately.

    The blocking rmtree/remove work runs in a worker thread, so deleting
    multi-GB trees never blocks the event loop (previously froze the whole
    server → 504s at the reverse proxy). Progress via get_file_op(op_id).
    """
    if not paths or not isinstance(paths, list):
        raise ValueError("paths must be a non-empty array")
    abs_paths = [_sanitize(p) for p in paths]

    op_id = uuid.uuid4().hex[:12]
    op = {
        "op_id": op_id, "action": "hard_delete", "status": "running",
        "paths": abs_paths, "total": len(abs_paths), "index": 0, "current": None,
        "bytes_total": 0, "bytes_done": 0,
        "succeeded": 0, "failed": 0, "details": [],
        "started": time.time(),
    }
    _prune_ops()
    _ops[op_id] = op
    op["_task"] = asyncio.create_task(asyncio.to_thread(_delete_worker, op))
    return op_id


# ── Zip Archive (streaming) ──────────────────────────────────────────────────

def _create_zip_archive_sync(sources: list[str], output_path: str | None = None) -> dict:
    """Blocking zip creation — run in a worker thread via create_zip_archive.

    Streams items into the archive without loading everything into memory.
    Returns {path, filename, size, archived_count, failed}.
    """
    import time as _time
    import zipfile

    if not sources:
        raise ValueError("No source items provided")

    # Generate output path if not specified
    if not output_path:
        ts = _time.strftime("%Y%m%d_%H%M%S")
        output_dir = settings.DATA_DIR / "temp"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(output_dir / f"ethos_download_{ts}.zip")

    archived_count = 0
    failed = 0

    try:
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for src in sources:
                abs_src = _sanitize(src)
                if not os.path.exists(abs_src):
                    logger.warning("Zip source not found: %s", abs_src)
                    failed += 1
                    continue

                name_in_zip = os.path.basename(abs_src)

                if os.path.isdir(abs_src):
                    for root, _dirs, files in os.walk(abs_src):
                        rel_root = os.path.relpath(root, os.path.dirname(abs_src))
                        for fname in files:
                            fpath = os.path.join(root, fname)
                            arcname = os.path.join(rel_root, fname).replace(os.sep, '/')
                            try:
                                zf.write(fpath, arcname)
                                archived_count += 1
                            except Exception as exc:
                                logger.error("Zip add failed %s: %s", fpath, exc)
                                failed += 1
                else:
                    try:
                        zf.write(abs_src, name_in_zip)
                        archived_count += 1
                    except Exception as exc:
                        logger.error("Zip add failed %s: %s", abs_src, exc)
                        failed += 1

        archive_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0

    except Exception as exc:
        raise RuntimeError(f"Failed to create zip archive: {exc}") from exc

    # Broadcast event with archive metadata
    broadcast_mutation(
        "zip", output_path,
        [{"name": os.path.basename(output_path), "size": archive_size}]
    )

    return {
        "path": output_path,
        "filename": os.path.basename(output_path),
        "size": archive_size,
        "archived_count": archived_count,
        "failed": failed,
    }


async def create_zip_archive(sources: list[str], output_path: str | None = None) -> dict:
    """Create a zip archive from the given paths.

    The blocking zip work runs in a worker thread so archiving a large
    directory never stalls the event loop.

    Args:
        sources: List of absolute source paths (files or directories).
        output_path: Optional custom output path; auto-generated if not provided.

    Returns:
        {path, filename, size, archived_count, failed}
    """
    return await asyncio.to_thread(_create_zip_archive_sync, sources, output_path)


# ── Archive Extraction ───────────────────────────────────────────────────────

# Every format the File Manager can unpack. The stdlib covers ZIP, tar
# (gz/bz2/xz/zst) and the classic single-stream compressors; everything else
# shells out to an installed extractor — bsdtar (libarchive-tools) first, then
# 7-Zip, unar and unrar. `frontend/static/apps/files.js` mirrors these names;
# tests/test_archive_extract.py fails when the two lists drift apart.
_ZIP_FAMILY_EXTS = {
    ".zip", ".zipx", ".jar", ".war", ".ear", ".whl", ".pyz", ".cbz",
    ".xpi", ".crx", ".vsix", ".nupkg", ".apk",
}
_TAR_EXTS = {
    ".tar", ".tgz", ".tbz", ".txz", ".tzst", ".tlz", ".tlz4", ".tlzma", ".taz",
}
# Multi-suffix archives; the stdlib opens all but the lz4/lzip/lzma/.Z tars.
_TAR_MULTI_SUFFIXES = (
    ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lz4", ".tar.lz",
    ".tar.lzma", ".tar.z",
)
_CLI_TAR_SUFFIXES = (
    ".tar.lz4", ".tar.lz", ".tar.lzma", ".tar.z", ".tlz4", ".tlz", ".tlzma", ".taz",
)
# Bare single-stream files (decompressed next to the archive, not into a folder).
_STREAM_EXTS = {".gz", ".bz2", ".xz", ".lzma", ".lz", ".z", ".zst", ".lz4"}
_STDLIB_STREAM_EXTS = {".gz", ".bz2", ".xz", ".lzma"}
# Formats that always need an external tool.
_CLI_EXTS = {".7z", ".rar", ".cab", ".iso", ".ar", ".deb", ".cpio", ".rpm"}

ARCHIVE_EXTS = _ZIP_FAMILY_EXTS | _TAR_EXTS | _STREAM_EXTS | _CLI_EXTS

# Multi-volume sets: archive.part01.rar, archive.r00, archive.7z.001, …
# Kept in sync with the Download Manager grouping patterns (routers/downloads.py)
# so a package extracted there and a file right-clicked here agree on volumes.
SPLIT_PATTERNS = (
    re.compile(r"(?i)^(?P<base>.+?)\.part0*(?P<part>\d{1,4})\.(?:rar|zip|7z)$"),
    re.compile(r"(?i)^(?P<base>.+?)\.r(?P<part>\d{2})$"),
    re.compile(r"(?i)^(?P<base>.+?)\.z(?P<part>\d{2})$"),
    re.compile(r"(?i)^(?P<base>.+?)\.(?P<part>\d{3})$"),
)

# Generous ceiling: only there to stop a prompt-hung tool from leaking a worker.
_CLI_TIMEOUT = 6 * 60 * 60


class MissingExtractor(RuntimeError):
    """No installed tool can open this format (the router maps it to 409)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.requirement = "archive-tools"


def split_volume_info(name: str) -> dict | None:
    """Volume membership for a multi-part archive name, or None.

    Returns ``{"base": "foo", "part": 3}``; ``part`` is only an ordering key.
    """
    base_name = os.path.basename(str(name or "").replace("\\", "/")).strip()
    if not base_name:
        return None
    for pattern in SPLIT_PATTERNS:
        match = pattern.match(base_name)
        if not match:
            continue
        base = match.group("base").strip().rstrip(".")
        if not base:
            return None
        return {"base": base, "part": int(match.group("part") or 0)}
    return None


def _split_members(directory: str, base: str) -> list[tuple[int, str]]:
    """(order, filename) for every present volume of ``base``, lowest first."""
    found: dict[str, int] = {}
    try:
        entries = os.listdir(directory)
    except OSError:
        logger.warning("Cannot list %s while resolving archive volumes", directory)
        entries = []
    low_base = base.lower()
    for entry in entries:
        info = split_volume_info(entry)
        if info and info["base"].lower() == low_base:
            found[entry] = info["part"]
    # `foo.rar` is volume 1 of a `.r00`/`.z00` set, so it must sort first.
    for ext in (".rar", ".zip", ".7z"):
        if os.path.isfile(os.path.join(directory, base + ext)):
            found[base + ext] = -1
    return sorted((part, name) for name, part in found.items())


def find_first_volume(archive_path: str) -> str:
    """First volume of a split archive (unchanged for ordinary archives)."""
    info = split_volume_info(os.path.basename(archive_path))
    if not info:
        return archive_path
    directory = os.path.dirname(archive_path) or "/"
    members = _split_members(directory, info["base"])
    if len(members) <= 1:
        return archive_path
    return os.path.join(directory, members[0][1])


def is_extractable_name(name: str) -> bool:
    """True when the File Manager should offer Extract for this file name."""
    lower = str(name or "").lower()
    if not lower:
        return False
    if lower.endswith(_TAR_MULTI_SUFFIXES):
        return True
    if os.path.splitext(lower)[1] in ARCHIVE_EXTS:
        return True
    return split_volume_info(lower) is not None


def _archive_stem(name: str) -> str:
    """Strip archive/volume suffixes: 'foo.tar.gz' → 'foo', 'bar.part1.rar' → 'bar'."""
    lower = name.lower()
    for multi in _TAR_MULTI_SUFFIXES:
        if lower.endswith(multi):
            return name[: -len(multi)] or name
    info = split_volume_info(name)
    if info:
        base = info["base"]
        for multi in _TAR_MULTI_SUFFIXES:
            if base.lower().endswith(multi):
                return base[: -len(multi)] or base
        stem, ext = os.path.splitext(base)
        return stem if ext.lower() in ARCHIVE_EXTS else base
    stem, _ext = os.path.splitext(name)
    return stem or name


def _unique_dir(parent: str, base_name: str) -> str:
    """parent/base_name, or ' (1)', ' (2)'… if it already exists."""
    candidate = os.path.join(parent, base_name)
    n = 1
    while os.path.exists(candidate):
        candidate = os.path.join(parent, f"{base_name} ({n})")
        n += 1
    return candidate


def _unique_file(parent: str, filename: str) -> str:
    """parent/filename with a ' (n)' suffix if it already exists."""
    candidate = os.path.join(parent, filename)
    stem, ext = os.path.splitext(filename)
    n = 1
    while os.path.exists(candidate):
        candidate = os.path.join(parent, f"{stem} ({n}){ext}")
        n += 1
    return candidate


def _extract_zip(archive_path: str, dest_dir: str, *, overwrite: bool = False,
                 password: str | None = None) -> tuple[int, int]:
    """Extract a ZIP into dest_dir with zip-slip protection.

    Returns (extracted_count, skipped_existing). Existing files are skipped
    unless ``overwrite`` — an "extract here" must never clobber silently.
    """
    import zipfile

    real_dest = os.path.realpath(dest_dir)
    pwd = password.encode("utf-8") if password else None
    count = 0
    skipped_existing = 0
    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            # Reject absolute paths and any '..' component (zip-slip).
            if os.path.isabs(name) or ".." in parts:
                raise ValueError(f"Unsafe path in archive: {name}")
            target = os.path.join(dest_dir, *parts) if parts else dest_dir
            # Defense in depth: resolved target must stay inside dest_dir.
            real_target = os.path.realpath(target)
            if real_target != real_dest and not real_target.startswith(real_dest + os.sep):
                raise ValueError(f"Unsafe path in archive: {name}")
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            if os.path.lexists(target) and not overwrite:
                skipped_existing += 1
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # Write content directly (never follows symlinks stored in the zip).
            try:
                with zf.open(info, pwd=pwd) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except NotImplementedError:
                # ZIPX / WinZip AES — the stdlib cannot decode it; let the caller
                # retry with 7-Zip (NotImplementedError subclasses RuntimeError).
                raise
            except RuntimeError as exc:  # encrypted entry, missing/wrong password
                logger.error("Cannot decrypt %s in %s: %s", name, archive_path, exc)
                raise ValueError(f"Cannot decrypt {name}: set the archive password") from exc
            except OSError as exc:
                logger.error("Extraction failed for entry %s in %s: %s", name, archive_path, exc)
                raise ValueError(f"Corrupt or unreadable archive entry: {name}") from exc
            count += 1
    return count, skipped_existing


def _extract_tar(archive_path: str, dest_dir: str, *,
                 overwrite: bool = False) -> tuple[int, list[str], int]:
    """Extract a (possibly compressed) tar into dest_dir.

    Returns (file_count, skipped_special, skipped_existing). Uses the stdlib
    'data' filter which rejects absolute paths and '..' traversal and confines
    links to dest_dir; special files (FIFOs, devices — e.g. mail spool pipes) are
    *skipped* rather than aborting the whole extraction, since they are useless
    outside a live service. Existing files are skipped unless ``overwrite``.
    """
    import tarfile

    with tarfile.open(archive_path) as tf:
        safe: list = []
        skipped: list[str] = []
        skipped_existing = 0
        for m in tf.getmembers():
            try:
                # Pre-scan with the same filter extractall will apply, so we can
                # distinguish skippable special files from genuinely unsafe entries.
                tarfile.data_filter(m, dest_dir)
            except tarfile.SpecialFileError:
                skipped.append(m.name)
                continue
            except tarfile.TarError as exc:
                raise ValueError(f"Refusing to extract unsafe archive entry: {exc}") from exc
            if m.isfile() and not overwrite:
                target = os.path.join(dest_dir, *[p for p in m.name.split("/") if p not in ("", ".")])
                if os.path.lexists(target):
                    skipped_existing += 1
                    continue
            safe.append(m)
        tf.extractall(path=dest_dir, members=safe, filter="data")
    return len([m for m in safe if m.isfile()]), skipped, skipped_existing


def _extract_single_file(archive_path: str, parent_dir: str) -> tuple[str, int]:
    """Decompress a bare gz/bz2/xz/lzma stream next to the archive."""
    import bz2
    import gzip
    import lzma

    ext = os.path.splitext(archive_path)[1].lower()
    opener = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open, ".lzma": lzma.open}[ext]
    out_name = _unique_file(parent_dir, os.path.basename(archive_path)[: -len(ext)])
    try:
        with opener(archive_path, "rb") as src, open(out_name, "wb") as dst:
            shutil.copyfileobj(src, dst)
    except (OSError, EOFError, lzma.LZMAError) as exc:
        logger.error("Decompress failed %s → %s: %s", archive_path, out_name, exc)
        raise ValueError(f"Corrupt or unreadable archive: {exc}") from exc
    return out_name, 1


def _stream_tool_command(archive_path: str) -> tuple[list[str] | None, str]:
    """Command that writes a single decompressed stream to stdout."""
    bsdtar = shutil.which("bsdtar")
    if bsdtar:
        return [bsdtar, "-xOf", archive_path], "bsdtar"
    for name in ("7zz", "7z", "7za"):
        binary = shutil.which(name)
        if binary:
            return [binary, "e", "-so", archive_path], name
    return None, ""


def _decompress_stream_cli(archive_path: str, parent_dir: str) -> tuple[str, int, str]:
    """Bare lz/Z/zst/lz4 → one file beside the archive, via an external tool."""
    ext = os.path.splitext(archive_path)[1].lower()
    cmd, tool = _stream_tool_command(archive_path)
    if not cmd:
        raise MissingExtractor(
            f"No extractor installed for {os.path.basename(archive_path)}"
            " (install libarchive-tools or p7zip-full)"
        )
    out_name = _unique_file(parent_dir, os.path.basename(archive_path)[: -len(ext)])
    try:
        with open(out_name, "wb") as dst:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=dst,
                                  stderr=subprocess.PIPE, timeout=_CLI_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        with contextlib.suppress(OSError):
            os.unlink(out_name)
        raise ValueError(f"Decompress failed: {exc}") from exc
    if proc.returncode != 0:
        with contextlib.suppress(OSError):
            os.unlink(out_name)
        detail = proc.stderr.decode("utf-8", "replace").strip()[:300]
        raise ValueError(f"Decompress failed: {detail or f'exit {proc.returncode}'}")
    logger.info("Decompressed %s with %s", archive_path, tool)
    return out_name, 1, tool


_CLI_CANDIDATES = ("bsdtar", "7zz", "7z", "7za", "unar", "unrar")
# Split volumes: libarchive cannot follow .partN/.001 chains reliably.
_CLI_SPLIT_CANDIDATES = ("7zz", "7z", "7za", "unar", "unrar")


def _cli_command(tool: str, binary: str, archive_path: str, dest_dir: str,
                 overwrite: bool) -> list[str]:
    """Argument list for one extractor — never a shell string."""
    if tool == "bsdtar":
        cmd = [binary, "-xf", archive_path, "-C", dest_dir]
        if not overwrite:
            cmd.insert(1, "-k")  # keep (skip) existing files
        return cmd
    if tool in ("7zz", "7z", "7za"):
        # -aos skips existing files; -y would silently overwrite them.
        return [binary, "x", f"-o{dest_dir}", "-y" if overwrite else "-aos", archive_path]
    if tool == "unar":
        return [binary, "-o", dest_dir, "-f" if overwrite else "-s", archive_path]
    # unrar: -o- never overwrite, -o+ always overwrite
    return [binary, "x", "-o+" if overwrite else "-o-", archive_path, dest_dir + os.sep]


def _tree_files(root: str) -> set[str]:
    out: set[str] = set()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            out.add(os.path.join(dirpath, name))
    return out


def _extract_with_cli(archive_path: str, dest_dir: str, *, overwrite: bool = False,
                      password: str | None = None,
                      split: bool = False) -> tuple[int, str]:
    """Extract via an external tool. Returns (new_file_count, tool)."""
    candidates = _CLI_SPLIT_CANDIDATES if split else _CLI_CANDIDATES
    if password:
        # Only tools that read the password from stdin can be used: a value in
        # argv would leak into `ps`. bsdtar/unar only accept it on the command
        # line, so encrypted archives go through 7-Zip or unrar.
        candidates = ("7zz", "7z", "7za", "unrar")

    before = _tree_files(dest_dir)
    errors: list[str] = []
    for tool in candidates:
        binary = shutil.which(tool)
        if not binary:
            continue
        if tool == "unrar" and not archive_path.lower().endswith(".rar"):
            continue
        cmd = _cli_command(tool, binary, archive_path, dest_dir, overwrite)
        kwargs: dict = {"capture_output": True, "text": True, "timeout": _CLI_TIMEOUT}
        if password:
            cmd.insert(2, "-p")  # prompt mode: 7-Zip/unrar then read stdin
            kwargs["input"] = password + "\n"
        else:
            kwargs["stdin"] = subprocess.DEVNULL
        logger.info("Extracting %s with %s", archive_path, tool)
        try:
            proc = subprocess.run(cmd, **kwargs)
        except subprocess.TimeoutExpired:
            logger.error("Extractor %s timed out on %s", tool, archive_path)
            errors.append(f"{tool}: timed out")
            continue
        if proc.returncode == 0:
            return len(_tree_files(dest_dir) - before), tool
        detail = (proc.stderr or proc.stdout or "").strip()[:300] or f"exit {proc.returncode}"
        logger.warning("Extractor %s failed on %s: %s", tool, archive_path, detail)
        errors.append(f"{tool}: {detail}")

    if not errors:
        raise MissingExtractor(
            f"No extractor installed for {os.path.basename(archive_path)}"
            " (install libarchive-tools or p7zip-full)"
        )
    raise ValueError("Extraction failed: " + "; ".join(errors)[:400])


def extract_archive(archive_path: str, dest_dir: str | None = None,
                    subfolder: str | None = None, overwrite: bool = False,
                    password: str | None = None) -> dict:
    """Extract an archive (synchronous — run in a worker thread).

    - ``dest_dir``   where the result lands; default: next to the archive.
    - ``subfolder``  None → folder named after the archive ('foo.zip' → 'foo/',
      numeric suffix on collision); "" → straight into ``dest_dir`` (existing
      files are skipped unless ``overwrite``); any other value → that folder.
    - ``password``   only ever passed through stdin / zipfile, never argv.

    Split volumes (.partN/.001/.rNN) are resolved to their first volume.
    Returns {dest_dir, archive, extracted_count, skipped, skipped_existing, tool}.
    Raises ValueError for bad input/unsafe archives, PermissionError for a
    non-writable destination, MissingExtractor when no tool can open the format.
    """
    import tarfile
    import zipfile

    archive_path = os.path.abspath(archive_path)
    if not os.path.isfile(archive_path):
        raise ValueError(f"Archive not found: {archive_path}")
    source = find_first_volume(archive_path)
    name = os.path.basename(source)
    lower = name.lower()
    ext = os.path.splitext(lower)[1]
    parent = os.path.dirname(archive_path) or "/"
    base_dir = os.path.abspath(dest_dir) if dest_dir else parent
    if not os.path.isdir(base_dir):
        raise ValueError(f"Destination is not a directory: {base_dir}")
    if not os.access(base_dir, os.W_OK | os.X_OK):
        raise PermissionError(f"Destination is not writable: {base_dir}")

    stem = _archive_stem(os.path.basename(archive_path)) or _archive_stem(name) or "archive"
    if subfolder is None:
        target = _unique_dir(base_dir, stem)
    elif subfolder == "":
        target = base_dir
    else:
        target = os.path.join(base_dir, subfolder)

    def _ensure_target() -> None:
        """Create the folder-mode destination (bare streams write beside it)."""
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"Cannot create destination {target}: {exc}") from exc

    split_info = split_volume_info(name)
    split = bool(split_info) and len(
        _split_members(os.path.dirname(source) or "/", split_info["base"])
    ) > 1
    tool = ""
    skipped: list[str] = []
    skipped_existing = 0

    try:
        # 1) tar family (the stdlib opens all but lz4/lzip/lzma/.Z tars)
        if lower.endswith(_TAR_MULTI_SUFFIXES) or ext in _TAR_EXTS:
            _ensure_target()
            if lower.endswith(_CLI_TAR_SUFFIXES):
                count, tool = _extract_with_cli(source, target, overwrite=overwrite,
                                                split=split)
            else:
                count, skipped, skipped_existing = _extract_tar(source, target, overwrite=overwrite)
        # 2) ZIP family
        elif ext in _ZIP_FAMILY_EXTS:
            if not zipfile.is_zipfile(source):
                raise ValueError("Not a valid ZIP archive")
            _ensure_target()
            try:
                count, skipped_existing = _extract_zip(source, target, overwrite=overwrite,
                                                       password=password)
            except NotImplementedError:
                # ZIPX / AES entries need 7-Zip.
                count, tool = _extract_with_cli(source, target, overwrite=overwrite,
                                                password=password)
        # 3) bare single-stream files
        elif ext in _STREAM_EXTS:
            if tarfile.is_tarfile(source):
                _ensure_target()
                count, skipped, skipped_existing = _extract_tar(source, target, overwrite=overwrite)
            elif ext in _STDLIB_STREAM_EXTS:
                out_path, count = _extract_single_file(source, base_dir)
                return {"dest_dir": os.path.dirname(out_path), "archive": source,
                        "extracted_count": count, "skipped": [], "skipped_existing": 0,
                        "tool": ""}
            else:
                out_path, count, tool = _decompress_stream_cli(source, base_dir)
                return {"dest_dir": os.path.dirname(out_path), "archive": source,
                        "extracted_count": count, "skipped": [], "skipped_existing": 0,
                        "tool": tool}
        # 4) everything else needs an external extractor
        else:
            _ensure_target()
            count, tool = _extract_with_cli(source, target, overwrite=overwrite,
                                            password=password, split=split)
    except (zipfile.BadZipFile, tarfile.TarError) as exc:
        raise ValueError(f"Corrupt or unreadable archive: {exc}") from exc

    logger.info("Extracted %s → %s (%d files, %d skipped, tool=%s)",
                source, target, count, skipped_existing, tool or "stdlib")
    return {"dest_dir": target, "archive": source, "extracted_count": count,
            "skipped": skipped, "skipped_existing": skipped_existing, "tool": tool}


# ── Chunked Upload ───────────────────────────────────────────────────────────

class _ChunkStore:
    """Tracks in-progress chunked uploads per session/file.

    Each upload gets a temp file that accumulates chunks; on complete() the temp
    file is moved to its final destination.
    """

    def __init__(self):
        self._chunks: dict[str, dict] = {}  # upload_id -> metadata

    def create(self, file_id: str, dest_dir: str, filename: str,
               total_size: int = 0) -> dict:
        """Create a new chunked upload session.

        `total_size` is the exact byte count the client is about to send — the
        only way complete() can tell a finished upload from a truncated one.
        Returns the upload session info including temp file path.
        """
        tmp_dir = settings.DATA_DIR / "temp_chunks"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # Sanitize + validate up front so a relative or missing dest_dir fails fast
        # (400 at init) instead of resolving against the server CWD or losing the
        # whole upload at complete().
        abs_dest = _sanitize(dest_dir)
        if not os.path.isdir(abs_dest):
            raise ValueError(f"Destination is not a directory: {dest_dir}")

        # uuid4 instead of "<file_id>_<epoch-seconds>": two uploads of the same
        # file in the same second collided, and the second create() truncated the
        # first session's temp file while both appended into it.
        upload_id = f"{file_id}_{uuid.uuid4().hex[:10]}"
        dest_path = os.path.join(abs_dest, filename)
        chunk_file = str(tmp_dir / upload_id)

        self._chunks[upload_id] = {
            "file_id": file_id,
            "dest_path": dest_path,
            "filename": filename,
            "total_chunks": 0,  # unknown up front — progress is byte-based
            "total_size": int(total_size or 0),
            "received": set(),
            "_chunk_file": chunk_file,
        }

        # Materialise the temp file immediately: a zero-byte upload never sends
        # a chunk, so complete() would otherwise try to move a missing file and
        # fail with "Failed to finalize upload".
        try:
            with open(chunk_file, "wb"):
                pass
        except OSError as exc:
            self._chunks.pop(upload_id, None)
            logger.error("Chunked upload init failed for %s: %s", chunk_file, exc)
            raise ValueError(f"Cannot create upload session: {exc}") from exc

        return {
            "upload_id": upload_id,
            "dest_dir": dest_dir,
            "filename": filename,
        }

    def append_chunk(self, upload_id: str, chunk_index: int, data: bytes) -> dict:
        """Append a chunk to an in-progress upload.

        Returns status including current progress percentage and whether complete.
        """
        session = self._chunks.get(upload_id)
        if not session:
            raise ValueError(f"Unknown upload ID: {upload_id}")

        # Chunks are appended in arrival order, so an out-of-order (retried /
        # raced) chunk would silently corrupt the file. Require sequential
        # delivery instead of writing garbage.
        if chunk_index != len(session["received"]):
            logger.error("Chunk out of order for upload %s: got %d, expected %d",
                         upload_id, chunk_index, len(session["received"]))
            raise ValueError(
                f"Chunk out of order: got {chunk_index}, expected {len(session['received'])}"
            )

        session["received"].add(chunk_index)
        # Append chunk data to the temp file (binary append mode)
        try:
            with open(session["_chunk_file"], "ab") as f:
                f.write(data)
        except OSError as exc:
            logger.error("Chunk append failed for upload %s (chunk %d): %s", upload_id, chunk_index, exc)
            raise ValueError(f"Chunk write failed: {exc}") from exc

        # Byte-based progress: the client never declares a chunk count, so the
        # old ratio of received/total_chunks reported 0% forever.
        total_bytes = int(session.get("total_size") or 0)
        received_bytes = 0
        with contextlib.suppress(OSError):
            received_bytes = os.path.getsize(session["_chunk_file"])
        progress = min(100.0, received_bytes / total_bytes * 100) if total_bytes else 0.0

        return {
            "upload_id": upload_id,
            "progress": round(progress, 1),
            "chunks_received": len(session["received"]),
            "total_chunks": session["total_chunks"],
            "complete": bool(total_bytes) and received_bytes >= total_bytes,
        }

    def complete(self, upload_id: str) -> dict:
        """Finalize a chunked upload — move temp file to destination.

        Returns the final upload result including broadcast event data.
        """
        session = self._chunks.pop(upload_id, None)
        if not session:
            raise ValueError(f"Unknown upload ID: {upload_id}")

        tmp_path = session["_chunk_file"]
        dest_path = session["dest_path"]

        # A dropped connection still reaches complete() upstream: verify the
        # byte count, otherwise a truncated temp file replaced the destination
        # and the API happily reported ok:True.
        expected = int(session.get("total_size") or 0)
        actual = 0
        with contextlib.suppress(OSError):
            actual = os.path.getsize(tmp_path)
        if expected and actual != expected:
            with contextlib.suppress(OSError):
                os.remove(tmp_path)
            raise ValueError(
                f"Upload incomplete: received {actual} of {expected} bytes — please retry")

        # Never overwrite a sibling — auto-suffix like copy/move does.
        dest_path = _unique_dest(os.path.dirname(dest_path), os.path.basename(dest_path))
        session["dest_path"] = dest_path
        session["filename"] = os.path.basename(dest_path)

        try:
            shutil.move(tmp_path, dest_path)
            file_size = os.path.getsize(dest_path)

            # Broadcast event for the destination directory
            broadcast_mutation(
                "upload", session["dest_path"],
                [{"name": session["filename"], "size": file_size}]
            )

            return {
                "ok": True,
                "path": dest_path,
                "filename": session["filename"],
                "size": file_size,
            }
        except Exception as exc:
            logger.error("Chunked upload finalize failed %s → %s: %s", tmp_path, dest_path, exc)
            # Clean up temp file on failure
            with contextlib.suppress(OSError):
                os.remove(tmp_path)
            raise RuntimeError(f"Failed to finalize upload: {exc}") from exc

    def remove(self, upload_id: str) -> None:
        """Remove an in-progress upload (cleanup)."""
        session = self._chunks.pop(upload_id, None)
        if session and os.path.exists(session["_chunk_file"]):
            with contextlib.suppress(OSError):
                os.remove(session["_chunk_file"])


_chunk_store = _ChunkStore()


def create_chunked_upload(file_id: str, dest_dir: str, filename: str,
                          total_size: int = 0) -> dict:
    """Create a new chunked upload session."""
    return _chunk_store.create(file_id, dest_dir, filename, total_size)


def append_chunk(upload_id: str, chunk_index: int, data: bytes) -> dict:
    """Append a chunk to an in-progress upload."""
    return _chunk_store.append_chunk(upload_id, chunk_index, data)


def complete_chunked_upload(upload_id: str) -> dict:
    """Finalize and move the uploaded file to its destination."""
    return _chunk_store.complete(upload_id)


# ── Utility ──────────────────────────────────────────────────────────────────

def get_events_for_dir(dir_path: str) -> list[dict]:
    """Get recent mutation events for a directory (for reactive sync)."""
    return get_recent_events_for(dir_path)


def clear_events() -> None:
    """Clear all stored events."""
    _file_events.clear()
