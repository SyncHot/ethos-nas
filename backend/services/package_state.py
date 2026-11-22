"""Core-owned state of a package.

Some published packages are only the window: the engine lives in EthOS (the
Cameras / `ethos-surveillance` package fronts `camera_service` +
`camera_runner`, the recording engine "stays in EthOS so it keeps running
headless"). Their state therefore never lands in `<DATA_DIR>/packages/data/<id>`,
so an uninstall left the cameras configured: the core kept restarting ffmpeg
for a package that no longer existed and logged `Connection refused` every
backoff forever.

`CORE_STATE` is an allowlist — never a path taken from a manifest or a request.
A package id maps to functions that describe and delete the state EthOS itself
keeps for it, so `remove_data=1` (and the orphan-data purge) can drop the
configuration along with the package.
"""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from backend.services import packages_store

logger = logging.getLogger("ethos-nas.services.package_state")


@dataclass(frozen=True)
class CoreState:
    """Core state owned by one package id."""

    label: str
    paths: Callable[[], list[Path]]
    stop: Optional[Callable[[], None]] = None


# ── Cameras (ethos-surveillance) ────────────────────────────────────────────

def _surveillance_paths() -> list[Path]:
    """Every config/transient path the Cameras app keeps under DATA_DIR.

    Recordings are deliberately absent: they are the operator's footage and may
    live on an external volume (`surveillance_settings.storage_dir`), so only
    the configuration, motion state and the transient live window go.
    """
    from backend.config import settings
    from backend.services import camera_recordings, camera_service, camera_settings

    surveillance = settings.DATA_DIR / "surveillance"
    paths = [
        camera_service.CAMERAS_PATH,          # cameras + encrypted credentials
        camera_settings.STORE_PATH,           # storage/quota/notification settings
        camera_service.EVENTS_PATH,           # motion events
        camera_recordings.EXPORTS_PATH,       # export index (the MP4s stay too)
        surveillance / "snaps",
        surveillance / "events",
        surveillance / "clips",
        surveillance / "exports",
    ]
    storage = camera_settings.storage_dir()
    live = (storage or surveillance) / "live"
    if live not in paths:
        paths.append(live)
    return paths


def _stop_cameras() -> None:
    """Stop every camera pipeline before its configuration disappears."""
    from backend.services import camera_runner

    camera_runner.stop_all()


CORE_STATE: dict[str, CoreState] = {
    "ethos-surveillance": CoreState(
        label="camera configuration, motion events, snapshots and the live window "
              "(recordings are kept)",
        paths=_surveillance_paths,
        stop=_stop_cameras,
    ),
}


# ── Public API ──────────────────────────────────────────────────────────────

def known_packages() -> tuple[str, ...]:
    """Ids whose core state this module knows how to describe and delete."""
    return tuple(CORE_STATE)


def _measure(paths: list[Path]) -> tuple[list[Path], int, int]:
    """(existing paths, total bytes, newest mtime) — never raises."""
    existing: list[Path] = []
    total = 0
    newest = 0
    for path in paths:
        try:
            info = path.stat()
        except OSError:
            continue  # missing is the normal "nothing to delete" case
        existing.append(path)
        newest = max(newest, int(info.st_mtime))
        total += packages_store.dir_size(path) if path.is_dir() else info.st_size
    return existing, total, newest


def describe(pkg_id: str) -> Optional[dict]:
    """Core state left for `pkg_id`, or None when the id is not in the allowlist.

    Shape matches the app-data inventory: `{path, exists, size_bytes}` plus
    `paths` (one per store) and a human `label` for the uninstall dialog.
    """
    spec = CORE_STATE.get(pkg_id)
    if spec is None:
        return None
    try:
        configured = spec.paths()
    except Exception:  # noqa: BLE001 — a broken config must not break the preview
        logger.error("Could not resolve core state paths for %s", pkg_id, exc_info=True)
        return None
    existing, size, modified = _measure(configured)
    return {
        "path": ", ".join(str(path) for path in (existing or configured)),
        "paths": [str(path) for path in existing],
        "exists": bool(existing),
        "size_bytes": size,
        "modified": modified,
        "label": spec.label,
    }


def purge(pkg_id: str) -> Optional[dict]:
    """Delete the core state of `pkg_id`. Returns None when nothing is known.

    The engines are stopped first so no process recreates a file after the
    sweep. Errors are collected instead of raised: an uninstall must finish,
    and the caller reports what stayed on disk.
    """
    spec = CORE_STATE.get(pkg_id)
    if spec is None:
        return None
    if spec.stop is not None:
        try:
            spec.stop()
        except Exception:  # noqa: BLE001 — the delete below still has to run
            logger.error("Could not stop %s before deleting its state", pkg_id, exc_info=True)
    freed = 0
    errors: list[str] = []
    try:
        paths = spec.paths()
    except Exception:  # noqa: BLE001
        logger.error("Could not resolve core state paths for %s", pkg_id, exc_info=True)
        return {"size_bytes": 0, "errors": ["could not resolve the state paths"],
                "label": spec.label}
    for path in paths:
        try:
            if not path.exists():
                continue
            if path.is_dir():
                size = packages_store.dir_size(path)
                shutil.rmtree(path, ignore_errors=True)
                if path.exists():
                    errors.append(str(path))
                else:
                    freed += size
            else:
                size = path.stat().st_size
                path.unlink()
                freed += size
        except OSError:
            logger.warning("Could not delete core state path %s", path, exc_info=True)
            errors.append(str(path))
    logger.info("Deleted core state for %s (%d bytes, %d path(s) kept)",
                pkg_id, freed, len(errors))
    return {"size_bytes": freed, "errors": errors, "label": spec.label}
