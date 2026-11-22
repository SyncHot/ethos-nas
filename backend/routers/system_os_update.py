"""EthOS NAS — Ubuntu OS package update endpoints.

`GET  /api/system/os-update`         — cached package/check status (any user)
`GET  /api/system/os-update/status`  — helper progress/result (any user)
`POST /api/system/os-update/check`   — refresh the apt lists (admin)
`POST /api/system/os-update`         — start `ethos-os-update apply` (admin)

The backend never runs apt itself; both mutating calls go through the
root-owned helper with a fixed, argument-exact sudoers rule. See
docs/os-updates.md.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from backend.routers.auth import require_admin
from backend.services import os_update_service

logger = logging.getLogger("ethos-nas.routers.system_os_update")
router = APIRouter()


@router.get("")
async def os_update_status() -> dict:
    """Cached check result plus live reboot/auto-upgrade state."""
    return os_update_service.status()


@router.get("/status")
async def os_update_apply_status() -> dict:
    """Progress/result of the root helper (and whether it is installed)."""
    return os_update_service.apply_status()


@router.post("/check", dependencies=[Depends(require_admin)])
async def os_update_check() -> dict:
    """Refresh the package lists and report the pending upgrades."""
    logger.info("OS package update check requested")
    try:
        return await os_update_service.check(force=True)
    except os_update_service.OsUpdateError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None


@router.post("", dependencies=[Depends(require_admin)])
async def os_update_apply() -> dict:
    """Start the privileged OS update helper."""
    logger.info("OS package update apply requested")
    try:
        return await os_update_service.request_apply()
    except os_update_service.OsUpdateError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
