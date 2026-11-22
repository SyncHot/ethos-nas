"""Downloads — queue and retry scheduling helpers (TODO.md P2-01).

The pump itself stays in the router (it owns the task store, worker pool,
provider credentials and notifications). These two decisions are pure and were
inline:

- in which order waiting tasks may start (priority, then FIFO);
- when the retry timer should fire next.
"""

from typing import Iterable, Optional


def queue_order(tasks: Iterable[dict], status: str = "waiting") -> list:
    """Tasks in start order: higher ``priority`` first, then FIFO by creation."""
    candidates = [task for task in tasks if task.get("status") == status]
    return sorted(
        candidates,
        key=lambda task: (-int(task.get("priority") or 0), task.get("created_at", 0)),
    )


def next_retry_delay(tasks: Iterable[dict], now: float, minimum: float = 1.0) -> Optional[float]:
    """Seconds until the earliest waiting retry is due.

    ``None`` when nothing is waiting in the future (the pump is then driven by
    the window timer or an event, not by a retry timer). Due/backoff-less tasks
    are ignored — those are the pump's job right now.
    """
    pending = [
        float(task.get("_next_retry_at") or 0)
        for task in tasks
        if task.get("status") == "waiting" and float(task.get("_next_retry_at") or 0) > now
    ]
    if not pending:
        return None
    return max(minimum, min(pending) - now)
