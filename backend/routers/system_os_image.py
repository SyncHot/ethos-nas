"""EthOS NAS — EthOS OS image (A/B) update endpoints.

`GET    /api/system/os-image`           — cached index + slots (any user)
`GET    /api/system/os-image/status`    — download/install job (any user)
`POST   /api/system/os-image/check`     — signed index refresh (admin)
`POST   /api/system/os-image/download`  — download the rootfs artifact (admin)
`POST   /api/system/os-image/install`   — write the standby slot (admin)
`DELETE /api/system/os-image/download`  — remove the downloaded image (admin)

The backend never writes a disk: it downloads and verifies, the root helper
`ethos-os-image apply` verifies again and writes the inactive slot. See
docs/os-image-updates.md.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from backend.routers.auth import require_admin
from backend.services import os_image_service

logger = logging.getLogger("ethos-nas.routers.system_os_image")
router = APIRouter()


def _raise(exc: os_image_service.OsImageError) -> None:
    raise HTTPException(status_code=exc.status, detail=str(exc)) from None


@router.get("")
async def os_image_status() -> dict:
    """Cached index, slot state and the download/install status."""
    return os_image_service.status()


@router.get("/status")
async def os_image_job_status() -> dict:
    """Download and install progress/result."""
    return {"download": os_image_service.download_status(),
            "install": os_image_service.install_status()}


@router.post("/check", dependencies=[Depends(require_admin)])
async def os_image_check() -> dict:
    logger.info("OS image check requested")
    try:
        return await os_image_service.check(force=True)
    except os_image_service.OsImageError as exc:
        _raise(exc)


@router.post("/download", dependencies=[Depends(require_admin)])
async def os_image_download() -> dict:
    logger.info("OS image download requested")
    try:
        return await os_image_service.start_download()
    except os_image_service.OsImageError as exc:
        _raise(exc)


@router.delete("/download", dependencies=[Depends(require_admin)])
async def os_image_download_delete() -> dict:
    logger.info("OS image download removal requested")
    try:
        return await os_image_service.delete_download()
    except os_image_service.OsImageError as exc:
        _raise(exc)


@router.post("/install", dependencies=[Depends(require_admin)])
async def os_image_install() -> dict:
    logger.info("OS image install requested")
    try:
        return await os_image_service.install()
    except os_image_service.OsImageError as exc:
        _raise(exc)
