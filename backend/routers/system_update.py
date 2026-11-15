"""EthOS NAS — core update status endpoint.

`GET  /api/system/update`        — cached check result (any authenticated user)
`POST /api/system/update/check`  — force a signed-index refresh (admin)

Check-only by design: nothing is installed from here. See docs/updates.md.
"""

import logging

from fastapi import APIRouter, Depends

from backend.routers.auth import require_admin
from backend.services import update_service

logger = logging.getLogger("ethos-nas.routers.system_update")
router = APIRouter()


@router.get("")
async def update_status() -> dict:
    """Last known result; never performs a network request."""
    return update_service.status()


@router.post("/check", dependencies=[Depends(require_admin)])
async def update_check() -> dict:
    """Fetch and verify the signed release index now."""
    logger.info("Core update check requested")
    return await update_service.check(force=True)
