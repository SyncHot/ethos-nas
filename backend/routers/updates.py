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

``app=extension`` serves the **browser extension** zip instead (``EthOS NAS
Downloader``, built by the separate `ethos-nas-chrome-extensions` repo). There
is no sidecar manifest: the version comes from the
``ethos-nas-downloader-<x.y.z>.zip`` filename and the newest semantic version
wins. Deploy it to ``apks/extension/`` in this checkout, or point
``ETHOS_EXTENSION_ZIP_PATH`` at the file/directory.

Default channel per app (see ``APK_DEFAULTS``): **release** APK when present,
falling back to **debug** (legacy installs were on the debug channel; both
builds share one signing key, so switching channels needs no uninstall).
Override the APK locations with the ``ETHOS_MOMENTS_APK_PATH`` /
``ETHOS_MUSIC_APK_PATH`` / ``ETHOS_VIDEO_APK_PATH`` env vars.
"""

import hashlib
import json
import logging
import re
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


# ── Browser extension zip ────────────────────────────────────────────────────

EXTENSION_APP = "extension"
_EXTENSION_RE = re.compile(r"^ethos-nas-downloader-(\d+\.\d+\.\d+)\.zip$")


def _extension_dirs() -> list[Path]:
    override = str(settings.EXTENSION_ZIP_PATH or "").strip()
    if override:
        path = Path(override)
        return [path] if path.is_dir() else []
    return [Path(folder) for folder in (settings.EXTENSION_DIRS or [])]


def _extension_zip() -> Path | None:
    """Newest ``ethos-nas-downloader-<x.y.z>.zip`` (semver, then mtime)."""
    override = str(settings.EXTENSION_ZIP_PATH or "").strip()
    if override:
        path = Path(override)
        if path.is_file():
            return path

    best: Path | None = None
    best_key = ((-1, -1, -1), -1.0)
    for folder in _extension_dirs():
        try:
            candidates = list(folder.glob("ethos-nas-downloader-*.zip"))
        except OSError:
            logger.warning("Could not scan the extension folder %s", folder, exc_info=True)
            continue
        for path in candidates:
            match = _EXTENSION_RE.match(path.name)
            if not match or not path.is_file():
                continue
            version = tuple(int(part) for part in match.group(1).split("."))
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            key = (version, mtime)
            if key > best_key:
                best_key, best = key, path
    return best


def _extension_version(path: Path | None) -> str:
    if path is None:
        return ""
    match = _EXTENSION_RE.match(path.name)
    return match.group(1) if match else ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        logger.warning("Could not hash the extension package %s", path, exc_info=True)
        return ""
    return digest.hexdigest()


def _extension_info() -> dict:
    package = _extension_zip()
    if package is None:
        logger.warning("Browser extension package not found in %s", settings.EXTENSION_DIRS)
        raise HTTPException(status_code=404, detail="Browser extension package not available")
    try:
        size = package.stat().st_size
    except OSError:
        size = 0
    return {
        "app": EXTENSION_APP,
        "version_code": 0,
        "version_name": _extension_version(package),
        "url": f"/api/app/download?app={EXTENSION_APP}",
        "sha256": _sha256_file(package),
        "size_bytes": size,
        "notes": "",
    }


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
    app: str = Query(default="moments", pattern="^(moments|music|video|extension)$"),
) -> dict:
    """Return the latest APK (or browser-extension zip) version info (public)."""
    _retired(app)
    if app == EXTENSION_APP:
        return _extension_info()
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
    app: str = Query(default="moments", pattern="^(moments|music|video|extension)$"),
) -> FileResponse:
    """Serve the current APK or browser-extension zip (authenticated)."""
    _retired(app)
    if app == EXTENSION_APP:
        package = _extension_zip()
        if package is None:
            logger.warning("Browser extension package not found in %s", settings.EXTENSION_DIRS)
            raise HTTPException(status_code=404, detail="Browser extension package not available")
        return FileResponse(
            path=package,
            media_type="application/zip",
            filename=package.name,
        )
    apk = _apk_path(app)
    if not apk.is_file():
        logger.warning("APK for %r not found at %s", app, apk)
        raise HTTPException(status_code=404, detail="APK not available")
    return FileResponse(
        path=apk,
        media_type="application/vnd.android.package-archive",
        filename=f"ethos-{app}.apk",
    )
