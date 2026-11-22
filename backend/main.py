"""EthOS NAS — FastAPI Application Entry Point

Mounts all routers and serves the frontend SPA.
"""

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import APP_VERSION, DEFAULT_JWT_SECRET, settings
from backend.routers.system import router as system_router, stats_ws_router
from backend.routers.system_update import router as system_update_router
from backend.routers.system_os_update import router as system_os_update_router
from backend.routers.processes import router as processes_router
from backend.routers.logs import router as logs_router
from backend.routers.logs_ws import (
    router as logs_ws_router,
    setup_broadcaster as _setup_log_broadcaster,
    teardown_broadcaster as _teardown_log_broadcaster,
)
from backend.routers.docker import router as docker_router
from backend.routers.docker_logs import router as docker_logs_router
from backend.routers.storage import router as storage_router
from backend.routers.network import router as network_router
from backend.routers.files import router as files_router
from backend.routers.terminal import router as terminal_router
from backend.routers.auth import router as auth_router
from backend.routers.users import router as users_router
from backend.routers.setup import router as setup_router
from backend.routers.backup import router as backup_router
from backend.services.backup_scheduler import (
    start_backup_scheduler,
    stop_backup_scheduler,
)
from backend.routers.shares import router as shares_router
from backend.routers.packages import router as packages_router
from backend.routers.gallery import router as gallery_router
from backend.routers.moments import router as moments_router
from backend.routers.downloads import (
    router as downloads_router,
    start_download_engine,
    start_watch_folder,
    stop_download_engine,
    stop_watch_folder,
)
from backend.routers.file_ops import router as file_ops_router
from backend.routers.file_events_ws import _setup_event_broadcaster, router as file_events_router
from backend.routers.access_control import router as access_control_router
from backend.routers.smart import router as smart_router
from backend.routers.systemd import router as systemd_router
from backend.routers.cron import router as cron_router
from backend.routers.music import router as music_router
from backend.routers.video_station import router as video_station_router
from backend.routers.ethos_service import router as ethos_service_router
from backend.routers.sync import router as sync_router
from backend.routers.updates import router as updates_router
from backend.routers.disk_clean import router as disk_clean_router
from backend.routers.notifications import router as notifications_router
from backend.routers.notifications_ws import (
    router as notifications_ws_router,
    setup_broadcaster as _setup_notification_broadcaster,
)
from backend.routers.health import router as health_router
from backend.routers.surveillance import router as surveillance_router
from backend.routers.share_links import router as share_links_router
from backend.routers.editor import router as editor_router
from backend.routers.appearance import router as appearance_router
from backend.services.qb_compat import router as qb_compat_router, v2_router as qb_compat_v2_router
from backend.services.health_monitor_service import (
    start_health_monitor,
    stop_health_monitor,
)
from backend.services.net_stats_service import (
    start_net_stats_sampler,
    stop_net_stats_sampler,
)
from backend.services.retention_service import (
    start_retention_sweeper,
    stop_retention_sweeper,
)
from backend.services.autoban_service import (
    start_autoban,
    stop_autoban,
)

logging.basicConfig(
    level=(
        logging.DEBUG if settings.DEBUG
        else getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    ),
    format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ethos-nas")

# Diagnostics: one unified store (docs/logs.md) — in-process records, browser
# errors and native-package messages. The legacy app_events.jsonl is imported
# once and archived as logs/legacy-app-events.jsonl.
from backend.services.log_service import (  # noqa: E402
    init as _applog_init,
    attach_to_logging as _applog_attach,
)
_applog_init(
    settings.DATA_DIR / "logs",
    legacy_path=settings.DATA_DIR / "app_events.jsonl",
    ring_size=settings.LOG_RING_SIZE,
    max_bytes=settings.LOG_MAX_BYTES,
    file_count=settings.LOG_FILES,
    dedupe_window=settings.LOG_DEDUPE_WINDOW,
)
_applog_attach()

BASE_DIR = Path(__file__).parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"


def _warn_insecure_config() -> None:
    """Refuse to start when no JWT secret could be established.

    `backend.config._bootstrap_jwt_secret()` generates and persists a random
    secret when none is configured, so this guard only fires when that failed
    (unwritable data dir) or when the operator explicitly opted into the
    placeholder with `ETHOS_ALLOW_INSECURE=1` (throwaway boxes only).

    A hard failure is the safe default here: running with the placeholder means
    every issued token is forgeable by anyone who reads the source.
    """
    if settings.DEBUG or settings.JWT_SECRET != DEFAULT_JWT_SECRET:
        return
    msg = ("SECURITY: ETHOS_JWT_SECRET is unset (built-in default) — every "
           "issued token would be forgeable. Generate one with "
           "`openssl rand -hex 32`, put it in /etc/ethos-nas/ethos.env as "
           "ETHOS_JWT_SECRET=…, then restart. Set ETHOS_ALLOW_INSECURE=1 only "
           "on a throwaway box.")
    logger.critical(msg)
    if settings.ALLOW_INSECURE:
        logger.warning("ETHOS_ALLOW_INSECURE=1 — continuing with the default JWT secret")
        try:
            from backend.services.notifications_service import emit_event
            emit_event("error", "Insecure JWT secret in use", msg, source="system")
        except Exception:
            logger.debug("Could not emit insecure-secret notification", exc_info=True)
        return
    raise RuntimeError(msg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: startup → yield → shutdown.

    Registers the file mutation event broadcaster (service → WebSocket transport)
    so that every successful file operation is pushed to connected File Manager clients.
    """
    # Wire file mutation events → WebSocket transport for real-time sync
    _setup_event_broadcaster()

    # Wire Notification Center changes → /ws/notifications (badge + sheet push)
    _setup_notification_broadcaster()

    # Wire unified log entries → /ws/logs (live viewer + package attribution)
    _setup_log_broadcaster()

    # Run store migrations / report downgrades before anything reads a store
    try:
        from backend.services import json_store as _json_store

        _json_store.verify_versions()
    except Exception:
        logger.error("Store schema check failed", exc_info=True)

    # Per-user libraries: adopt the pre-multi-user stores into the admin's
    # directory and split the legacy music library (idempotent).
    try:
        from backend.services import user_data as _user_data

        _user_data.adopt_all()
    except Exception:
        logger.error("Per-user store migration failed", exc_info=True)

    # Move package state/directories to packages/apps/<id> (idempotent)
    try:
        from backend.services.packages_store import migrate_legacy as _migrate_packages

        _migrate_packages()
    except Exception:
        logger.error("Package store migration failed", exc_info=True)

    # Recycle bin: move the legacy flat trash into per-user namespaces (idempotent)
    try:
        from backend.services import trash_service as _trash_service

        _trash_migration = await asyncio.to_thread(_trash_service.migrate_legacy)
        if _trash_migration.get("moved") or _trash_migration.get("failed"):
            logger.info("Trash migration: %d item(s) moved, %d failed",
                        _trash_migration.get("moved", 0), _trash_migration.get("failed", 0))
    except Exception:
        logger.error("Trash migration failed", exc_info=True)

    # Start the Downloads watch-folder scanner (no-op unless enabled in config)
    start_watch_folder()

    # Run scheduled backups when their cron expression fires
    start_backup_scheduler()

    # Watch disk capacity + failed systemd units; emit Notification Center events
    start_health_monitor()

    # Record total RX/TX counters for the Network app's throughput history
    start_net_stats_sampler()

    # Trim caches, the recycle bin and abandoned uploads (services/retention_service.py)
    start_retention_sweeper()

    # Temporary, escalating IP bans from failed logins (services/autoban_service.py)
    start_autoban()

    # Core update check (signed release index) + apply helper result reconcile
    from backend.services.update_service import (
        reconcile_result as _reconcile_update_result,
        start_update_checker,
        stop_update_checker,
    )
    start_update_checker()
    try:
        _reconcile_update_result()
    except Exception:
        logger.error("Update result reconcile failed", exc_info=True)

    # OS package updates (apt) — background check + a previously unfinished
    # helper result. Sleeps first, so dev/test instances never touch apt.
    from backend.services.os_update_service import (
        reconcile_result as _reconcile_os_update_result,
        start_os_update_checker,
        stop_os_update_checker,
    )
    start_os_update_checker()
    try:
        _reconcile_os_update_result()
    except Exception:
        logger.error("OS update result reconcile failed", exc_info=True)

    # Cameras: resume motion-detection threads for enabled cameras
    from backend.services.camera_service import start_all_motion_threads, stop_all_motion_threads
    _motion_cams = start_all_motion_threads()
    if _motion_cams:
        logger.info("Surveillance: %d motion-detection thread(s) started", _motion_cams)

    # Resume queued downloads left over from a previous run (re-queued as `waiting` on load)
    await start_download_engine()

    # Start installed `runtime: venv` package services (no-op when none exist)
    from backend.services import package_runtime as _package_runtime
    from backend.services.task_registry import spawn as _spawn_task
    _spawn_task(_package_runtime.autostart_all(), name="package-autostart")

    _warn_insecure_config()

    logger.info("=" * 60)
    logger.info("EthOS NAS starting on %s:%s", settings.HOST, settings.PORT)
    logger.info("=" * 60)
    yield
    _teardown_log_broadcaster()
    await stop_backup_scheduler()
    await stop_download_engine()
    stop_watch_folder()
    await stop_health_monitor()
    await stop_net_stats_sampler()
    await stop_retention_sweeper()
    await stop_autoban()
    await stop_update_checker()
    await stop_os_update_checker()
    from backend.services import package_runtime as _package_runtime_stop
    await _package_runtime_stop.shutdown_all()
    from backend.services.camera_service import stop_all_recordings
    from backend.services.task_registry import shutdown as shutdown_tasks
    await shutdown_tasks()
    stop_all_motion_threads()
    _stopped_recs = stop_all_recordings()
    if _stopped_recs:
        logger.info("Surveillance: %d recording(s) stopped", _stopped_recs)
    logger.info("EthOS NAS shutting down")


app = FastAPI(
    title="EthOS NAS",
    description="Self-hosted Linux NAS management interface",
    version=APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # Honour ETHOS_CORS_ORIGINS in production. The wildcard is a development
    # convenience only: the JWT is also accepted from a cookie, so `*` would
    # let any origin ride an authenticated session.
    allow_origins=["*"] if settings.DEBUG else settings.ALLOWED_ORIGINS,
    allow_credentials=not settings.DEBUG,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth middleware — require JWT on every /api/* except login ────────────────

async def _get_token(request: Request) -> Optional[str]:
    """Extract Bearer token from Authorization header, cookie, or query param."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    # Fallback: check cookies for a JWT (Playwright's page.request shares cookies)
    for value in request.cookies.values():
        if isinstance(value, str) and "." in value and value.startswith("eyJ"):
            return value  # full JWT payload.header.sig
    # Fallback: query param `token=` (used by direct browser navigation — downloads, zip)
    q = request.query_params.get("token", "")
    if q:
        return q.strip()
    # Fallback for older app builds that appended a second `?token=...` to URLs
    # which already had a query string (e.g. /api/files/download?path=...?token=...).
    # The key then lands inside another param's value, so scan the raw query.
    m = re.search(r"ethos_[a-f0-9]{48}", request.url.query or "")
    if m:
        return m.group(0)
    return None


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Require valid JWT on every /api/* route except login."""
    # Starsze buildy apki dopisywały "?token=..." do URL-i, która już miała query
    # string (np. /api/files/download?path=/.../app.apk?token=...). Klucz trafiał do
    # wartości innego parametru → 401 + uszkodzony path. Normalizujemy drugi "?"
    # na "&", żeby token był poprawnym parametrem, a path pozostał czysty.
    qs = request.scope.get("query_string", b"").decode() or ""
    idx = qs.find("?token=")
    if idx > 0:
        request.scope["query_string"] = (qs[:idx] + "&token=" + qs[idx + len("?token="):]).encode()

    path = request.url.path

    # Allow these without token.
    # NOTE: file-serving endpoints (/api/files/raw, /download, /thumbnail,
    # /video-thumbnail) are intentionally NOT public — they accept an absolute
    # path and would allow anonymous arbitrary file reads (e.g. /etc/passwd).
    # Clients pass the JWT via `?token=` query param (see _get_token below),
    # which works for <img>/<video>/download links that can't set headers.
    # EXACT public paths only. Matching with `startswith` against a router
    # prefix is how /api/smart and /api/ethos-nas-service ended up fully
    # unauthenticated (anyone could start/stop/disable the NAS daemon).
    public_paths = {
        "/api/auth/login",
        # First-run setup: a flashed appliance has no account yet, so the
        # wizard has to work before anyone can log in. Both handlers read
        # /etc/ethos-nas/setup.json themselves and stop answering the moment it
        # is marked claimed; /status never reveals the code.
        "/api/setup/status",
        "/api/setup/claim",
        "/api/app/version",
        "/api/packages/desktop-icons",
        "/docs",
        "/docs/oauth2-redirect",
        "/openapi.json",
        "/favicon.ico",
        # PWA plumbing (TODO.md P3-14): the browser fetches both *before* the
        # user has a token — while the login overlay is up — and neither reveals
        # anything (a static JSON manifest and the worker script).
        "/manifest.webmanifest",
        "/sw.js",
    }
    if path in public_paths:
        return await call_next(request)

    # Deliberate public *prefixes*. Each one authenticates internally:
    #   • /api/share-links/public/<token> — unguessable 128-bit token,
    #     traversal-checked in share_links.py::_safe_child
    #   • /api/downloads/qb — qBittorrent-compatible API used by Sonarr/Radarr;
    #     every handler except app/version calls qb_compat._require_auth (SID)
    if path.startswith("/api/share-links/public/"):
        return await call_next(request)
    if path == "/api/downloads/qb" or path.startswith("/api/downloads/qb/"):
        return await call_next(request)

    # Static files and SPA catch-all are also public
    if path.startswith("/static/") or path.startswith("/assets/"):
        return await call_next(request)

    if path == "/":
        return await call_next(request)

    # All other /api/* require a valid token
    token = await _get_token(request)
    from backend.routers.auth import _verify_token
    payload = _verify_token(token or "")
    if not payload and token and token.startswith("ethos_"):
        # API key auth (per-user keys, e.g. EthOS Moments Android app).
        # Keys are long-lived — no expiry check.
        from backend.routers.auth_pam import find_user_by_api_key
        user = find_user_by_api_key(token)
        if user:
            payload = {
                "sub": str(user.get("id", "")),
                "username": user["username"],
                "role": user.get("role", "user"),
            }
    if not payload:
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
        )

    # The registry is authoritative for accounts it knows: a disabled/deleted
    # record rejects the request immediately (a 24 h JWT would otherwise outlive
    # the account) and a role change applies on the next request, not on the
    # next login. Tokens minted for unregistered names (tests, first-boot) keep
    # their claimed role.
    from backend.services import users_store
    record = users_store.find_username(str(payload.get("username") or ""))
    if record is not None:
        if not users_store.is_active(record):
            return JSONResponse(
                status_code=401,
                content={"detail": "Account is disabled"},
            )
        payload = dict(payload)
        payload["role"] = str(record.get("role") or "user")
        payload["sub"] = str(record.get("id") or payload.get("sub") or "")

    # Attach user info to request state for downstream use
    request.state.current_user = payload
    return await call_next(request)


@app.middleware("http")
async def frontend_cache_middleware(request: Request, call_next):
    """Force browser revalidation for frontend assets.

    StaticFiles/FileResponse emit no Cache-Control, so browsers fall back to
    heuristic caching and may keep serving stale JS/HTML long after a deploy —
    users silently run old frontend code (e.g. a fixed terminal resize staying
    broken in their tab). no-cache makes the browser revalidate on every load;
    unchanged files still cost only a cheap 304 via ETag.
    """
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if "javascript" in ctype or "text/html" in ctype or "text/css" in ctype \
            or "image/" in ctype or "font/" in ctype:
        response.headers["Cache-Control"] = "no-cache"
    # The module graph root must never be stale: a cached app.js that still
    # imports a module removed by an upgrade 404s, the whole graph fails to
    # evaluate and the desktop silently stops working (incident 2026-11-15).
    if request.url.path == "/static/app.js" or "text/html" in ctype:
        response.headers["Cache-Control"] = "no-store"
    return response


# ── API routers ───────────────────────────────────────────────────────────────
app.include_router(system_router,    prefix="/api/system",    tags=["System"])
app.include_router(system_update_router, prefix="/api/system/update", tags=["System"])
app.include_router(system_os_update_router, prefix="/api/system/os-update", tags=["System"])
app.include_router(processes_router, prefix="/api/processes", tags=["Processes"])
app.include_router(logs_router,      prefix="/api/logs",      tags=["Logs"])
app.include_router(docker_router,    prefix="/api",           tags=["Docker"])
app.include_router(storage_router,   prefix="/api/storage",   tags=["Storage"])
app.include_router(network_router,   prefix="/api/network",   tags=["Network"])
app.include_router(access_control_router, prefix="/api/security", tags=["Security"])
app.include_router(files_router,     prefix="/api/files",     tags=["Files"])
app.include_router(file_ops_router,  prefix="/api/files",     tags=["File Operations"])
app.include_router(auth_router,      prefix="/api/auth",      tags=["Auth"])
app.include_router(users_router,     prefix="/api/users",     tags=["Users"])
app.include_router(setup_router,     prefix="/api/setup",     tags=["Setup"])
app.include_router(backup_router,     prefix="/api/backup",    tags=["Backup"])
app.include_router(shares_router,     prefix="/api/shares",    tags=["Shares"])
app.include_router(packages_router,   prefix="/api/packages",  tags=["Packages"])
app.include_router(gallery_router,    prefix="/api/gallery",    tags=["Gallery"])
app.include_router(moments_router,    prefix="/api/moments",    tags=["Highlights"])
app.include_router(downloads_router,  prefix="/api/downloads",  tags=["Downloads"])
app.include_router(qb_compat_router,    prefix="/api/downloads/qb/api",    tags=["qBittorrent Compat"])
app.include_router(qb_compat_v2_router, prefix="/api/downloads/qb/api/v2", tags=["qBittorrent Compat v2"])
app.include_router(smart_router,      prefix="/api/smart",     tags=["SMART"])
app.include_router(systemd_router,        prefix="/api/systemd",         tags=["Systemd"])
app.include_router(ethos_service_router,  prefix="/api/ethos-nas-service", tags=["EthOS Service"])
app.include_router(cron_router,           prefix="/api/cron",            tags=["Cron"])
app.include_router(music_router,          prefix="/api/music",           tags=["Music"])
app.include_router(video_station_router,  prefix="/api/videos",          tags=["Videos"])
app.include_router(sync_router,           prefix="/api/sync",            tags=["Sync"])
app.include_router(updates_router,        prefix="/api/app",             tags=["App Updates"])
app.include_router(disk_clean_router,     prefix="/api/disk-clean",      tags=["Disk Cleaner"])
app.include_router(notifications_router,  prefix="/api/notifications",   tags=["Notifications"])
app.include_router(health_router,         prefix="/api/health",          tags=["Health"])
app.include_router(surveillance_router,   prefix="/api/surveillance",    tags=["Cameras"])
app.include_router(share_links_router,    prefix="/api/share-links",      tags=["Share Links"])
app.include_router(editor_router,         prefix="/api/editor",           tags=["Code Editor"])
app.include_router(appearance_router,     prefix="/api/appearance",       tags=["Appearance"])

# ── WebSocket routes (mounted directly — no prefix conflict with HTTP) ────────
app.include_router(terminal_router,   tags=["Terminal"])
# MUST be registered before docker_logs: `/ws/logs/system` is a static route in
# the same router, and the legacy `/ws/logs/{container}` shim below must not
# swallow it.
app.include_router(logs_ws_router,    tags=["Logs"])
app.include_router(docker_logs_router,tags=["Docker"])
app.include_router(file_events_router, tags=["File Events"])
app.include_router(notifications_ws_router, tags=["Notifications"])
app.include_router(stats_ws_router,    tags=["System"])


# ── Frontend (SPA) ────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR / "static")), name="static")

if (FRONTEND_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")


@app.get("/", include_in_schema=False)
async def serve_index() -> FileResponse:
    """Serve the SPA shell."""
    return FileResponse(str(FRONTEND_DIR / "index.html"))


# PWA plumbing (TODO.md P3-14). Both files must live at the origin root: a
# service worker's scope is its own directory, and browsers expect the manifest
# at a stable path. The catch-all below would otherwise answer with index.html.
@app.get("/manifest.webmanifest", include_in_schema=False)
async def web_manifest() -> FileResponse:
    return FileResponse(str(FRONTEND_DIR / "manifest.webmanifest"),
                        media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    return FileResponse(
        str(FRONTEND_DIR / "sw.js"),
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            # The worker itself must never be served from a cache, or an update
            # could be invisible for a day.
            "Cache-Control": "no-cache, max-age=0",
        },
    )


@app.get("/{full_path:path}", include_in_schema=False)
async def catch_all(full_path: str):
    """SPA catch-all — index.html for unknown paths, but a proper 404 JSON for
    unknown API routes (HTML there breaks clients expecting JSON)."""
    if full_path == "api" or full_path.startswith("api/"):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    return FileResponse(str(FRONTEND_DIR / "index.html"))
