"""Container-health monitor for compose packages.

`docker compose ps` knows whether a container is healthy, restarting or dead; the
Packages window only sees the project status, so a container in a restart loop
still looked “running”. This loop samples every installed compose package, keeps
the last observed value per container and publishes **one** Notification Center
event when a container turns unhealthy (and one when it recovers) — the
`dedupe_key` collapses repeats, so a persisting condition does not spam.

State lives in `<DATA_DIR>/packages/health.json` through the versioned
`json_store`, so a core restart does not re-alert a condition the operator has
already seen. Every failure stays inside the loop: Docker being down must never
break startup, shutdown or the Packages window.
"""

import asyncio
import logging
import time
from typing import Optional

from backend.config import settings
from backend.services import json_store, package_installer, packages_store
from backend.services.notifications_service import emit_event

logger = logging.getLogger("ethos-nas.services.package_health_service")

HEALTH_FILE = settings.DATA_DIR / "packages" / "health.json"
_STORE = json_store.get(HEALTH_FILE, dict, version=1)

# A container's own health state and the crash-loop state are the two signals
# worth an alert; an `exited` one-shot helper is normal.
_BAD_STATES = {"restarting"}

_loop_task: asyncio.Task | None = None
_loop_stop: asyncio.Event | None = None


def _load_state() -> dict:
    try:
        data = _STORE.load()
    except Exception:
        logger.error("Cannot read the package health store", exc_info=True)
        return {}
    containers = data.get("containers") if isinstance(data, dict) else None
    return containers if isinstance(containers, dict) else {}


def _save_state(containers: dict) -> None:
    # Bounded: a NAS runs a handful of packages, but never let a bug grow the file.
    items = list(containers.items())[:200]

    def _apply(data) -> None:
        if not isinstance(data, dict):
            raise TypeError("package health store must be a JSON object")
        data["containers"] = dict(items)

    try:
        _STORE.update(_apply)
    except Exception:
        logger.error("Cannot save the package health store", exc_info=True)


def _problem(container: dict) -> Optional[str]:
    health = str(container.get("health") or "").lower()
    state = str(container.get("state") or "").lower()
    if health == "unhealthy":
        return "unhealthy"
    if state in _BAD_STATES:
        return state
    return None


def _container_key(pkg_id: str, container: dict) -> str:
    label = str(container.get("name") or container.get("service") or "container")
    return f"{pkg_id}/{label}"


async def check_once() -> dict:
    """Sample every installed compose package; publish one event per transition."""
    previous = _load_state()
    installed = packages_store.load_installed()
    installed_ids = {str(p.get("id")) for p in installed if p.get("id")}
    observed: dict[str, str] = {}

    for record in installed:
        pkg_id = str(record.get("id") or "")
        if not pkg_id or str(record.get("runtime") or "compose") != "compose":
            continue
        name = str(record.get("name") or pkg_id)
        containers = await package_installer.compose_ps(pkg_id)
        for container in containers:
            key = _container_key(pkg_id, container)
            problem = _problem(container)
            label = key.split("/", 1)[1]
            earlier = previous.get(key)
            if problem and earlier != problem:
                emit_event(
                    "warning",
                    f"{name}: container {label} is {problem}",
                    "Docker reports a failing container. Open Packages for the container list and "
                    "the package log.",
                    source="packages",
                    dedupe_key=f"pkg-health:{key}",
                    action={"label": "Open Packages", "appId": "packages"},
                )
                logger.warning("Package %s container %s is %s", pkg_id, label, problem)
            elif not problem and earlier and earlier != "ok":
                emit_event(
                    "info",
                    f"{name}: container {label} is healthy again",
                    f"Previously reported as {earlier}.",
                    source="packages",
                    dedupe_key=f"pkg-health-ok:{key}:{int(time.time())}",
                )
                logger.info("Package %s container %s recovered", pkg_id, label)
            observed[key] = problem or "ok"

    # Forget containers of uninstalled packages, keep entries we could not probe
    # this round (a Docker hiccup must not re-alert an unchanged condition).
    state = {key: value for key, value in previous.items()
             if key.split("/", 1)[0] in installed_ids}
    state.update(observed)
    if state != previous:
        _save_state(state)
    return state


async def _loop(stop: asyncio.Event) -> None:
    interval = max(30, int(settings.PACKAGES_HEALTH_INTERVAL_SECONDS))
    logger.info("Package health monitor started (every %ds)", interval)
    while not stop.is_set():
        # Sleep first: a short-lived dev/test instance must not probe Docker.
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass  # the timeout is the scheduled wake-up, not a failure
        try:
            await check_once()
        except Exception:
            logger.error("Package health check iteration failed", exc_info=True)
    logger.info("Package health monitor stopped")


def start_health_monitor() -> None:
    global _loop_task, _loop_stop
    if not settings.PACKAGES_HEALTH_CHECK:
        return
    if _loop_task is not None and not _loop_task.done():
        return
    from backend.services.task_registry import spawn

    _loop_stop = asyncio.Event()
    _loop_task = spawn(_loop(_loop_stop), name="package-health-monitor")


async def stop_health_monitor() -> None:
    global _loop_task, _loop_stop
    if _loop_stop is not None:
        _loop_stop.set()
    if _loop_task is not None and not _loop_task.done():
        try:
            await asyncio.wait_for(_loop_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.debug("Package health monitor did not stop in time", exc_info=True)
    _loop_task = None
    _loop_stop = None
