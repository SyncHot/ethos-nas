"""Backup scheduler — cron firing, missed-run catch-up and the weekly digest.

The v1 loop dropped a scheduled minute whenever another operation was running.
Now every due schedule is submitted to the bounded queue; `last_scheduled_at` is
persisted so a restart cannot fire the same minute twice, and a run missed while
the service was down is queued once after startup (24 h window).
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from backend.services import backup_jobs, backup_store, cron_utils
from backend.services.notifications_service import emit_event

logger = logging.getLogger("ethos-nas.services.backup_scheduler")

_scheduler_task: Optional[asyncio.Task] = None
_first_tick = True
CATCH_UP_WINDOW_HOURS = 24


async def _tick() -> None:
    global _first_tick
    now = datetime.now().astimezone()
    minute_key = now.strftime("%Y-%m-%dT%H:%M")
    schedules = await asyncio.to_thread(backup_store.load_schedules)

    for schedule in schedules:
        if schedule.get("enabled") is False:
            continue
        schedule_id = str(schedule.get("id") or "")
        cron = str(schedule.get("cron") or "").strip()
        if not schedule_id or not cron:
            continue

        due = False
        trigger = "schedule"
        try:
            if _first_tick:
                due = _missed_since(schedule, now)
                if due:
                    trigger = "catch-up"
            if not due and cron_utils.matches(cron, now):
                due = True
        except Exception:
            logger.warning("Backup schedule %s has an invalid cron expression %r",
                           schedule_id, cron, exc_info=True)
            continue
        if not due:
            continue
        if schedule.get("last_scheduled_at") == minute_key:
            continue

        try:
            backup_store.patch_schedule(schedule_id, {"last_scheduled_at": minute_key})
        except Exception:
            logger.error("Could not persist last_scheduled_at for %s", schedule_id, exc_info=True)
        if backup_jobs.duplicate(schedule_id, "backup"):
            logger.info("Backup schedule %s already running/queued — skipping this tick", schedule_id)
            continue
        try:
            backup_jobs.submit(schedule, "backup", trigger)
            logger.info("Backup scheduler fired %r (%s, %s)", schedule.get("name"), cron, trigger)
        except backup_jobs.QueueFull:
            logger.warning("Backup scheduler queue is full — %r not queued", schedule.get("name"))

    _first_tick = False
    await asyncio.to_thread(_maybe_send_digest, now)


def _missed_since(schedule: dict, now: datetime) -> bool:
    """True when a fire time passed since the persisted last scheduled minute."""
    last = schedule.get("last_scheduled_at")
    cron = str(schedule.get("cron") or "")
    if not last or not cron:
        return False
    try:
        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return False
    if last_dt.tzinfo is None:
        last_dt = last_dt.astimezone()
    window_start = max(last_dt, now - timedelta(hours=CATCH_UP_WINDOW_HOURS))
    cursor = window_start.replace(second=0, microsecond=0)
    while cursor <= now:
        if cron_utils.matches(cron, cursor):
            return True
        cursor += timedelta(minutes=1)
    return False


def _maybe_send_digest(now: datetime) -> None:
    settings = backup_store.get_settings()
    if not settings.get("digest_enabled", True):
        return
    weekday = int(settings.get("digest_weekday") or 0)
    hour = int(settings.get("digest_hour") or 8)
    if now.weekday() != weekday or now.hour < hour:
        return
    today = now.strftime("%Y-%m-%d")
    if settings.get("last_digest_date") == today:
        return

    cutoff = now - timedelta(days=7)
    runs = [entry for entry in backup_store.load_history()
            if _recent(entry.get("finished_at"), cutoff)]
    success = sum(1 for run in runs if run.get("status") == "success")
    failed = sum(1 for run in runs if run.get("status") == "failed")
    skipped = sum(1 for run in runs if run.get("status") == "skipped")
    cancelled = sum(1 for run in runs if run.get("status") == "cancelled")
    total_bytes = sum(int(run.get("bytes_total") or 0) for run in runs
                      if run.get("kind") == "backup")
    schedules = backup_store.load_schedules()
    stale = [s.get("name") or s.get("id") for s in schedules
             if s.get("enabled") is not False and not _recent(s.get("last_run_at"), cutoff)]

    parts = [f"{success} succeeded", f"{failed} failed", f"{skipped} skipped",
             f"{cancelled} cancelled", f"{total_bytes // (1024 ** 3)} GB protected"]
    if stale:
        parts.append("no recent run: " + ", ".join(str(name) for name in stale[:5]))
    try:
        emit_event("warning" if failed or skipped or stale else "success",
                   "Weekly backup report", " · ".join(parts), source="backup")
        backup_store.save_settings({"last_digest_date": today})
        logger.info("Weekly backup digest sent: %s", " · ".join(parts))
    except Exception:
        logger.error("Could not send the weekly backup digest", exc_info=True)


def _recent(value, cutoff: datetime) -> bool:
    if not value:
        return False
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.astimezone()
    return when >= cutoff


async def _loop() -> None:
    while True:
        try:
            await asyncio.sleep(20)
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Backup scheduler tick failed", exc_info=True)


def start_backup_scheduler() -> None:
    """Start the schedule runner (idempotent). Called from the app lifespan."""
    global _scheduler_task
    if _scheduler_task is not None and not _scheduler_task.done():
        return
    _scheduler_task = asyncio.create_task(_loop(), name="backup-scheduler")


async def stop_backup_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is None:
        return
    _scheduler_task.cancel()
    try:
        await _scheduler_task
    except (asyncio.CancelledError, Exception):
        pass  # intentional: a scheduler failure must not break shutdown
    _scheduler_task = None
