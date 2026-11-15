"""EthOS NAS — unified Logs API (docs/logs.md).

REST (mounted at /api/logs):
- GET    /             list the unified event store (admin)
- GET    /apps         per-app summary for the viewer's source tree (admin)
- GET    /stats        ring/disk/dropped counters (admin)
- POST   /ingest       browser + native-package events (any authenticated user)
- DELETE /             clear ring + active file (admin)
- GET    /files        readable files under /var/log (admin)
- GET    /tail         last N lines of a file, plain or .gz (admin)
- GET    /journal      journalctl tail (admin)
- GET    /containers   containers grouped by compose project (= package id)
- GET /app, POST /client, DELETE /app — legacy aliases for one release

Every failure is logged with ``exc_info`` and surfaced as a real HTTP error, never
as the 200-with-"[Error …]" trick the old viewer relied on (docs/api-conventions.md).
"""

import asyncio
import gzip
import logging
import os
import re
import subprocess
import threading
import time
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.config import settings
from backend.routers.auth import require_admin
from backend.services import log_service, packages_store

logger = logging.getLogger("ethos-nas.routers.logs")
router = APIRouter()

_ADMIN = [Depends(require_admin)]

# Host log files may contain credentials and internal hostnames — admin only.
ALLOWED_LOG_DIRS: list[str] = ["/var/log", "/tmp"]
_MAX_GZ_BYTES = 32 * 1024 * 1024

KNOWN_LOG_FILES: list[dict] = [
    {"path": "/var/log/syslog", "label": "System (syslog)"},
    {"path": "/var/log/auth.log", "label": "Auth"},
    {"path": "/var/log/kern.log", "label": "Kernel"},
    {"path": "/var/log/dpkg.log", "label": "Package manager"},
    {"path": "/var/log/apt/history.log", "label": "APT history"},
    {"path": "/var/log/messages", "label": "Messages"},
    {"path": "/var/log/boot.log", "label": "Boot"},
]

_UNIT_RE = re.compile(r"^[A-Za-z0-9@._\-]{1,64}$")

# ── ingest rate limit (per user, events per minute) ──────────────────────────

_INGEST_LOCK = threading.Lock()
_INGEST_HITS: dict[str, list[float]] = {}


def _ingest_allowed(username: str, budget: int) -> bool:
    now = time.time()
    with _INGEST_LOCK:
        hits = [ts for ts in _INGEST_HITS.get(username, []) if now - ts < 60.0]
        if len(hits) >= budget:
            _INGEST_HITS[username] = hits
            return False
        hits.append(now)
        _INGEST_HITS[username] = hits
        return True


# ── path helpers ─────────────────────────────────────────────────────────────

def _validate_log_path(path: str) -> str:
    """Resolve a log path (symlinks included) against the allowlist."""
    if not path or not isinstance(path, str):
        raise HTTPException(status_code=400, detail="Invalid path")
    real = os.path.realpath(path)
    for allowed in ALLOWED_LOG_DIRS:
        if real == allowed or real.startswith(allowed + os.sep):
            return real
    raise HTTPException(status_code=403, detail="Path outside allowed log directories")


def _tail_file(path: str, lines: int) -> list[str]:
    """Return the last `lines` lines of a plain file; raises on failure."""
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), path],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 — re-raised as RuntimeError by the caller
        raise RuntimeError(str(exc)) from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"tail exited {result.returncode}")
    return result.stdout.splitlines()


def _tail_gz(path: str, lines: int) -> list[str]:
    """Tail a rotated `.gz` log without shelling out to zcat."""
    with open(path, "rb") as handle:
        blob = handle.read(_MAX_GZ_BYTES)
    text = gzip.decompress(blob).decode("utf-8", "replace")
    return text.splitlines()[-lines:]


@router.get("/files", dependencies=_ADMIN)
async def list_log_files() -> dict:
    """Return readable host log files (journald is a separate endpoint)."""
    def _scan() -> list[dict]:
        files = []
        for entry in KNOWN_LOG_FILES:
            path = entry["path"]
            if os.path.isfile(path) and os.access(path, os.R_OK):
                files.append({"path": path, "label": entry["label"], "size": os.path.getsize(path)})
        known_paths = {entry["path"] for entry in KNOWN_LOG_FILES}
        try:
            for name in sorted(os.listdir("/var/log")):
                full = f"/var/log/{name}"
                if full not in known_paths and os.path.isfile(full) and os.access(full, os.R_OK):
                    files.append({"path": full, "label": name, "size": os.path.getsize(full)})
        except OSError:
            logger.debug("Could not scan /var/log", exc_info=True)
        return files

    files = await asyncio.to_thread(_scan)
    return {"files": files, "total": len(files)}


@router.get("/tail", dependencies=_ADMIN)
async def tail_log(
    path: str = Query(...),
    lines: int = Query(50, ge=1, le=500),
) -> dict:
    """Return the last N lines of a host log file (plain or rotated `.gz`)."""
    validated = _validate_log_path(path)
    if not os.path.isfile(validated):
        raise HTTPException(status_code=404, detail="Log file not found")
    try:
        if validated.endswith(".gz"):
            content = await asyncio.to_thread(_tail_gz, validated, lines)
        else:
            content = await asyncio.to_thread(_tail_file, validated, lines)
    except (RuntimeError, OSError, EOFError) as exc:
        logger.warning("Reading log file %s failed: %s", validated, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not read {validated}: {exc}")
    return {"path": validated, "lines": content, "count": len(content)}


@router.get("/journal", dependencies=_ADMIN)
async def journal_tail(
    lines: int = Query(200, ge=1, le=2000),
    unit: str = "",
) -> dict:
    """Tail the systemd journal (all units, or one allowlisted-looking unit)."""
    if unit and not _UNIT_RE.fullmatch(unit):
        raise HTTPException(status_code=400, detail="Invalid unit name")

    def _run() -> subprocess.CompletedProcess:
        cmd = ["journalctl", "-q", "--no-pager", "-n", str(lines), "-o", "short-iso"]
        if unit:
            cmd += ["-u", unit]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15)

    try:
        result = await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001 — surfaced with a 503 below
        logger.error("journalctl failed", exc_info=True)
        raise HTTPException(status_code=503, detail=f"journalctl unavailable: {exc}")
    if result.returncode != 0:
        logger.warning("journalctl exited %s: %s", result.returncode, result.stderr.strip())
        raise HTTPException(status_code=500, detail=result.stderr.strip() or "journalctl failed")
    rows = result.stdout.splitlines()
    return {"unit": unit or "all", "lines": rows, "count": len(rows)}


@router.get("/containers", dependencies=_ADMIN)
async def list_containers() -> dict:
    """Containers grouped by compose project; package projects carry their id."""
    def _scan() -> list[dict]:
        import docker

        client = docker.from_env()
        installed = set()
        for record in packages_store.load_installed():
            pkg_id = record.get("id") or record.get("package_id")
            if pkg_id:
                installed.add(str(pkg_id))
        items = []
        for container in client.containers.list(all=True):
            labels = container.labels or {}
            project = labels.get("com.docker.compose.project", "") or ""
            items.append({
                "name": container.name,
                "state": container.status,
                "project": project,
                "service": labels.get("com.docker.compose.service", ""),
                "package_id": project if project in installed else "",
            })
        items.sort(key=lambda item: (item["package_id"] == "", item["project"], item["name"]))
        return items

    try:
        items = await asyncio.to_thread(_scan)
    except Exception as exc:  # noqa: BLE001 — Docker may be missing on this host
        logger.error("Container listing for logs failed", exc_info=True)
        raise HTTPException(status_code=503, detail=f"Docker is not reachable: {exc}")
    return {"containers": items, "total": len(items)}


# ── unified event store ──────────────────────────────────────────────────────

class ClientLogRequest(BaseModel):
    """Body for `POST /api/logs/ingest` (single object or array of these)."""

    level: Literal["debug", "info", "warning", "error"] = "error"
    message: str = Field(default="", max_length=500)
    detail: str = Field(default="", max_length=4000)
    source: str = Field(default="", max_length=120)
    package_id: Optional[str] = Field(default=None, max_length=60)


def _query(limit: int, level: Optional[str], app: Optional[str], source: Optional[str],
           search: str, since: Optional[float]) -> dict:
    service = log_service.get_service()
    entries = service.get(limit=limit, level=level, app=app or None, source=source or None,
                          search=search or "", since=since)
    return {
        "entries": entries,
        # `events` kept so a cached pre-refactor client still renders during rollout.
        "events": entries,
        "counts": service.counts(),
        "total": len(entries),
    }


@router.get("", dependencies=_ADMIN)
async def list_events(
    limit: int = Query(200, ge=1, le=1000),
    level: Optional[str] = Query(default=None, pattern="^(debug|info|warning|error)$"),
    app: Optional[str] = Query(default=None, max_length=80),
    source: Optional[str] = Query(default=None, max_length=120),
    search: str = Query(default="", max_length=200),
    since: Optional[float] = Query(default=None, ge=0),
) -> dict:
    """List unified events, newest first."""
    return _query(limit, level, app, source, search, since)


@router.get("/apps", dependencies=_ADMIN)
async def list_apps() -> dict:
    """Per-app summary for the viewer's source tree."""
    service = log_service.get_service()
    return {"apps": service.apps()}


@router.get("/stats", dependencies=_ADMIN)
async def log_stats() -> dict:
    """Ring/disk/dropped counters for the Logs header and the doctor check."""
    return log_service.get_service().stats()


@router.post("/ingest")
async def ingest_events(request: Request) -> dict:
    """Accept browser/package events. Any authenticated user may report."""
    user = getattr(request.state, "current_user", None) or {}
    username = str(user.get("username") or "anonymous")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    is_batch = isinstance(payload, list)
    items = payload if is_batch else [payload]
    dropped = max(0, len(items) - 50)
    accepted = 0
    budget = settings.LOG_INGEST_PER_MIN
    service = log_service.get_service()

    for item in items[:50]:
        try:
            event = ClientLogRequest(**item) if isinstance(item, dict) else None
        except Exception:
            event = None
        if event is None or not event.message.strip():
            dropped += 1
            continue
        if event.package_id:
            if not packages_store.get_package(event.package_id):
                if not is_batch:
                    raise HTTPException(status_code=403,
                                        detail=f"Package {event.package_id} is not installed")
                dropped += 1
                continue
            app = f"pkg:{event.package_id}"
            source = event.source or "pkg-ui"
        else:
            app = "browser"
            source = event.source or "browser"
        if not _ingest_allowed(username, budget):
            dropped += 1
            continue
        service.record(event.level, source, event.message.strip(), event.detail, app=app)
        accepted += 1

    if dropped:
        service.drop(dropped)
    return {"ok": True, "accepted": accepted, "dropped": dropped}


@router.delete("", dependencies=_ADMIN)
async def clear_events() -> dict:
    """Clear the ring and the active file; rotated history stays on disk."""
    cleared = log_service.get_service().clear()
    logger.info("Unified log store cleared (%d in-memory entries)", cleared)
    return {"ok": True, "cleared": cleared}


# ── legacy aliases (one release) ────────────────────────────────────────────

@router.get("/app", dependencies=_ADMIN)
async def list_app_events(
    limit: int = Query(200, ge=1, le=500),
    level: Optional[str] = None,
    search: str = "",
) -> dict:
    """Legacy alias for `GET /api/logs`."""
    return _query(limit, level, None, None, search, None)


@router.post("/client")
async def report_client_event(request: Request) -> dict:
    """Legacy alias for `POST /api/logs/ingest`."""
    return await ingest_events(request)


@router.delete("/app", dependencies=_ADMIN)
async def clear_app_events() -> dict:
    """Legacy alias for `DELETE /api/logs`."""
    return await clear_events()

