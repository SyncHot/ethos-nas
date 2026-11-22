"""EthOS NAS — App auto-update endpoint (self-hosted, no Play Store)

Serves version info for the Android apps (Photos and Video; **Music is
retired** — its phone client is the core ``/music`` PWA, see ``RETIRED_APPS``).
Each APK is built on this machine and a sidecar manifest
(``<apk>.update.json``) is written next to it by the Gradle build:

    {
      "app": "moments",
      "version_code": 2,
      "version_name": "1.0.1",
      "sha256": "<hex>",
      "size_bytes": 123456,
      "notes": "optional release notes"
    }

The Android app polls ``GET /api/app/version?app=<moments|music|video>`` (public —
version info is not sensitive), compares ``version_code`` with its own, and
downloads the APK via the regular authenticated file download endpoint.

Default channel per app (see ``APK_DEFAULTS``): **release** APK when present,
falling back to **debug** (legacy installs were on the debug channel; both
builds share one signing key, so switching channels needs no uninstall).
Override the APK locations with the ``ETHOS_MOMENTS_APK_PATH`` /
``ETHOS_MUSIC_APK_PATH`` / ``ETHOS_VIDEO_APK_PATH`` env vars.
"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from backend.config import settings

logger = logging.getLogger("ethos-nas.routers.updates")
router = APIRouter()

APK_DEFAULTS = settings.APK_DEFAULTS

# Retired APK channels. ``music`` now ships as the installable ``/music`` PWA,
# so the build pipeline stops publishing it: old installs still poll the public
# endpoint and must be told *why* instead of silently receiving nothing, and a
# stale APK found in a developer checkout must never be served again.
RETIRED_APPS = {
    "music": "Music now runs as the /music web app — open it in the phone browser and install it",
}


def _retired(app: str) -> None:
    """404 for a retired channel, with a pointer at its replacement."""
    detail = RETIRED_APPS.get(app)
    if detail:
        raise HTTPException(status_code=404, detail=detail)


def _manifest_version_code(apk: Path) -> int:
    """``version_code`` z sidecara ``<apk>.update.json`` (0 gdy brak/uszkodzony)."""
    try:
        with open(apk.parent / (apk.name + ".update.json"), encoding="utf-8") as fh:
            return int(json.load(fh).get("version_code", 0))
    except Exception:
        return 0


def _apk_path(app: str) -> Path:
    override = settings.APK_PATHS.get(app)
    if override:
        return Path(override)
    # Najnowszy, a nie „pierwszy istniejący”: sztywne release→debug sprawiało,
    # że stary release przykrywał świeży build i telefony dostawały kod sprzed
    # tygodni (incydent 2026-11-04: release 14.10 vs debug 03.11, oba code 26).
    # Klucz sortowania: (version_code, mtime) — mtime rozstrzyga remisy kodów.
    best = None
    best_key = (-1, -1.0)
    for candidate in APK_DEFAULTS[app]:
        p = Path(candidate)
        if not p.is_file():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        key = (_manifest_version_code(p), mtime)
        if key > best_key:
            best_key = key
            best = p
    if best is not None:
        return best
    return Path(APK_DEFAULTS[app][-1])


@router.get("/version")
async def get_app_version(
    app: str = Query(default="moments", pattern="^(moments|music|video)$"),
) -> dict:
    """Return the latest APK version info for ``app`` (public endpoint)."""
    _retired(app)
    apk = _apk_path(app)
    if not apk.is_file():
        # Never leak the deploy layout to an unauthenticated caller.
        logger.warning("APK for %r not found at %s", app, apk)
        raise HTTPException(status_code=404, detail="APK not available")

    manifest = apk.parent / (apk.name + ".update.json")
    if not manifest.is_file():
        # APK exists but the build didn't write version info — the client
        # can't know whether it's newer, so report no update available.
        logger.warning("Update manifest missing next to %s", apk)
        raise HTTPException(
            status_code=404,
            detail="No update info available (rebuild the APK to generate it)",
        )

    try:
        info = json.loads(manifest.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not parse update manifest %s: %s", apk, exc)
        raise HTTPException(status_code=500, detail="Corrupt update manifest")

    return {
        "app": app,
        "version_code": int(info.get("version_code", 0)),
        "version_name": str(info.get("version_name", "")),
        # App-scoped URL: the absolute path of the deploy layout never leaves
        # the server (the endpoint is public).
        "url": f"/api/app/download?app={app}",
        "sha256": info.get("sha256", ""),
        "size_bytes": int(info.get("size_bytes", apk.stat().st_size)),
        "notes": str(info.get("notes", "")),
    }


@router.get("/download")
async def download_apk(
    app: str = Query(default="moments", pattern="^(moments|music|video)$"),
) -> FileResponse:
    """Serve the current APK for ``app`` (authenticated, no path exposure)."""
    _retired(app)
    apk = _apk_path(app)
    if not apk.is_file():
        logger.warning("APK for %r not found at %s", app, apk)
        raise HTTPException(status_code=404, detail="APK not available")
    return FileResponse(
        path=apk,
        media_type="application/vnd.android.package-archive",
        filename=f"ethos-{app}.apk",
    )
