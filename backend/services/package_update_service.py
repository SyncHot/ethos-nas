"""Background check for package updates (Package Center notifications).

``GET /api/packages/updates`` is passive: it only answers while the Packages
window is open. A NAS owner should hear about a security fix in an installed
package the same way they hear about a core update — from the Notification
Center — so this service refreshes the catalog on a schedule and publishes one
event per package version that is newer than the installed one.

The event carries ``dedupe_key = pkg-update:<id>:<version>``, so the same update
never produces a second row, and a package that is updated (or uninstalled)
simply stops being reported. Every failure stays inside the loop: a repository
that is unreachable must never break startup or shutdown.
"""

import asyncio
import logging

from backend.config import settings
from backend.services import package_repo, packages_store
from backend.services.notifications_service import emit_event
from backend.services.package_schema import PackageError

logger = logging.getLogger("ethos-nas.services.package_update_service")

_loop_task: asyncio.Task | None = None
_loop_stop: asyncio.Event | None = None
_check_lock = asyncio.Lock()


async def check(force: bool = True) -> list[dict]:
    """Refresh the catalog and notify about updates for installed packages."""
    if not [s for s in packages_store.load_sources() if s.get("enabled", True)]:
        return []
    async with _check_lock:
        try:
            catalog = await package_repo.browse(force=force)
        except PackageError as exc:
            logger.warning("Package update check failed: %s", exc.detail)
            return []
        except Exception:
            logger.error("Package update check crashed", exc_info=True)
            return []

        installed = {str(p.get("id")): p for p in packages_store.load_installed()}
        updates: list[dict] = []
        for package in catalog.get("packages") or []:
            pkg_id = str(package.get("id") or "")
            record = installed.get(pkg_id)
            if not record or not package.get("update_available"):
                continue
            updates.append({
                "id": pkg_id,
                "name": str(package.get("name") or pkg_id),
                "from": str(record.get("version") or ""),
                "to": str(package.get("version") or ""),
            })

    for item in updates:
        emit_event(
            "info",
            f"Update available: {item['name']}",
            f"v{item['from']} → v{item['to']}",
            source="packages",
            dedupe_key=f"pkg-update:{item['id']}:{item['to']}",
            action={"label": "Open Packages", "appId": "packages"},
        )
    if updates:
        logger.info("Package update check: %d update(s) available", len(updates))
    else:
        logger.debug("Package update check: everything up to date")
    return updates


async def _loop(stop: asyncio.Event) -> None:
    interval = max(0.25, float(settings.PACKAGES_UPDATE_CHECK_HOURS)) * 3600
    logger.info("Package update checker started (every %.1f h)", interval / 3600)
    while not stop.is_set():
        # Sleep first: a short-lived dev/test instance must never reach the
        # network before the operator has even opened the desktop.
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass  # the timeout is the scheduled wake-up, not a failure
        try:
            await check(force=True)
        except Exception:
            logger.error("Package update checker iteration failed", exc_info=True)
    logger.info("Package update checker stopped")


def start_update_checker() -> None:
    global _loop_task, _loop_stop
    if not settings.PACKAGES_UPDATE_CHECK:
        return
    if _loop_task is not None and not _loop_task.done():
        return
    from backend.services.task_registry import spawn

    _loop_stop = asyncio.Event()
    _loop_task = spawn(_loop(_loop_stop), name="package-update-checker")


async def stop_update_checker() -> None:
    global _loop_task, _loop_stop
    if _loop_stop is not None:
        _loop_stop.set()
    if _loop_task is not None and not _loop_task.done():
        try:
            await asyncio.wait_for(_loop_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.debug("Package update checker did not stop in time", exc_info=True)
    _loop_task = None
    _loop_stop = None
