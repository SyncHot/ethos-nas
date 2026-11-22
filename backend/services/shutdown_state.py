"""Process-wide shutdown flag shared by background services and stream endpoints.

systemd stops the unit with SIGTERM and kills the cgroup after ``TimeoutStopSec``.
uvicorn only runs the ASGI lifespan teardown *after* every open connection has
drained, so long-lived responses (music proxy streams, camera MJPEG live view,
transcoded video) can delay both the teardown and the whole restart. On
2026-11-22 that turned a core-update restart into a 90 s SIGKILL sweep: a
stream kept ``Waiting for connections to close`` alive, the package supervisor
respawned services that systemd had just signalled, and the render queue started
a fresh ffmpeg mid-shutdown (see ``docs/updates.md``).

The lifespan sets this flag as its first teardown step. Supervisors stop
respawn processes, the render queue refuses/aborts work and the streaming
generators end their body promptly once it is set.

Deliberately a plain module-level flag (not a service class): it must be
reachable from routers, services and tests without wiring, and it is only ever
set once per process.
"""

import asyncio

_event: asyncio.Event | None = None


def _ensure() -> asyncio.Event:
    global _event
    if _event is None:
        _event = asyncio.Event()
    return _event


def request() -> None:
    """Mark the process as shutting down (idempotent)."""
    _ensure().set()


def is_set() -> bool:
    """True once the lifespan teardown has begun."""
    return _event is not None and _event.is_set()


def reset() -> None:
    """Clear the flag — tests only; a real process never comes back."""
    _ensure().clear()
