"""EthOS NAS — System Metrics Router

REST:
- GET  /api/system/status        — one-shot snapshot
- POST /api/system/restart       — graceful uvicorn restart via os.execv
- POST /api/system/shutdown      — sudo systemctl reboot|shutdown (requires confirm)

WebSocket:
- WS /ws/stats            — stream metrics every 2 s
"""

import asyncio
import logging
import os
import signal
import socket
import subprocess
import sys
import time as _time
from pathlib import Path
from typing import Optional

import psutil
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

from backend.config import APP_VERSION, INSTALL_DIR, settings
from backend.services.net_utils import port_is_listening
from backend.routers.auth import require_admin

logger = logging.getLogger("ethos-nas.routers.system")
router = APIRouter()
# Separate root-level router for the stats WebSocket. The REST `router` is
# mounted under /api/system, which would push this WS to /api/system/ws/stats;
# the frontend (and every other WS in the app) expects it at /ws/stats.
stats_ws_router = APIRouter()

_boot_time: float = psutil.boot_time()

# One shared sampler feeds every /ws/stats client. Before this, each open tab
# ran a full psutil snapshot in a thread every STATS_INTERVAL — four dashboards
# meant eight snapshots per second for identical data.
_stats_cache: dict = {"at": 0.0, "data": None}
_stats_task: Optional[asyncio.Task] = None


def _cgroup_memory() -> Optional[dict]:
    """Memory of the unit's own cgroup (v2), if the host exposes it."""
    try:
        rel = Path("/proc/self/cgroup").read_text(encoding="utf-8").strip()
        rel = rel.split("::")[-1].lstrip("/")
        base = Path("/sys/fs/cgroup") / rel
        return {
            "current": int((base / "memory.current").read_text(encoding="utf-8").strip()),
            "peak": int((base / "memory.peak").read_text(encoding="utf-8").strip()),
        }
    except (OSError, ValueError):
        return None


def _uptime_seconds() -> int:
    """Return seconds since system boot."""
    return int(_time.time() - _boot_time)


def _temperature() -> Optional[float]:
    """Return CPU temperature in °C, or None if unavailable."""
    try:
        temps = psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
            if key in temps and temps[key]:
                return round(temps[key][0].current, 1)
        for entries in temps.values():
            if entries:
                return round(entries[0].current, 1)
    except Exception:
        logger.debug("Temperature read failed", exc_info=True)
    return None


def collect_system_stats() -> dict:
    """Collect full system metrics via psutil.

    Returns a JSON-serialisable dict with CPU, memory, disk, network,
    temperature, uptime, and hostname.
    """
    stats: dict = {}

    stats["cpu_percent"] = psutil.cpu_percent(interval=0)
    stats["cpu_per_core"] = list(psutil.cpu_percent(percpu=True, interval=0))

    mem = psutil.virtual_memory()
    stats["memory"] = {
        "total": mem.total,
        "used": mem.used,
        "free": mem.free,
        "available": mem.available,
        "percent": mem.percent,
    }

    swap = psutil.swap_memory()
    stats["swap"] = {
        "total": swap.total,
        "used": swap.used,
        "free": swap.free,
        "percent": swap.percent,
    }
    try:
        from backend.services import pressure_guard

        stats["pressure"] = pressure_guard.snapshot()
    except Exception:  # noqa: BLE001 — a stats read must never fail on this
        logger.debug("Pressure snapshot failed", exc_info=True)
    stats["cgroup"] = _cgroup_memory()

    disk = psutil.disk_usage("/")
    stats["disk"] = {
        "device": "/",
        "total": disk.total,
        "used": disk.used,
        "free": disk.free,
        "percent": disk.percent,
    }

    net = psutil.net_io_counters()
    stats["network"] = {
        "bytes_sent": net.bytes_sent,
        "bytes_recv": net.bytes_recv,
        "packets_sent": net.packets_sent,
        "packets_recv": net.packets_recv,
    }

    stats["temperature"] = _temperature()
    stats["uptime_seconds"] = _uptime_seconds()
    stats["hostname"] = socket.gethostname()

    return stats


@router.get("/status")
async def get_system_status() -> dict:
    """Return a one-shot snapshot of all system metrics."""
    data = await asyncio.to_thread(collect_system_stats)
    try:
        from backend.routers.downloads import engine_ready

        data["download_engine_ready"] = engine_ready()
    except Exception:  # downloads router unavailable — not fatal
        logger.debug("Could not read download engine state", exc_info=True)
    # Hardware virtualization decides whether the `vmm` package can use KVM.
    data["virtualization"] = {"kvm": os.path.exists("/dev/kvm"), "device": "/dev/kvm"}
    # GPUs a package with `capabilities: ["dri"]`/`["nvidia"]` could use. The probe
    # is cached (60 s) and runs off the event loop because it may call `docker info`.
    try:
        from backend.services import gpu_host

        data["gpu"] = await asyncio.to_thread(gpu_host.probe)
    except Exception:  # noqa: BLE001 — a status snapshot must never fail on a probe
        logger.warning("GPU probe failed for the system status", exc_info=True)
    return data


@stats_ws_router.websocket("/ws/stats")
async def ws_stats(websocket: WebSocket) -> None:
    """Stream system metrics as JSON every STATS_INTERVAL seconds."""
    # Validate JWT from query params before accepting connection
    token = websocket.url.query.split("token=")[-1] if "token=" in websocket.url.query else ""
    from backend.routers.auth import _verify_token

    payload = _verify_token(token)
    if not payload:
        await websocket.close(code=4001, reason="Authentication required")
        return
    await websocket.accept()
    try:
        while True:
            data = _stats_cache.get("data")
            stale = data is None or _time.monotonic() - float(_stats_cache.get("at") or 0.0) > 3 * max(
                0.5, settings.STATS_INTERVAL
            )
            if stale:
                # Sampler not running (tests, early startup): compute directly.
                data = await asyncio.to_thread(collect_system_stats)
            await websocket.send_json(data)
            await asyncio.sleep(settings.STATS_INTERVAL)
    except WebSocketDisconnect:
        logger.info("Stats WebSocket disconnected")
    except Exception as exc:
        logger.error("Stats WebSocket error: %s", exc)


async def _stats_loop() -> None:
    """Refresh the shared snapshot for every connected client."""
    while True:
        try:
            data = await asyncio.to_thread(collect_system_stats)
            _stats_cache.update({"at": _time.monotonic(), "data": data})
        except Exception:  # noqa: BLE001 — the sampler must outlive one bad read
            logger.error("System stats sampler failed", exc_info=True)
        await asyncio.sleep(max(0.5, settings.STATS_INTERVAL))


async def start_stats_sampler() -> None:
    global _stats_task
    from backend.services import task_registry

    if _stats_task is None or _stats_task.done():
        _stats_task = task_registry.spawn(_stats_loop(), name="system-stats-sampler")


async def stop_stats_sampler() -> None:
    global _stats_task
    task, _stats_task = _stats_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass  # cancelled on purpose during teardown — nothing to report


# ── Lifecycle endpoints ──────────────────────────────────────────────────────


class _LifecycleRequest(BaseModel):
    action: str  # reboot | shutdown
    confirm: bool = False
    model_config = {"extra": "forbid"}


def _read_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def _reexec_argv() -> Optional[list]:
    """Return the argv list to re-exec uvicorn in place, or None if not possible.

    - ``python -m uvicorn ...``  → sys.argv[0] is ``.../site-packages/uvicorn/__main__.py``
    - ``.venv/bin/uvicorn ...``  → sys.argv[0] is the console-script executable itself
    """
    argv0 = sys.argv[0] if sys.argv else ""
    try:
        parts = Path(argv0).parts
    except OSError:
        return None
    if "uvicorn" in parts and Path(argv0).name == "__main__.py":
        return [sys.executable, "-m", "uvicorn"] + sys.argv[1:]
    if Path(argv0).name == "uvicorn":
        return list(sys.argv)
    return None


async def _stop_children_before_restart() -> None:
    """Stop processes `os.execv` would orphan (it skips the ASGI lifespan).

    Package services are direct children with their own loopback ports; left
    alive they make the new image's autostart fail with “port already in use”.
    Camera ffmpeg recordings are the same class of leak.
    """
    try:
        from backend.services import package_runtime

        await package_runtime.shutdown_all()
    except Exception:
        logger.error("Pre-restart package shutdown failed", exc_info=True)
    try:
        from backend.services.camera_service import stop_all_recordings

        stopped = await asyncio.to_thread(stop_all_recordings)
        if stopped:
            logger.info("Pre-restart: stopped %d camera recording(s)", stopped)
    except Exception:
        logger.error("Pre-restart recording stop failed", exc_info=True)
    # os.execv never runs the lifespan teardown — clear the unclean-stop marker
    # ourselves so the next image does not report a kill (services/startup_guard.py).
    try:
        from backend.services import startup_guard

        startup_guard.mark_clean()
    except Exception:
        logger.debug("Could not clear the startup marker", exc_info=True)


async def _perform_restart() -> None:
    """Actually restart the server. Runs in a background task, after the HTTP response is sent."""
    await _stop_children_before_restart()
    if "multiprocessing.spawn" in _read_cmdline(os.getpid()):
        # We are inside a uvicorn supervisor child (multiprocessing spawn context).
        if "--reload" in _read_cmdline(os.getppid()):
            # Dev mode: bump the mtime of a watched file — uvicorn's WatchFiles
            # reloader detects it and respawns this worker with the same sockets.
            os.utime(Path(__file__).resolve().parent.parent / "main.py")
            logger.info("Reload triggered via backend/main.py mtime bump (reloader will respawn)")
        else:
            # --workers mode: the supervisor health-check respawns dead workers.
            os.kill(os.getpid(), signal.SIGTERM)
    else:
        argv = _reexec_argv()
        if argv is None:
            raise RuntimeError(f"restart not supported for launch mode {sys.argv!r}")
        # Daemon mode: re-exec in place. PID and environment are preserved;
        # listening sockets carry CLOEXEC so the port is released.
        os.execv(argv[0], argv)


def _version_info() -> dict:
    """Build identity for the running server (git sha, frontend hash)."""
    import hashlib
    import subprocess as _sp
    from datetime import datetime, timezone

    root = Path(__file__).resolve().parent.parent.parent
    sha = ""
    try:
        sha = _sp.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3
        ).stdout.strip()
    except Exception:
        logger.debug("git rev-parse unavailable", exc_info=True)
    built_at = datetime.fromtimestamp(Path(__file__).stat().st_mtime, tz=timezone.utc).isoformat()
    fhash = ""
    try:
        fhash = hashlib.sha256((root / "frontend" / "static" / "app.js").read_bytes()).hexdigest()[:12]
    except OSError:
        logger.debug("frontend hash unavailable", exc_info=True)
    return {"version": APP_VERSION, "git_sha": sha, "backend_built_at": built_at, "frontend_hash": fhash}


def _doctor_checks() -> dict:
    """Read-only environment self-check: tools, permissions, secrets."""
    import shutil
    import tempfile

    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "", hint: str = "", required: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "hint": hint, "required": required})

    # Data directory writable
    try:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=str(settings.DATA_DIR), prefix=".doctor-", delete=True):
            pass
        add("data-dir", True, str(settings.DATA_DIR))
    except OSError as exc:
        add("data-dir", False, str(exc), "Fix ownership/permissions of ETHOS_DATA_DIR")

    # JWT secret
    from backend.config import DEFAULT_JWT_SECRET

    secure = settings.JWT_SECRET != DEFAULT_JWT_SECRET
    add(
        "jwt-secret",
        secure,
        "default" if not secure else "custom",
        "Set ETHOS_JWT_SECRET via systemctl edit ethos-nas",
        required=False,
    )

    # External tools
    for tool, why, required in [
        ("ffmpeg", "thumbnails, transcodes, surveillance", True),
        ("rsync", "backup schedules", False),
        ("testparm", "Samba share validation", False),
        ("smartctl", "SMART health", False),
        ("docker", "container app", False),
        ("git", "code editor SCM", False),
    ]:
        path = shutil.which(tool)
        add(f"tool:{tool}", bool(path), path or "not found", f"Install {tool} ({why})", required=required)

    # Docker Engine + Compose v2 (Docker Manager app / Packages stacks)
    from backend.services import docker_host

    try:
        engine = docker_host.engine_status()
    except Exception:
        logger.error("Docker engine probe failed", exc_info=True)
        engine = {}
    add(
        "docker-engine",
        bool(engine.get("daemon")),
        (f"v{engine['version']}" if engine.get("version") else "not reachable"),
        "Open Docker Manager and install Docker Engine",
        required=False,
    )
    add(
        "docker-compose",
        bool(engine.get("compose")),
        engine.get("compose") or "not found",
        "Install the Docker Compose v2 plugin (Docker Manager)",
        required=False,
    )
    add(
        "docker-group",
        bool(engine.get("group_member")),
        "member" if engine.get("group_member") else "not a member",
        "Re-run setup_scripts/setup_service.sh and restart EthOS to join the docker group",
        required=False,
    )

    # OS package update helper (Settings → System). Optional: an install that
    # never re-ran setup_service.sh simply cannot install Ubuntu updates yet.
    from backend.services import os_update_service

    add(
        "os-update-helper",
        os_update_service.helper_available(),
        settings.OS_UPDATE_HELPER,
        "Run: sudo bash setup_scripts/setup_service.sh (installs the OS update helper and its two sudoers rules)",
        required=False,
    )

    # Privileged configuration applied from the signed release (deploy/ethos-provision).
    import json as _json

    try:
        provision = _json.loads((settings.DATA_DIR / "provision" / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        provision = {}
    provision_status = str(provision.get("status") or "not run")
    add(
        "system-provisioning",
        provision_status in ("ok", "noop"),
        f"{provision_status} {provision.get('at') or ''}".strip(),
        "Run: sudo bash setup_scripts/setup_service.sh once — core updates apply "
        "the signed configuration automatically afterwards",
        required=False,
    )

    # The updater installs root helpers from the checkout. The backend must not
    # be able to modify it, or the service could plant its own root helper.
    deploy_dir = Path(INSTALL_DIR) / "deploy"
    writable = os.access(deploy_dir, os.W_OK)
    add(
        "checkout-writable",
        not writable,
        str(deploy_dir),
        "Remove the service user from the repository owner's group "
        "(sudo gpasswd -d <service-user> <owner-group>): a writable checkout lets "
        "the backend plant root helpers",
        required=False,
    )

    # User store reachable
    try:
        from backend.services import users_store

        users = users_store.load()
        add("user-store", True, f"{len(users)} account(s) — {users_store.path()}")
    except Exception as exc:
        add("user-store", False, str(exc), "Check DATA_DIR permissions and users.json")

    # Store schema versions (TODO.md P5-05)
    try:
        from backend.services import json_store

        report = json_store.store_registry_report()
        versioned = [e for e in report if e["version"] is not None]
        add(
            "store-versions",
            True,
            f"{len(report)} store(s) registered, {len(versioned)} schema-versioned",
            "Add `version=`/`migrate=` when a store's shape changes",
        )
    except Exception as exc:
        add("store-versions", False, str(exc), "json_store registry unavailable", required=False)

    # Listening socket
    listening = port_is_listening(settings.PORT)
    add("listening", listening, f"port {settings.PORT}", required=False)

    required_ok = all(c["ok"] for c in checks if c["required"])
    return {"ok": required_ok, "checks": checks}


@router.get("/config-export", dependencies=[Depends(require_admin)])
async def config_export() -> Response:
    """Download the whole NAS state as a zip (admin only — contains API keys)."""
    from datetime import datetime, timezone

    from backend.services import config_archive

    try:
        payload = await asyncio.to_thread(config_archive.export_bytes)
    except OSError:
        logger.error("Config export failed", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not read the data directory")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="ethos-config-{stamp}.zip"'},
    )


@router.post("/config-import", dependencies=[Depends(require_admin)])
async def config_import(file: UploadFile = File(...)) -> dict:
    """Restore stores from a `config-export` archive (admin only, snapshots first)."""
    from backend.services import config_archive

    raw = await file.read()
    try:
        return await asyncio.to_thread(config_archive.import_bytes, raw)
    except config_archive.ArchiveError as exc:
        logger.warning("Config import rejected: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError:
        logger.error("Config import failed", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not write the data directory")


@router.get("/config-snapshots", dependencies=[Depends(require_admin)])
async def config_snapshots() -> dict:
    """List the daily state archives kept in `<DATA_DIR>/snapshots/`."""
    from backend.services import config_archive

    items = await asyncio.to_thread(config_archive.list_snapshots)
    return {
        "snapshots": items,
        "count": len(items),
        "enabled": settings.CONFIG_SNAPSHOT_ENABLED,
        "keep": settings.CONFIG_SNAPSHOT_KEEP,
        "interval": settings.CONFIG_SNAPSHOT_INTERVAL,
    }


@router.post("/config-snapshots", dependencies=[Depends(require_admin)])
async def config_snapshot_now() -> dict:
    """Take a state archive immediately (normally done daily by the sweeper)."""
    from backend.services import config_archive

    path = await asyncio.to_thread(config_archive.take_snapshot, "manual")
    if path is None:
        raise HTTPException(status_code=503, detail="Snapshot disabled or failed — see App Events")
    return {"ok": True, "name": path.name, "bytes": path.stat().st_size}


@router.get("/version")
async def app_version() -> dict:
    """Build identity for the running server."""
    return await asyncio.to_thread(_version_info)


@router.get("/doctor")
async def doctor() -> dict:
    """Environment self-check (missing tools, permissions, insecure defaults)."""
    return await asyncio.to_thread(_doctor_checks)


@router.get("/storage-usage")
async def storage_usage() -> dict:
    """Bytes/files per managed cache area (see services/retention_service.py)."""
    from backend.services.retention_service import usage

    return await asyncio.to_thread(usage)


class RetentionSweepRequest(BaseModel):
    """Optional body for POST /retention/sweep.

    ``areas`` limits the run to the named managed caches (Disk Cleaner reclaims
    one area at a time); omitting it sweeps everything, as before.
    """

    areas: list[str] | None = Field(default=None, max_length=20)

    model_config = {"extra": "forbid"}


@router.post("/retention/sweep", dependencies=[Depends(require_admin)])
async def retention_sweep(req: RetentionSweepRequest | None = None) -> dict:
    """Run the retention policy now ("Clean now" in Settings → System).

    The sweeper already runs on a timer (`ETHOS_RETENTION_INTERVAL`); this is the
    on-demand trigger. The summary is returned *and* emitted to the Notification
    Center when something was actually freed, so the result is visible later.
    """
    from backend.services.notifications_service import emit_event
    from backend.services.retention_service import area_names, sweep

    only = None
    if req and req.areas:
        unknown = sorted(set(req.areas) - set(area_names()))
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown retention area(s): {', '.join(unknown)}",
            )
        only = req.areas

    try:
        summary = await asyncio.to_thread(sweep, only)
    except Exception as exc:
        logger.error("Manual retention sweep failed", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Cleanup failed: {exc}") from exc

    freed = int(summary.get("freed_bytes") or 0)
    removed = int(summary.get("removed_files") or 0)
    if removed:
        emit_event(
            "success",
            "Caches cleaned",
            f"Removed {removed} item(s), freed {freed / 1_048_576:.1f} MB",
            source="storage",
        )
    logger.info("Manual retention sweep: %d item(s), %.1f MB freed", removed, freed / 1_048_576)
    return {"ok": True, **summary}


@router.post("/deps/{requirement}/install", dependencies=[Depends(require_admin)])
async def install_host_dependency(requirement: str) -> dict:
    """Install one allowlisted host requirement (e.g. archive-tools).

    The File Manager offers this when an archive needs an extractor that is not
    installed yet. Only ids from services/system_deps.py qualify, and only
    through the root-owned `ethos-system-deps` helper — never apt directly.
    """
    from backend.services import system_deps
    from backend.services.package_schema import PackageError

    meta = system_deps.SYSTEM_REQUIREMENTS.get(requirement)
    if not meta or not meta.get("serviceable"):
        raise HTTPException(status_code=400, detail=f"{requirement} cannot be installed by EthOS")
    if requirement == "docker":
        # Docker has its own fingerprinted helper and daemon checks.
        raise HTTPException(status_code=400, detail="Install Docker from the Docker Manager")

    try:
        await asyncio.to_thread(system_deps.install, requirement, None)
    except PackageError as exc:
        logger.error("Host dependency install failed (%s): %s", requirement, exc)
        raise HTTPException(status_code=exc.status or 500, detail=exc.detail) from exc
    except Exception as exc:
        logger.error("Host dependency install failed (%s)", requirement, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Install failed: {exc}") from exc

    probe = await asyncio.to_thread(system_deps.probe_one, requirement)
    logger.info("Host dependency %s installed through the API", requirement)
    return {"ok": True, **probe}


@router.post("/restart", dependencies=[Depends(require_admin)])
async def system_restart(background_tasks: BackgroundTasks) -> dict:
    """Gracefully restart the uvicorn process.

    - Daemon mode (``python -m uvicorn ...``): re-exec in place via os.execv, PID preserved.
    - Dev mode (``--reload``): trigger uvicorn's native reload of this worker.
    The response is sent first; the actual restart happens afterwards.
    """
    logger.info("Application restart requested — will restart after response is sent")
    background_tasks.add_task(_perform_restart)
    return {
        "status": "restarting",
        "message": "Restarting application. The page will reload.",
    }


@router.post("/shutdown", dependencies=[Depends(require_admin)])
async def system_shutdown(req: _LifecycleRequest) -> dict:
    """Reboot or shutdown the server via sudo systemctl."""
    if not req.confirm:
        raise HTTPException(status_code=400, detail="Must set confirm=true to proceed")

    target = req.action.lower() if req.action else "reboot"
    if target not in ("reboot", "shutdown"):
        raise HTTPException(status_code=400, detail=f"Unknown action: {target}")

    logger.warning("Server %s requested by admin — sudo systemctl %s", target, target)
    try:
        await asyncio.to_thread(
            subprocess.run,
            ["sudo", "-n", "systemctl", target],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return {"ok": True, "status": "success", "action": target, "message": f"Server will {target}"}
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="sudo or systemctl not available on this system")
    except subprocess.TimeoutExpired:
        logger.error("systemctl %s timed out", target)
        return {"ok": True, "status": "success", "action": target, "message": f"Server will {target}"}
