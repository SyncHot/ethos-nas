"""Memory-pressure guard: measure PSI + available RAM and gate heavy work.

Why this exists: both OOM killers on a stock Ubuntu box pick a *victim cgroup*
or *victim process* — `systemd-oomd` is enabled and kills the whole cgroup when
memory pressure stays high (the user session is explicitly kill-eligible at
50 %), and the kernel OOM killer takes the largest process once swap is
exhausted. EthOS cannot decide those host policies, so it stays out of the
danger zone: a sampler turns "the box is nearly out of memory" into a pause of
heavy background work *before* anything is killed.

Levels (see ``backend/config.py`` for the thresholds):

* ``ok``   — everything runs.
* ``soft`` — logged only; the box is heading into reclaim.
* ``hard`` — offline renders, thumbnails, package installs and download
  workers wait (``heavy_work_allowed()`` / ``wait_until_ok()``) and a
  Notification Center event is emitted when the level changes.

Signals: ``/proc/pressure/memory`` (PSI ``some``/``full`` avg10), MemAvailable
as a share of RAM, and swap usage for the health snapshot.
"""

import asyncio
import logging
import threading
import time
from typing import Optional

import psutil

from backend.config import settings
from backend.services import task_registry
from backend.services.notifications_service import emit_event

logger = logging.getLogger("ethos-nas.services.pressure_guard")

LEVEL_OK = "ok"
LEVEL_SOFT = "soft"
LEVEL_HARD = "hard"

_MIN_REFRESH_SECONDS = 2.0  # a burst of requests must not hammer /proc

_lock = threading.Lock()
_snapshot: dict = {"level": LEVEL_OK, "available_pct": 100.0, "available_bytes": 0,
                   "total_bytes": 0, "used_pct": 0.0, "swap_pct": 0.0,
                   "psi_some": 0.0, "psi_full": 0.0, "at": 0.0}
# Created eagerly (and set): a lazy Event would start life *set* even when the
# first sample is already HARD, so wait_until_ok() would return immediately.
_ok_event: asyncio.Event = asyncio.Event()
_ok_event.set()
_loop: Optional[asyncio.AbstractEventLoop] = None
_task: Optional[asyncio.Task] = None


# ── Signals ─────────────────────────────────────────────────────────────────

def read_psi(path: Optional[str] = None) -> tuple[float, float]:
    """``(some avg10, full avg10)`` from the PSI file; ``(0, 0)`` if absent."""
    target = path or settings.PRESSURE_FILE
    try:
        with open(target, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return 0.0, 0.0
    some = full = 0.0
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        values: dict[str, str] = {}
        for item in parts[1:]:
            name, _, value = item.partition("=")
            values[name] = value
        try:
            if parts[0] == "some":
                some = float(values.get("avg10") or 0.0)
            elif parts[0] == "full":
                full = float(values.get("avg10") or 0.0)
        except ValueError:
            continue
    return some, full


def classify(available_pct: float, psi_some: float, psi_full: float) -> str:
    """Pure rule used by the sampler and by tests."""
    if (available_pct <= settings.PRESSURE_HARD_AVAILABLE_PCT
            or psi_full >= settings.PRESSURE_HARD_PSI_FULL):
        return LEVEL_HARD
    if (available_pct <= settings.PRESSURE_SOFT_AVAILABLE_PCT
            or psi_some >= settings.PRESSURE_SOFT_PSI_SOME):
        return LEVEL_SOFT
    return LEVEL_OK


def _collect() -> dict:
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    total = mem.total or 1
    available_pct = round(mem.available / total * 100, 1)
    psi_some, psi_full = read_psi()
    return {
        "level": classify(available_pct, psi_some, psi_full),
        "available_pct": available_pct,
        "available_bytes": int(mem.available),
        "total_bytes": int(mem.total),
        "used_pct": round(float(mem.percent), 1),
        "swap_pct": round(float(swap.percent), 1),
        "psi_some": psi_some,
        "psi_full": psi_full,
        "at": time.time(),
    }


# ── State transitions ───────────────────────────────────────────────────────

def top_consumers(limit: int = 3) -> list[dict]:
    """Biggest RSS processes — included in alerts so the operator knows whom."""
    rows: list[dict] = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = proc.info
            rss = int((info.get("memory_info") or (None,))[0] or 0)
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, IndexError):
            continue
        if rss > 0:
            rows.append({"pid": info.get("pid"), "name": info.get("name") or "?",
                         "rss": rss})
    rows.sort(key=lambda r: r["rss"], reverse=True)
    return rows[:max(1, int(limit))]


def _notify_transition(previous: str, current: str, snap: dict) -> None:
    if current == previous:
        return
    if current == LEVEL_HARD:
        top = ", ".join(f"{c['name']} {c['rss'] // (1024 * 1024)} MB"
                        for c in top_consumers(3))
        message = (f"Available memory is down to {snap['available_pct']:g}% "
                   f"(swap {snap['swap_pct']:g}%, PSI full {snap['psi_full']:g}). "
                   "Renders, thumbnails, package installs and downloads are paused "
                   "until it recovers.")
        if top:
            message += f" Biggest processes: {top}."
        logger.warning("Memory pressure HARD — heavy work paused: %s", message)
        emit_event("warning", "Low memory — heavy tasks paused", message,
                   source="system", dedupe_key="memory-pressure")
    elif previous == LEVEL_HARD:
        message = (f"Available memory recovered to {snap['available_pct']:g}% — "
                   "heavy tasks resume.")
        logger.info("Memory pressure cleared: %s", message)
        emit_event("info", "Memory pressure cleared", message,
                   source="system", dedupe_key="memory-pressure")
    elif current == LEVEL_SOFT:
        logger.info("Memory pressure soft: available %.1f%%, PSI some %.1f, swap %.1f%%",
                    snap["available_pct"], snap["psi_some"], snap["swap_pct"])


def _signal(ok: bool) -> None:
    """Wake (or block) ``wait_until_ok()``; safe from any thread."""
    event = _ok_event
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if _loop is not None and running is not _loop:
        _loop.call_soon_threadsafe(event.set if ok else event.clear)
        return
    if ok:
        event.set()
    else:
        event.clear()


def refresh(*, force: bool = False) -> dict:
    """Re-read the signals (rate-limited) and publish the current level."""
    global _snapshot
    now = time.time()
    with _lock:
        if (not force and _snapshot["at"]
                and now - _snapshot["at"] < _MIN_REFRESH_SECONDS):
            return dict(_snapshot)
        try:
            snap = _collect()
        except Exception:
            # Never let a health signal break a request path.
            logger.error("Memory-pressure check failed", exc_info=True)
            return dict(_snapshot)
        previous = _snapshot["level"]
        _snapshot = snap
    _signal(snap["level"] != LEVEL_HARD)
    _notify_transition(previous, snap["level"], snap)
    return dict(snap)


# ── Public API ──────────────────────────────────────────────────────────────

def heavy_work_allowed() -> bool:
    """False while the box is under hard memory pressure."""
    return refresh()["level"] != LEVEL_HARD


def wait_seconds_left() -> float:
    return float(settings.PRESSURE_WAIT_SECONDS)


async def wait_until_ok(timeout: Optional[float] = None) -> bool:
    """Wait for hard pressure to clear; False if it did not within `timeout`."""
    if heavy_work_allowed():
        return True
    event = _event()
    limit = float(timeout if timeout is not None else settings.PRESSURE_WAIT_SECONDS)
    try:
        await asyncio.wait_for(event.wait(), timeout=max(0.1, limit))
    except (asyncio.TimeoutError, TimeoutError):
        return heavy_work_allowed()
    return heavy_work_allowed()


def snapshot() -> dict:
    snap = refresh()
    return {
        "level": snap["level"],
        "available_pct": snap["available_pct"],
        "available_bytes": snap["available_bytes"],
        "total_bytes": snap["total_bytes"],
        "used_pct": snap["used_pct"],
        "swap_pct": snap["swap_pct"],
        "psi_some": snap["psi_some"],
        "psi_full": snap["psi_full"],
        "at": snap["at"],
    }


def _event() -> asyncio.Event:
    return _ok_event


# ── Lifespan loop ───────────────────────────────────────────────────────────

async def _loop_body() -> None:
    while True:
        try:
            refresh(force=True)
        except Exception:  # pragma: no cover - refresh() already guards
            logger.error("Memory-pressure loop iteration failed", exc_info=True)
        await asyncio.sleep(max(5.0, float(settings.PRESSURE_CHECK_INTERVAL)))


async def start() -> None:
    """Start the sampler and remember the running loop for thread-safe wakes."""
    global _loop, _task
    _loop = asyncio.get_running_loop()
    _event()
    if _task is None or _task.done():
        _task = task_registry.spawn(_loop_body(), name="pressure-guard")
    logger.info("Memory-pressure guard started (interval=%ss)", settings.PRESSURE_CHECK_INTERVAL)


async def stop() -> None:
    global _task
    task = _task
    _task = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass  # cancelled on purpose during teardown — nothing to report


def reset() -> None:
    """Tests only: forget the snapshot so the next refresh re-reads the box."""
    global _snapshot
    with _lock:
        _snapshot = {"level": LEVEL_OK, "available_pct": 100.0, "available_bytes": 0,
                     "total_bytes": 0, "used_pct": 0.0, "swap_pct": 0.0,
                     "psi_some": 0.0, "psi_full": 0.0, "at": 0.0}
    _ok_event.set()
