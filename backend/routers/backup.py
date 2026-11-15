"""EthOS NAS — Backup Manager v2 API (admin-only).

Thin layer over:
- `services/backup_store.py`    schedules/history/settings
- `services/backup_layout.py`   snapshots + legacy migration
- `services/backup_jobs.py`     single-flight queue + cancellation
- `services/backup_engine.py`   rsync hardlink snapshots, docker, verify, restore
- `services/backup_remote.py`   rclone replication

The Packages app `backup-manager` is the UI; the built-in Backup app was
removed. `/meta` lets the package detect an older core.
"""

import asyncio
import logging
import os
import shutil
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.routers.auth import require_admin
from backend.services import (
    backup_docker,
    backup_jobs,
    backup_layout,
    backup_remote,
    backup_store,
)
from backend.services import path_scope
from backend.services.backup_models import (
    CancelRequest,
    RemoteTestRequest,
    RestoreRequest,
    ScheduleCreateRequest,
    ScheduleUpdateRequest,
    SettingsRequest,
    VerifyRequest,
)
from backend.services.path_scope import scope_request

logger = logging.getLogger("ethos-nas.routers.backup")

router = APIRouter(dependencies=[Depends(require_admin), Depends(scope_request)])

API_VERSION = 2


# ── helpers ──────────────────────────────────────────────────────────────────

def _clean_path(value: str, label: str, required: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise HTTPException(status_code=400, detail=f"{label} must be set")
        return ""
    return path_scope.sanitize(raw)


def _require_schedule(schedule_id: str) -> dict:
    schedule = backup_store.find_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return schedule


def _validate_or_400(schedule: dict) -> None:
    error = backup_store.validate_schedule(schedule)
    if error:
        raise HTTPException(status_code=400, detail=error)


def _conflict_or_409(draft: dict, exclude_id: Optional[str] = None) -> None:
    conflict = backup_store.schedule_conflict(draft, backup_store.load_schedules(),
                                              exclude_id=exclude_id)
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)


def _targets_overview() -> list[dict]:
    schedules = backup_store.load_schedules()
    grouped: dict[str, dict] = {}
    for schedule in schedules:
        key = os.path.realpath(schedule.get("destination") or "")
        entry = grouped.setdefault(key, {
            "path": schedule.get("destination") or key,
            "schedules": [], "backup_bytes": 0,
            "online": False, "total_bytes": 0, "free_bytes": 0, "error": "",
        })
        entry["schedules"].append(schedule.get("name") or schedule.get("id"))
        try:
            backup_layout.fill_missing_sizes(schedule)
            entry["backup_bytes"] += backup_layout.backup_usage(schedule)
        except Exception:
            logger.warning("Could not size backups for %s", schedule.get("id"), exc_info=True)
    for entry in grouped.values():
        path = entry["path"]
        if not os.path.isdir(path):
            entry["error"] = "Destination is not available"
            continue
        try:
            stats = os.statvfs(path)
            entry["total_bytes"] = stats.f_blocks * stats.f_frsize
            entry["free_bytes"] = stats.f_bavail * stats.f_frsize
            entry["online"] = True
        except OSError as exc:
            entry["error"] = str(exc)
    return sorted(grouped.values(), key=lambda item: item["path"])


# ── meta / overview ──────────────────────────────────────────────────────────

@router.get("/meta")
async def get_meta() -> dict:
    return {
        "api_version": API_VERSION,
        "engine": "rsync-hardlink",
        "layout": backup_layout.LAYOUT_VERSION,
        "capabilities": ["generations", "retention", "verify", "queue", "cancel",
                         "restore-to-folder", "docker-projects", "rclone"],
    }


@router.get("/overview")
async def get_overview() -> dict:
    schedules = backup_store.load_schedules()
    history = backup_store.load_history()
    status = backup_jobs.status()

    recent_failures = sum(1 for entry in history[:50] if entry.get("status") == "failed")
    recent_skips = sum(1 for entry in history[:50] if entry.get("status") == "skipped")
    last_success = next((entry.get("finished_at") for entry in history
                         if entry.get("status") == "success" and entry.get("kind") == "backup"), None)
    next_runs = [backup_store.public_schedule(s).get("next_run") for s in schedules
                 if s.get("enabled") is not False]
    next_runs = [value for value in next_runs if value]
    targets = await asyncio.to_thread(_targets_overview)
    total_bytes = sum(int(entry.get("backup_bytes") or 0) for entry in targets)

    return {
        "summary": {
            "schedules": len(schedules),
            "enabled": sum(1 for s in schedules if s.get("enabled") is not False),
            "last_success_at": last_success,
            "next_run_at": min(next_runs) if next_runs else None,
            "backup_bytes": total_bytes,
            "recent_failures": recent_failures,
            "recent_skips": recent_skips,
        },
        "targets": targets,
        "recent": history[:5],
        "status": status,
        "settings": backup_store.get_settings(),
    }


@router.get("/settings")
async def get_settings() -> dict:
    return backup_store.get_settings()


@router.put("/settings")
async def put_settings(body: SettingsRequest) -> dict:
    updates = body.model_dump(exclude_none=True)
    return {"ok": True, "settings": backup_store.save_settings(updates)}


# ── schedules ────────────────────────────────────────────────────────────────

@router.get("/schedules")
async def list_schedules() -> dict:
    return {
        "version": backup_store.SCHEMA_VERSION,
        "schedules": [backup_store.public_schedule(s) for s in backup_store.load_schedules()],
        "status": backup_jobs.status(),
    }


@router.post("/schedules", status_code=201)
async def create_schedule(body: ScheduleCreateRequest) -> dict:
    settings = backup_store.get_settings()
    source = _clean_path(body.source, "Source")
    destination = _clean_path(body.destination, "Destination", required=True)
    draft = {
        "name": body.name.strip(),
        "source": source,
        "destination": destination,
        "cron": (body.cron or "").strip() or "0 2 * * *",
        "type": body.type,
        "enabled": bool(body.enabled),
        "options": body.options or {},
        "retention": body.retention or settings.get("default_retention"),
        "verify_after_run": body.verify_after_run or settings.get("verify_after_run"),
        "skip_if_missing": body.skip_if_missing,
        "remote": body.remote or {},
    }
    if not draft["name"]:
        raise HTTPException(status_code=400, detail="Name must be set")
    _validate_or_400(draft)
    _conflict_or_409(draft)

    entry = backup_store.add_schedule({
        **draft,
        "id": backup_store.new_id(),
        "created_at": backup_store.now_iso(),
        "last_status": "never",
    })
    logger.info("Backup schedule created: %s (%s)", entry["id"], entry["name"])
    return {"ok": True, "schedule": backup_store.public_schedule(entry)}


@router.put("/schedules/{schedule_id}")
async def update_schedule(schedule_id: str, body: ScheduleUpdateRequest) -> dict:
    current = _require_schedule(schedule_id)
    updates = body.model_dump(exclude_none=True)
    if "source" in updates:
        updates["source"] = _clean_path(str(updates["source"]), "Source")
    if "destination" in updates:
        updates["destination"] = _clean_path(str(updates["destination"]), "Destination", True)
    if isinstance(updates.get("name"), str):
        updates["name"] = updates["name"].strip()
    merged = {**current, **updates}
    _validate_or_400(merged)
    _conflict_or_409(merged, exclude_id=schedule_id)
    updated = backup_store.replace_schedule(schedule_id, updates)
    if not updated:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"ok": True, "schedule": backup_store.public_schedule(updated)}


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str,
                          delete_data: bool = Query(False)) -> dict:
    schedule = _require_schedule(schedule_id)
    target = os.path.realpath(backup_store.backup_target(schedule))
    destination = os.path.realpath(schedule.get("destination") or "")
    if delete_data and (target in ("/", destination) or not target.startswith(destination + os.sep)):
        raise HTTPException(status_code=400, detail="Refusing to remove an unsafe target path")
    if not backup_store.delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    deleted = False
    if delete_data:
        if os.path.isdir(target):
            await asyncio.to_thread(shutil.rmtree, target, True)
            deleted = True
        logger.warning("Backup schedule %s deleted together with %s", schedule_id, target)
    return {"ok": True, "data_deleted": deleted}


# ── operations ───────────────────────────────────────────────────────────────

@router.post("/schedules/{schedule_id}/run", status_code=202)
async def run_schedule(schedule_id: str) -> dict:
    schedule = _require_schedule(schedule_id)
    _validate_or_400(schedule)
    if backup_jobs.duplicate(schedule_id, "backup"):
        raise HTTPException(status_code=409, detail="This backup is already running or queued")
    op = backup_jobs.submit(schedule, "backup", "manual")
    return {"ok": True, "op_id": op.op_id, "queued": backup_jobs.is_busy()}


@router.post("/schedules/{schedule_id}/dry-run", status_code=202)
async def dry_run_schedule(schedule_id: str) -> dict:
    schedule = _require_schedule(schedule_id)
    _validate_or_400(schedule)
    if backup_jobs.duplicate(schedule_id, "dry-run"):
        raise HTTPException(status_code=409, detail="A dry run is already queued")
    op = backup_jobs.submit(schedule, "dry-run", "manual")
    return {"ok": True, "op_id": op.op_id}


@router.get("/schedules/{schedule_id}/generations")
async def list_generations(schedule_id: str) -> dict:
    schedule = _require_schedule(schedule_id)
    layout = await asyncio.to_thread(backup_layout.ensure_layout, schedule)
    if not layout["ok"]:
        raise HTTPException(status_code=500,
                            detail=f"Could not read the backup folder: {layout['error']}")
    await asyncio.to_thread(backup_layout.fill_missing_sizes, schedule)
    generations = await asyncio.to_thread(backup_layout.list_generations, schedule)
    return {
        "generations": generations,
        "target": backup_store.backup_target(schedule),
        "retention": schedule.get("retention"),
        "migrated": layout.get("generation"),
    }


@router.post("/schedules/{schedule_id}/generations/{generation}/verify", status_code=202)
async def verify_generation(schedule_id: str, generation: str,
                            body: VerifyRequest = VerifyRequest()) -> dict:
    schedule = _require_schedule(schedule_id)
    await asyncio.to_thread(backup_layout.ensure_layout, schedule)
    known = {entry["generation"] for entry in backup_layout.list_generations(schedule)}
    if generation not in known:
        raise HTTPException(status_code=404, detail="Generation not found")
    op = backup_jobs.submit(schedule, "verify", "manual",
                            generation=generation, deep=body.deep)
    return {"ok": True, "op_id": op.op_id}


@router.post("/schedules/{schedule_id}/restore", status_code=202)
async def restore_schedule(schedule_id: str, body: RestoreRequest) -> dict:
    schedule = _require_schedule(schedule_id)
    _validate_or_400(schedule)
    await asyncio.to_thread(backup_layout.ensure_layout, schedule)
    generations = backup_layout.list_generations(schedule)
    usable = [entry for entry in generations if entry.get("status") in ("complete", "legacy")]
    if not usable:
        raise HTTPException(status_code=400, detail="No completed backup to restore yet")
    if body.mode not in ("in_place", "to_folder"):
        raise HTTPException(status_code=400, detail="Restore mode must be in_place or to_folder")
    if body.generation and body.generation != "latest" and \
            body.generation not in {entry["generation"] for entry in usable}:
        raise HTTPException(status_code=404, detail="Generation not found")
    target = None
    if body.mode == "to_folder":
        target = _clean_path(body.target or "", "Restore folder", required=True)
    if backup_jobs.duplicate(schedule_id, "restore"):
        raise HTTPException(status_code=409, detail="A restore is already running or queued")
    op = backup_jobs.submit(schedule, "restore", "manual",
                            generation=body.generation, mode=body.mode,
                            target=target, delete_extra=body.delete_extra)
    return {"ok": True, "op_id": op.op_id}


@router.get("/status")
async def get_status() -> dict:
    return backup_jobs.status()


@router.post("/cancel")
async def cancel_operation(body: CancelRequest = CancelRequest()) -> dict:
    cancelled = backup_jobs.cancel(body.op_id)
    return {"ok": True, "cancelled": cancelled}


# ── history / discovery / destinations ──────────────────────────────────────

@router.get("/history")
async def get_history(limit: int = Query(50, ge=0, le=500),
                      offset: int = Query(0, ge=0)) -> dict:
    return backup_store.history_page(limit=limit, offset=offset)


@router.get("/docker/projects")
async def list_docker_projects(source: Optional[str] = None) -> dict:
    root = _clean_path(source or "", "Source") if source else None
    try:
        projects = await backup_docker.discover_projects(root)
    except backup_docker.DockerUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Docker unavailable: {exc}") from exc
    except Exception as exc:
        logger.error("Docker project discovery failed", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "projects": projects}


@router.get("/remotes")
async def get_remotes() -> dict:
    try:
        remotes = await backup_remote.list_remotes()
    except backup_remote.RemoteUnavailable as exc:
        return {"available": False, "remotes": [], "detail": str(exc)}
    return {"available": True, "remotes": remotes}


@router.post("/remotes/test")
async def test_remote(body: RemoteTestRequest) -> dict:
    return await backup_remote.test_remote(body.remote)


@router.get("/destinations/check")
async def check_destination(path: str = Query(...)) -> dict:
    target = _clean_path(path, "Destination", required=True)
    result = {
        "path": target, "exists": os.path.isdir(target), "is_mount": os.path.ismount(target),
        "writable": False, "free_bytes": 0, "total_bytes": 0, "hardlinks": False, "error": "",
    }
    if not result["exists"]:
        result["error"] = "Folder does not exist"
        return result
    result["writable"] = os.access(target, os.W_OK)
    try:
        stats = os.statvfs(target)
        result["free_bytes"] = stats.f_bavail * stats.f_frsize
        result["total_bytes"] = stats.f_blocks * stats.f_frsize
    except OSError as exc:
        result["error"] = str(exc)
    result["hardlinks"] = await asyncio.to_thread(
        backup_layout._hardlink_probe, target)  # noqa: SLF001 — shared probe
    return result
