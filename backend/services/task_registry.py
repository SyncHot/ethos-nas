"""Tracked background tasks (TODO.md P1-06).

Fire-and-forget `asyncio.create_task(...)` calls are invisible: nothing cancels
them at shutdown, nothing logs their exceptions, and the GC may even drop the
reference mid-flight (which happened to the file-event broadcaster).

`spawn()` keeps every task in a process-wide set, logs unhandled exceptions and
is drained by `shutdown()` from the app lifespan. Tasks that are awaited by the
caller can keep using `asyncio.create_task` — this is for the detached ones.
"""

import asyncio
import logging
from typing import Any, Coroutine

logger = logging.getLogger("ethos-nas.services.task_registry")

_tasks: set[asyncio.Task] = set()


def _on_done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background task %r failed: %s", task.get_name(), exc,
                     exc_info=(type(exc), exc, exc.__traceback__))


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task | None:
    """Run `coro` detached, tracked, and with exceptions logged.

    Returns the task, or None when there is no running loop (e.g. called from a
    worker thread) — the coroutine is closed to avoid a "never awaited" warning.
    """
    try:
        task = asyncio.create_task(coro, name=name)
    except RuntimeError:
        logger.debug("No running loop for background task %s — dropping", name)
        coro.close()
        return None
    _tasks.add(task)
    task.add_done_callback(_on_done)
    return task


def spawn_on_loop(loop: asyncio.AbstractEventLoop, coro_factory, *, name: str | None = None) -> None:
    """Schedule `coro_factory()` on `loop` from any thread (thread-safe)."""
    try:
        loop.call_soon_threadsafe(lambda: spawn(coro_factory(), name=name))
    except RuntimeError:
        logger.debug("Loop closed; dropping background task %s", name)


def pending() -> int:
    return sum(1 for t in _tasks if not t.done())


async def shutdown(timeout: float = 10.0) -> int:
    """Cancel and await all tracked tasks. Returns how many were cancelled."""
    tasks = [t for t in list(_tasks) if not t.done()]
    if not tasks:
        return 0
    for task in tasks:
        task.cancel()
    done, still_pending = await asyncio.wait(tasks, timeout=timeout)
    if still_pending:
        logger.warning("Background tasks still running after %.1fs: %d",
                       timeout, len(still_pending))
    logger.info("Cancelled %d background task(s) on shutdown", len(tasks))
    return len(tasks)
