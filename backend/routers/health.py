"""EthOS NAS — System Health Router

REST:
- GET  /api/health/status    — current snapshot (disks, failed units) + monitor config/state
- POST /api/health/check-now — run a check immediately; emits Notification Center events
- PUT  /api/health/config    — persist runtime threshold overrides (422 on invalid values)
- DELETE /api/health/config  — clear overrides, back to env defaults
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services import health_monitor_service as hms

logger = logging.getLogger("ethos-nas.routers.health")

router = APIRouter()


class HealthConfigUpdate(BaseModel):
    """Partial update — only provided keys are changed."""
    model_config = {"extra": "forbid"}

    disk_warn_percent: Optional[float] = None
    disk_recover_percent: Optional[float] = None
    memory_warn_percent: Optional[float] = None
    memory_recover_percent: Optional[float] = None
    temp_warn_celsius: Optional[float] = None
    alert_cooldown_seconds: Optional[int] = None
    check_interval_seconds: Optional[int] = None


@router.get("/status")
async def get_status() -> dict:
    """One-shot snapshot. Pure read — never emits events."""
    snap = await asyncio.to_thread(hms.collect_snapshot)
    trends = await asyncio.to_thread(hms.smart_trends)
    return {
        "disks": snap["disks"],
        "failed_units": snap["failed_units"],
        "memory": snap["memory"],
        "temps": snap["temps"],
        "smart": hms.last_smart(),
        "smart_trends": trends,
        "monitor": hms.monitor_status(),
    }


@router.post("/check-now")
async def check_now() -> dict:
    """Run a full health check right now (same logic as the background loop)."""
    try:
        result = await asyncio.to_thread(hms.run_check)
    except Exception as exc:
        logger.error("Health check-now failed", exc_info=True)
        raise HTTPException(status_code=500, detail="Health check failed") from exc
    return {**result, "monitor": hms.monitor_status()}


@router.put("/config")
async def update_config(body: HealthConfigUpdate) -> dict:
    """Persist threshold overrides; takes effect from the next check (no restart)."""
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        effective = await asyncio.to_thread(hms.update_config, patch)
    except ValueError as exc:
        logger.warning("Health config update rejected: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"effective": effective, "defaults": hms._defaults()}


@router.delete("/config")
async def reset_config() -> dict:
    """Clear all overrides — back to env defaults."""
    effective = await asyncio.to_thread(hms.reset_config)
    return {"effective": effective, "defaults": hms._defaults()}
