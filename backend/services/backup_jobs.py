"""Single-flight queue for backup/restore/verify operations.

Only one operation touches disks at a time. Additional requests are queued
(bounded) and the UI sees both the running op and the queue, so a scheduled run
that fires while a manual one is active is no longer dropped.

`task_registry.spawn` keeps the worker visible to shutdown; cancellation
terminates the child process through the `OperationHandle`.
"""

import asyncio
import logging
from collections import deque
from typing import Optional

from backend.services import backup_engine, backup_store, task_registry
from backend.services.backup_proc import OperationHandle
from backend.services.notifications_service import emit_event

logger = logging.getLogger("ethos-nas.services.backup_jobs")

MAX_QUEUE = 20
_queue: deque[dict] = deque()
_current: Optional[OperationHandle] = None
_current_meta: dict = {}
_worker: Optional[asyncio.Task] = None


class QueueFull(RuntimeError):
    """Raised when too many operations are waiting."""


def is_busy() -> bool:
    return _current is not None or bool(_queue)


def duplicate(schedule_id: str, kind: str) -> bool:
    if _current is not None and _current.schedule_id == schedule_id and _current.kind == kind:
        return True
    return any(entry["op"].schedule_id == schedule_id and entry["op"].kind == kind
               for entry in _queue)


def submit(schedule: dict, kind: str, trigger: str, **kwargs) -> OperationHandle:
    if len(_queue) >= MAX_QUEUE:
        raise QueueFull("Too many backup operations are queued")
    op = OperationHandle(backup_store.new_id(), kind, schedule, trigger)
    _queue.append({"op": op, "schedule": dict(schedule), "kwargs": kwargs})
    logger.info("Backup queue: %s %s for %s (op %s, %d waiting)",
                kind, trigger, schedule.get("name"), op.op_id, len(_queue))
    _ensure_worker()
    return op


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and not _worker.done():
        return
    _worker = task_registry.spawn(_worker_loop(), name="backup-worker")


def current_op() -> Optional[OperationHandle]:
    return _current


def status() -> dict:
    return {
        "running": _current is not None,
        "op": _current.snapshot() if _current is not None else None,
        "queue": [{
            "op_id": entry["op"].op_id,
            "kind": entry["op"].kind,
            "schedule_id": entry["op"].schedule_id,
            "schedule_name": entry["op"].schedule_name,
            "trigger": entry["op"].trigger,
            "queued_at": entry["op"].started_at,
        } for entry in _queue],
    }


def cancel(op_id: Optional[str] = None) -> bool:
    """Cancel the running op or a queued one. Returns True when something matched."""
    if _current is not None and (op_id is None or _current.op_id == op_id):
        _current.cancel()
        logger.info("Backup queue: cancel requested for op %s", _current.op_id)
        return True
    for entry in list(_queue):
        if op_id is None or entry["op"].op_id == op_id:
            entry["op"].cancel()
            _queue.remove(entry)
            logger.info("Backup queue: removed queued op %s", entry["op"].op_id)
            return True
    return False


async def _worker_loop() -> None:
    global _current, _current_meta
    while _queue:
        entry = _queue.popleft()
        op: OperationHandle = entry["op"]
        schedule: dict = entry["schedule"]
        kwargs: dict = entry["kwargs"]
        if op.cancel_requested:
            _record(op, schedule, {"status": "cancelled", "error": "Cancelled before it started"})
            continue
        _current = op
        _current_meta = {"schedule": schedule, "kwargs": kwargs}
        op.status = "running"
        result: dict = {"status": "failed", "error": "unknown error"}
        try:
            if op.kind == "backup":
                result = await backup_engine.run_backup(schedule, op)
            elif op.kind == "restore":
                result = await backup_engine.run_restore(
                    schedule, kwargs.get("generation"), mode=kwargs.get("mode", "in_place"),
                    target=kwargs.get("target"), delete_extra=bool(kwargs.get("delete_extra")),
                    op=op)
            elif op.kind == "verify":
                result = await backup_engine.verify_generation(
                    schedule, kwargs.get("generation") or "", deep=bool(kwargs.get("deep")),
                    op=op)
            elif op.kind == "dry-run":
                result = await backup_engine.run_dry_run(schedule, op)
            else:
                result = {"status": "failed", "error": f"Unknown operation kind: {op.kind}"}
        except asyncio.CancelledError:
            result = {"status": "cancelled", "error": "Cancelled during service shutdown"}
            _record(op, schedule, result)
            _current = None
            raise
        except Exception as exc:
            logger.error("Backup op %s crashed", op.op_id, exc_info=True)
            result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if _current is op:
                _record(op, schedule, result)
                _current = None
                _current_meta = {}


def _record(op: OperationHandle, schedule: dict, result: dict) -> None:
    status = str(result.get("status") or "failed")
    error = str(result.get("error") or "")
    op.status = status
    op.error = error[:300]
    op.generation = result.get("generation") or op.generation

    remote = result.get("remote") or {}
    entry = {
        "id": backup_store.new_id(),
        "op_id": op.op_id,
        "schedule_id": schedule.get("id"),
        "schedule_name": schedule.get("name", ""),
        "kind": op.kind,
        "type": schedule.get("type"),
        "status": status,
        "trigger": op.trigger,
        "generation": result.get("generation"),
        "source": schedule.get("source", ""),
        "destination": schedule.get("destination", ""),
        "started_at": op.started_at,
        "finished_at": backup_store.now_iso(),
        "duration_seconds": result.get("duration_seconds"),
        "bytes_total": result.get("bytes_total", 0),
        "bytes_copied": result.get("bytes_copied", 0),
        "files_total": result.get("files_total", 0),
        "error": error[:500],
        "remote_status": remote.get("status", ""),
        "verify": result.get("verified") or {},
        "changes": result.get("changes"),
    }
    try:
        backup_store.record_history(entry)
    except Exception:
        logger.error("Could not record backup history for %s", op.op_id, exc_info=True)

    if op.kind == "backup":
        try:
            backup_store.patch_schedule(schedule.get("id") or "", {
                "last_run_at": entry["finished_at"],
                "last_status": status,
                "last_generation": entry["generation"],
                "last_error": error[:300],
            })
        except Exception:
            logger.error("Could not update schedule %s after a run", schedule.get("id"), exc_info=True)

    _notify(op, schedule, status, error, entry)


def _notify(op: OperationHandle, schedule: dict, status: str, error: str, entry: dict) -> None:
    name = schedule.get("name") or "Backup"
    detail = f'{schedule.get("source", "")} → {schedule.get("destination", "")}'
    verb = {"restore": "Restore", "verify": "Backup verify", "dry-run": "Backup dry run"}.get(
        op.kind, "Backup")
    try:
        if status == "skipped":
            emit_event("warning", f"{verb} skipped",
                       f'"{name}" — {error or detail}', source="backup")
        elif status == "cancelled":
            emit_event("info", f"{verb} cancelled", f'"{name}" — {detail}', source="backup")
        elif status == "success":
            suffix = f" · {entry.get('bytes_total', 0) // (1024 ** 2)} MB"
            if op.kind == "verify":
                diffs = (entry.get("verify") or {}).get("diffs")
                suffix = " · verified" if not diffs else f" · {diffs} difference(s)"
            emit_event("success", f"{verb} completed", f'"{name}"{suffix}', source="backup")
        else:
            emit_event("error", f"{verb} failed",
                       f'"{name}" — {detail}. {error}'.strip(), source="backup")
    except Exception:
        logger.warning("Backup %s: could not emit notification", op.op_id, exc_info=True)
