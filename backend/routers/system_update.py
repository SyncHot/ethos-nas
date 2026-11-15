"""EthOS NAS — core update status and apply endpoints.

`GET  /api/system/update`         — cached check result (any authenticated user)
`GET  /api/system/update/status`  — helper progress/result (any authenticated user)
`POST /api/system/update/check`   — force a signed-index refresh (admin)
`POST /api/system/update`         — ask the root helper to install it (admin)

Apply is delegated to the argument-free `ethos-update` helper, which verifies
and fetches the signed index itself. See docs/updates.md.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from backend.routers.auth import require_admin
from backend.services import update_service

logger = logging.getLogger("ethos-nas.routers.system_update")
router = APIRouter()


@router.get("")
async def update_status() -> dict:
    """Last known result; never performs a network request."""
    return update_service.status()


@router.get("/status")
async def update_apply_status() -> dict:
    """Progress/result of the root helper (and whether it is installed)."""
    return update_service.apply_status()


@router.post("/check", dependencies=[Depends(require_admin)])
async def update_check() -> dict:
    """Fetch and verify the signed release index now."""
    logger.info("Core update check requested")
    return await update_service.check(force=True)


@router.post("", dependencies=[Depends(require_admin)])
async def update_apply() -> dict:
    """Start the privileged update helper (no-op when already up to date)."""
    logger.info("Core update apply requested")
    try:
        return await update_service.request_apply()
    except update_service.UpdateError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
